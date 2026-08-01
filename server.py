#!/usr/bin/env python3
import os
import re
import time
import json
import argparse
import ipaddress
import shlex
import shutil
import subprocess
import urllib.request
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file, abort

app = Flask(__name__)


def _abs_dir(path: str) -> str:
    """Absolute, ~-expanded form of a configured directory ('' stays '').

    SERVER_DIR ends up in a `cd` that runs *inside the tmux pane*, whose working
    directory is the game dir rather than the panel's, so a relative value would
    resolve against a base the admin never meant. Pin it to the panel's CWD at
    startup instead. Expanding ~ here is also required, not cosmetic: the `cd`
    argument is shlex-quoted, which stops the shell from expanding it for us.
    """
    return os.path.abspath(os.path.expanduser(path)) if path else ""


TMUX_TARGET    = os.environ.get("TMUX_TARGET", "minecraft")
SESSIONS       = [TMUX_TARGET]   # replaced in __main__; kept as list for route helpers
JARS_DIR       = os.environ.get("JARS_DIR", "server-jars")
# The only path here that is absolute; the rest stay relative to the game dir.
SERVER_DIR     = _abs_dir(os.environ.get("SERVER_DIR", ""))
WORLDS_DIR     = os.environ.get("WORLDS_DIR", "world-saves")
MODS_DIR       = os.environ.get("MODS_DIR", "mods")
MODS_SAVES_DIR = os.environ.get("MODS_SAVES_DIR", "mods-saves")

def _detect_version() -> str:
    """VERSION file (packaged builds) or git describe (dev checkouts)."""
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base, "VERSION")) as fh:
            v = fh.read().strip()
            if v:
                return v
    except OSError:
        pass
    try:
        return subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=base, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


VERSION = _detect_version()

# Our public-facing address, so the Server page can show admins what players type.
# Looked up exactly once, at startup — see _fetch_public_ip().
PUBLIC_IP     = None
PUBLIC_IP_URL = "https://api.ipify.org"


def _fetch_public_ip() -> str | None:
    """Ask ipify for our public IP.

    One of only two places the panel reaches the internet (the other is the Fabric
    download). Called once from __main__ so a busy page never turns into traffic
    against someone else's API; if it fails, the panel just doesn't show an IP.
    """
    try:
        with urllib.request.urlopen(PUBLIC_IP_URL, timeout=5) as resp:
            raw = resp.read(64).decode("utf-8", "replace").strip()
        return str(ipaddress.ip_address(raw))   # rejects anything that isn't an IP
    except Exception as e:
        print(f"Could not determine public IP from {PUBLIC_IP_URL}: {e}")
        return None

_ANSI   = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
_MC_FMT = re.compile(r'§[0-9a-fklmnorABCDEFKLMNOR]')


def clean(text: str) -> str:
    return _MC_FMT.sub('', _ANSI.sub('', text))


# Characters a shell would act on. Text we type into the pane is normally read by
# the Minecraft console, but nothing guarantees that: the server may have stopped
# a moment earlier and left a shell on the other end. Everything we send must
# therefore be inert *as a shell command line* — `say hi; rm -rf ~` would run rm.
#   ` $       command substitution and expansion
#   ( )       subshells
#   ; & |     command separators
#   < >       redirection (can truncate files)
#   \         escaping
#   ' "       quoting — an unbalanced quote swallows the lines we send afterwards
#   !         history expansion in interactive shells (`!!` re-runs the last command)
#
# The list is deliberately wider than the minimum needed to stop execution: with
# the separators gone a lone `(` only produces a bash syntax error, but keeping
# the set small and obviously-safe beats relying on our own line always starting
# with `say `/`ban `. The cost is that apostrophes, `!` and brackets don't survive
# into chat, which is why /api/say reports back the text it actually sent.
_SHELL_UNSAFE_RE = re.compile(r'''[`$();&|<>\\'"!\x00-\x1f\x7f-\x9f]''')


def pane_text(raw, limit: int) -> str:
    """Sanitise free text for typing into the pane, whatever is reading it.

    Minecraft treats the remainder as ordinary chat; a shell can do nothing with
    it. Callers that need to report what actually went out should use the return
    value rather than the input.
    """
    return _SHELL_UNSAFE_RE.sub('', str(raw)).strip()[:limit]


def tmux_send(command: str, target: str = None) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", target or TMUX_TARGET, command, "Enter"],
        check=True, capture_output=True,
    )


def tmux_capture(lines: int = 300, target: str = None) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", target or TMUX_TARGET, "-S", f"-{lines}"],
        capture_output=True, text=True, check=True,
    )
    return clean(result.stdout)


def tmux_pane_path(target: str = None) -> str:
    """Return the CWD of the foreground process in the tmux pane.

    Uses /proc to find the terminal's foreground process group (tpgid from
    /proc/<shell_pid>/stat) and resolves /proc/<tpgid>/cwd.  This correctly
    follows nested shells, manual cd after session creation, etc.
    Falls back to tmux's #{pane_current_path} on non-Linux hosts.
    """
    t = target or TMUX_TARGET
    shell_pid = subprocess.run(
        ["tmux", "display-message", "-t", t, "-p", "#{pane_pid}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    try:
        with open(f"/proc/{shell_pid}/stat") as f:
            stat = f.read()
        # comm is wrapped in parens and may contain spaces; strip past the last ')'
        after_comm = stat[stat.rindex(')') + 2:]
        fields = after_comm.split()
        # /proc/pid/stat fields (1-indexed per man page):
        #   3=state 4=ppid 5=pgrp 6=session 7=tty_nr 8=tpgid
        # after stripping pid+(comm) that's 0-indexed fields[0..5]
        tpgid = fields[5]
        if tpgid != "-1":
            return os.readlink(f"/proc/{tpgid}/cwd")
    except (FileNotFoundError, OSError, IndexError, ValueError):
        pass

    # Fallback for non-Linux or missing /proc entry
    result = subprocess.run(
        ["tmux", "display-message", "-t", t, "-p", "#{pane_current_path}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


_WORLD_SAVE_RE = re.compile(r'^world-\d{8}-\d{6}(?:-[a-zA-Z0-9_-]+)?\.tgz$')


def _pane_java_info(target: str = None) -> dict:
    """Scan the pane's tty for a java process; returns {"running": bool, "jar": str|None}.

    Uses #{pane_tty} + ps -t so it finds java regardless of how it was started:
    typed directly, via exec, or as a grandchild of a wrapper script
    (e.g. `bash start.sh` → java).  The tty is inherited by all descendants
    of the pane's shell, so process-tree depth doesn't matter.

    Raises subprocess.CalledProcessError if the tmux target is unreachable.
    """
    t = target or TMUX_TARGET
    pane_tty = subprocess.run(
        ["tmux", "display-message", "-t", t, "-p", "#{pane_tty}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not pane_tty:
        return {"running": False, "jar": None}

    tty = pane_tty.removeprefix("/dev/")
    ps = subprocess.run(
        ["ps", "-t", tty, "-o", "pid=,args="],
        capture_output=True, text=True,
    )
    for line in ps.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        args = parts[1].strip()
        argv = args.split()
        if not argv or os.path.basename(argv[0]) != "java":
            continue
        m = re.search(r"-jar\s+(\S+\.jar)", args)
        return {"running": True, "jar": os.path.basename(m.group(1)) if m else None}

    return {"running": False, "jar": None}


def _is_running(target: str = None) -> bool:
    """Return True if a Minecraft server (java) is running in our tmux pane."""
    try:
        return _pane_java_info(target)["running"]
    except Exception:
        return False


STATE_FILE = ".vibepanel.json"


def _load_state(gdir: str) -> dict:
    """Read the panel's per-game-dir state file ({} if missing/corrupt)."""
    try:
        with open(os.path.join(gdir, STATE_FILE)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _remember_last_jar(gdir: str, session: str, jar: str) -> None:
    """Persist the jar a session last ran, keyed by session name. Best-effort."""
    if not jar:
        return
    try:
        state = _load_state(gdir)
        state.setdefault("last_jar", {})[session] = jar
        path = os.path.join(gdir, STATE_FILE)
        tmp  = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _last_jar(gdir: str, session: str) -> str | None:
    return _load_state(gdir).get("last_jar", {}).get(session)


def _resolve_tmux_target(target: str) -> str:
    """Return target if it exists; if not and exactly one session is visible, use that."""
    # has-session operates on the session name only, not window/pane suffixes.
    session_name = target.split(":")[0]
    exists = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    ).returncode == 0
    if exists:
        return target

    ls = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if ls.returncode != 0:
        return target  # no tmux server running yet; keep configured value

    sessions = [s.strip() for s in ls.stdout.splitlines() if s.strip()]
    if len(sessions) == 1:
        print(f"tmux target '{target}' not found; attaching to sole session '{sessions[0]}'")
        return sessions[0]

    return target


def _session_target() -> str:
    """Return the tmux target for the current request, validated against SESSIONS."""
    name = request.args.get('s', '').strip()
    if len(SESSIONS) == 1 or not name:
        return SESSIONS[0]
    if name not in SESSIONS:
        abort(400, description=f"Unknown session: {name}")
    return name


_MOD_FILE_RE = re.compile(r'^[^\x00/\\]+\.(jar|zip)$', re.IGNORECASE)


def _validate_mod_filename(filename: str) -> bool:
    return bool(_MOD_FILE_RE.match(filename))


def _files_identical(path1: str, path2: str) -> bool:
    """Compare two files byte-for-byte; returns False immediately on size mismatch."""
    if os.path.getsize(path1) != os.path.getsize(path2):
        return False
    with open(path1, 'rb') as f1, open(path2, 'rb') as f2:
        while True:
            b1, b2 = f1.read(65536), f2.read(65536)
            if b1 != b2:
                return False
            if not b1:
                return True


def _do_mod_move(src_dir: str, dst_dir: str, filename: str):
    """Move filename from src_dir to dst_dir, coalescing identical duplicates.

    Returns a Flask response.  If a non-identical file already exists at dst,
    returns 409 with conflict=True so the caller can surface a Delete Both option.
    """
    src = os.path.join(src_dir, filename)
    dst = os.path.join(dst_dir, filename)

    if not os.path.isfile(src):
        return jsonify({"ok": False, "error": "File not found"}), 404

    if os.path.isfile(dst):
        try:
            same = _files_identical(src, dst)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Could not compare files: {e}"}), 500
        if same:
            os.remove(src)
            return jsonify({"ok": True, "coalesced": True})
        return jsonify({
            "ok":       False,
            "conflict": True,
            "error": (
                f"'{filename}' already exists at the destination with different content. "
                "Remove one version manually, or delete both here."
            ),
        }), 409

    os.makedirs(dst_dir, exist_ok=True)
    shutil.move(src, dst)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("index.html", tmux_target=TMUX_TARGET, version=VERSION)


@app.route("/api/sessions")
def api_sessions():
    return jsonify({"sessions": SESSIONS})


@app.route("/api/status")
def api_status():
    target = _session_target()
    try:
        tmux_capture(1, target)
        return jsonify({"ok": True, "target": target})
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503


@app.route("/api/players")
def api_players():
    target = _session_target()
    try:
        tmux_send("list", target)
        time.sleep(0.8)
        output = tmux_capture(50, target)
        for line in reversed(output.strip().splitlines()):
            m = re.search(
                r'There are (\d+) of a max(?: of)? (\d+) players online: ?(.*)',
                line, re.IGNORECASE,
            )
            if m:
                count = int(m.group(1))
                max_p = int(m.group(2))
                names_str = m.group(3).strip()
                players = [p.strip() for p in names_str.split(',') if p.strip()] if count > 0 else []
                return jsonify({"ok": True, "count": count, "max": max_p, "players": players})
        return jsonify({"ok": False, "error": "No response from server — is it running?"})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Player roster (whitelist / ops / bans) ──────────────────────────────
#
# Two write paths, picked by whether the server is up:
#   running  → send console commands; the server owns the json files and would
#              clobber any edit we made behind its back.
#   stopped  → edit whitelist.json / ops.json / banned-players.json directly.

WHITELIST_FILE = "whitelist.json"
OPS_FILE       = "ops.json"
BANNED_FILE    = "banned-players.json"
LOGS_DIR       = "logs"
LATEST_LOG     = "latest.log"

# Only the tail of logs/latest.log is ever read. Rotated .gz archives are slow to
# unpack, and stitching together the tails of several files would mean searching a
# history with holes in it — "seen in the recent log" stays one contiguous window.
LOG_SCAN_MAX_BYTES = 512 * 1024

_MC_NAME_RE   = re.compile(r'^[A-Za-z0-9_]{1,16}$')
_UUID_HEX_RE  = re.compile(r'^[0-9a-fA-F]{32}$')
_UUID_DASH_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

# "[12:34:56] [User Authenticator #1/INFO]: UUID of player Notch is 069a79f4-…"
_LOG_PLAYER_UUID_RE = re.compile(
    r'UUID of player (\S{1,16}) is '
    r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})'
)
_LOG_LINE_TIME_RE = re.compile(r'^\[(\d{2}:\d{2}:\d{2})')


def _normalize_uuid(raw) -> str | None:
    """Return a canonical dashed lowercase UUID, or None if unparseable."""
    s = str(raw or "").strip().lower()
    if _UUID_DASH_RE.match(s):
        return s
    if _UUID_HEX_RE.match(s):
        return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"
    return None


def _read_json_list(path: str) -> list:
    """Read a Minecraft json list file; [] if missing, corrupt, or wrong shape."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _write_json_list(path: str, entries: list) -> None:
    """Atomically write a Minecraft json list file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
    os.replace(tmp, path)


def _entry_matches(entry: dict, name: str, uuid: str | None) -> bool:
    if uuid and _normalize_uuid(entry.get("uuid")) == uuid:
        return True
    return str(entry.get("name", "")).lower() == name.lower()


def _list_remove(path: str, name: str, uuid: str | None) -> bool:
    """Drop every entry matching name or uuid. Returns True if anything changed."""
    entries = _read_json_list(path)
    kept    = [e for e in entries if not _entry_matches(e, name, uuid)]
    if len(kept) == len(entries):
        return False
    _write_json_list(path, kept)
    return True


def _list_upsert(path: str, entry: dict) -> None:
    """Insert entry, or merge it over the existing record for that player."""
    name    = str(entry.get("name", ""))
    uuid    = _normalize_uuid(entry.get("uuid"))
    entries = _read_json_list(path)

    out, merged = [], False
    for e in entries:
        if _entry_matches(e, name, uuid):
            if not merged:                      # keep fields we don't manage
                out.append({**e, **entry})
                merged = True
            continue                            # drop any duplicate records
        out.append(e)
    if not merged:
        out.append(entry)
    _write_json_list(path, out)


def _read_server_properties(gdir: str) -> dict:
    props = {}
    path  = os.path.join(gdir, "server.properties")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, val = line.partition("=")
                props[key.strip()] = val.strip()
    except OSError:
        pass
    return props


def _scan_latest_log(gdir: str) -> list:
    """Scrape name→UUID pairs from the tail of logs/latest.log.

    Reads at most LOG_SCAN_MAX_BYTES from the end of that one file — no rotated
    archives — so the result is one contiguous slice of history.
    """
    path = os.path.join(gdir, LOGS_DIR, LATEST_LOG)
    try:
        mtime = os.path.getmtime(path)
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > LOG_SCAN_MAX_BYTES:
                fh.seek(size - LOG_SCAN_MAX_BYTES)
            blob = fh.read().decode("utf-8", "replace")
    except OSError:
        return []

    # Log lines carry only [HH:MM:SS]. Within a single latest.log the clock runs
    # forward (a restart rotates the old file away), so every backwards jump is a
    # midnight rollover: count them, then date each hit by counting back from the
    # file's mtime, which is the date of its last line.
    hits, day, stamp = [], 0, None
    for line in blob.splitlines():
        t = _LOG_LINE_TIME_RE.match(line)
        if t:
            if stamp and t.group(1) < stamp:
                day += 1
            stamp = t.group(1)

        hit = _LOG_PLAYER_UUID_RE.search(line)
        if not hit:
            continue
        name = hit.group(1)
        uuid = _normalize_uuid(hit.group(2))
        if uuid and _MC_NAME_RE.match(name):
            hits.append((day, stamp, name, uuid))

    last_day = day
    base     = datetime.fromtimestamp(mtime).date()

    found = {}
    for day, stamp, name, uuid in hits:
        date = base - timedelta(days=last_day - day)
        seen = f"{date} {stamp}" if stamp else str(date)
        e = found.setdefault(uuid, {"name": name, "uuid": uuid,
                                    "seen": 0, "last_seen": seen})
        e["seen"] += 1
        e["name"]  = name                   # newest spelling wins
        if seen > e["last_seen"]:            # zero-padded, so string compare is fine
            e["last_seen"] = seen

    return sorted(found.values(), key=lambda e: e["last_seen"], reverse=True)


def _resolve_uuid(gdir: str, name: str, hint: str | None) -> tuple[str | None, str | None]:
    """Find a UUID for name without leaving the box. Returns (uuid, error_message).

    Sources, in order: the UUID the admin supplied, the json files we already
    manage, then logs/latest.log. There is deliberately no online lookup — the
    panel never talks to anything but its own server and its own files.
    """
    if hint:
        return hint, None

    for fname in (WHITELIST_FILE, OPS_FILE, BANNED_FILE):
        for e in _read_json_list(os.path.join(gdir, fname)):
            if str(e.get("name", "")).lower() == name.lower():
                uuid = _normalize_uuid(e.get("uuid"))
                if uuid:
                    return uuid, None

    for s in _scan_latest_log(gdir):
        if s["name"].lower() == name.lower():
            return s["uuid"], None

    return None, (f"No UUID on record for '{name}' — not in the player lists and not "
                  f"in the current {LATEST_LOG}. Paste their UUID to add them anyway, "
                  "or start the server and add them there.")


def _parse_player_body(target: str):
    """Validate the {name, uuid?} body common to the player endpoints.

    Returns (ctx, error_response); exactly one of the two is None.
    """
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not _MC_NAME_RE.match(name):
        return None, (jsonify({"ok": False, "error": "Invalid player name"}), 400)

    raw_uuid = str(data.get("uuid", "")).strip()
    uuid     = _normalize_uuid(raw_uuid) if raw_uuid else None
    if raw_uuid and not uuid:
        return None, (jsonify({"ok": False, "error": "Invalid UUID"}), 400)

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return None, (jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503)

    return {"data": data, "gdir": gdir, "name": name, "uuid": uuid,
            "running": _is_running(target)}, None


def _console(target: str, *commands: str):
    """Send console commands in order, pacing them so the server reads each one."""
    for i, cmd in enumerate(commands):
        if i:
            time.sleep(0.3)
        tmux_send(cmd, target)


@app.route("/api/players/roster")
def api_players_roster():
    """Merged whitelist / ops / bans, plus add-suggestions scraped from logs."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    try:
        players = {}

        def entry(name, uuid):
            uuid = _normalize_uuid(uuid)
            key  = uuid or f"name:{str(name).lower()}"
            e = players.setdefault(key, {
                "name": name, "uuid": uuid, "whitelisted": False,
                "op": False, "op_level": None, "banned": False,
                "ban_reason": None, "ban_expires": None,
            })
            if name:
                e["name"] = name
            return e

        for x in _read_json_list(os.path.join(gdir, WHITELIST_FILE)):
            entry(x.get("name"), x.get("uuid"))["whitelisted"] = True
        for x in _read_json_list(os.path.join(gdir, OPS_FILE)):
            e = entry(x.get("name"), x.get("uuid"))
            e["op"]       = True
            e["op_level"] = x.get("level")
        for x in _read_json_list(os.path.join(gdir, BANNED_FILE)):
            e = entry(x.get("name"), x.get("uuid"))
            e["banned"]      = True
            e["ban_reason"]  = x.get("reason")
            e["ban_expires"] = x.get("expires")

        known_uuids = {e["uuid"] for e in players.values() if e["uuid"]}
        known_names = {str(e["name"]).lower() for e in players.values() if e["name"]}
        suggestions = [
            s for s in _scan_latest_log(gdir)
            if s["uuid"] not in known_uuids and s["name"].lower() not in known_names
        ]

        props = _read_server_properties(gdir)
        # Report where we looked and what was there. An empty roster is almost always
        # a wrong game dir (the pane sitting somewhere else), not an empty whitelist,
        # and without this the page can only say "no players" and look broken.
        return jsonify({
            "ok":                True,
            "players":           sorted(players.values(),
                                        key=lambda e: str(e["name"] or "").lower()),
            "suggestions":       suggestions,
            "whitelist_enabled": props.get("white-list", "").lower() == "true",
            "game_dir":          gdir,
            "files":             {f: os.path.isfile(os.path.join(gdir, f))
                                  for f in (WHITELIST_FILE, OPS_FILE, BANNED_FILE)},
            "log_found":         os.path.isfile(os.path.join(gdir, LOGS_DIR, LATEST_LOG)),
            "running":           _is_running(target),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/players/add", methods=["POST"])
def api_players_add():
    """Whitelist a player (optionally op'ing them at the same time)."""
    target = _session_target()
    ctx, err = _parse_player_body(target)
    if err:
        return err
    name   = ctx["name"]
    add_op = bool(ctx["data"].get("op"))

    if ctx["running"]:
        cmds = [f"whitelist add {name}"] + ([f"op {name}"] if add_op else [])
        try:
            _console(target, *cmds)
        except subprocess.CalledProcessError as e:
            return jsonify({"ok": False, "error": str(e)}), 503
        return jsonify({"ok": True, "via": "console",
                        "message": f"Added {name} to the whitelist" + (" as an op" if add_op else "")})

    uuid, e = _resolve_uuid(ctx["gdir"], name, ctx["uuid"])
    if not uuid:
        return jsonify({"ok": False, "error": e}), 400
    try:
        _list_upsert(os.path.join(ctx["gdir"], WHITELIST_FILE), {"uuid": uuid, "name": name})
        if add_op:
            _list_upsert(os.path.join(ctx["gdir"], OPS_FILE), {
                "uuid": uuid, "name": name, "level": 4, "bypassesPlayerLimit": False,
            })
    except OSError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True, "via": "file",
                    "message": f"Added {name} to {WHITELIST_FILE}" + (f" and {OPS_FILE}" if add_op else "")})


@app.route("/api/players/op", methods=["POST"])
def api_players_op():
    """Grant or revoke operator status."""
    target = _session_target()
    ctx, err = _parse_player_body(target)
    if err:
        return err
    name    = ctx["name"]
    want_op = bool(ctx["data"].get("op", True))

    if ctx["running"]:
        try:
            _console(target, f"{'op' if want_op else 'deop'} {name}")
        except subprocess.CalledProcessError as e:
            return jsonify({"ok": False, "error": str(e)}), 503
        return jsonify({"ok": True, "via": "console",
                        "message": f"{'Op' if want_op else 'De-op'}'d {name}"})

    ops_path = os.path.join(ctx["gdir"], OPS_FILE)
    try:
        if want_op:
            uuid, e = _resolve_uuid(ctx["gdir"], name, ctx["uuid"])
            if not uuid:
                return jsonify({"ok": False, "error": e}), 400
            _list_upsert(ops_path, {"uuid": uuid, "name": name,
                                    "level": 4, "bypassesPlayerLimit": False})
        else:
            _list_remove(ops_path, name, ctx["uuid"])
    except OSError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True, "via": "file",
                    "message": f"{'Op' if want_op else 'De-op'}'d {name} in {OPS_FILE}"})


@app.route("/api/players/remove", methods=["POST"])
def api_players_remove():
    """Forget a player: de-op and drop them from the whitelist. Bans are untouched."""
    target = _session_target()
    ctx, err = _parse_player_body(target)
    if err:
        return err
    name = ctx["name"]

    if ctx["running"]:
        try:
            _console(target, f"deop {name}", f"whitelist remove {name}")
        except subprocess.CalledProcessError as e:
            return jsonify({"ok": False, "error": str(e)}), 503
        return jsonify({"ok": True, "via": "console",
                        "message": f"Removed {name} from the whitelist and ops"})

    try:
        removed = any([
            _list_remove(os.path.join(ctx["gdir"], OPS_FILE), name, ctx["uuid"]),
            _list_remove(os.path.join(ctx["gdir"], WHITELIST_FILE), name, ctx["uuid"]),
        ])
    except OSError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True, "via": "file",
                    "message": f"Removed {name}" if removed else f"{name} was not listed"})


@app.route("/api/players/ban", methods=["POST"])
def api_players_ban():
    """Ban (block) or pardon a player."""
    target = _session_target()
    ctx, err = _parse_player_body(target)
    if err:
        return err
    name = ctx["name"]
    ban  = bool(ctx["data"].get("ban", True))
    # Same treatment as /api/say: the reason is free text and goes out as part of
    # a console line, so it must be inert to both the pty and a shell.
    reason = pane_text(ctx["data"].get("reason", ""), 120)

    if ctx["running"]:
        cmd = (f"ban {name} {reason}".strip() if ban else f"pardon {name}")
        try:
            _console(target, cmd)
        except subprocess.CalledProcessError as e:
            return jsonify({"ok": False, "error": str(e)}), 503
        return jsonify({"ok": True, "via": "console",
                        "message": f"{'Banned' if ban else 'Pardoned'} {name}"})

    banned_path = os.path.join(ctx["gdir"], BANNED_FILE)
    try:
        if ban:
            uuid, e = _resolve_uuid(ctx["gdir"], name, ctx["uuid"])
            if not uuid:
                return jsonify({"ok": False, "error": e}), 400
            _list_upsert(banned_path, {
                "uuid":    uuid,
                "name":    name,
                "created": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                "source":  "VibePanel",
                "expires": "forever",
                "reason":  reason or "Banned by an operator.",
            })
        else:
            _list_remove(banned_path, name, ctx["uuid"])
    except OSError as ex:
        return jsonify({"ok": False, "error": str(ex)}), 500
    return jsonify({"ok": True, "via": "file",
                    "message": f"{'Banned' if ban else 'Pardoned'} {name} in {BANNED_FILE}"})


@app.route("/api/say", methods=["POST"])
def api_say():
    target = _session_target()
    try:
        data = request.get_json(force=True, silent=True) or {}
        raw = str(data.get("message", "")).strip()
        if not raw:
            return jsonify({"ok": False, "error": "Empty message"}), 400
        if len(raw) > 256:
            return jsonify({"ok": False, "error": "Message too long (max 256 chars)"}), 400
        # Drops control characters (\x03 Ctrl+C, \x1a Ctrl+Z and friends would reach
        # the pane's foreground process through the pty and could kill the server)
        # and shell metacharacters, in case a shell is reading rather than Minecraft.
        message = pane_text(raw, 256)
        if not message:
            return jsonify({"ok": False,
                            "error": "Nothing left to send after removing characters "
                                     "a shell could act on"}), 400
        tmux_send(f"say {message}", target)
        # Report what actually went out, so the panel's history doesn't claim we
        # broadcast something we altered.
        return jsonify({"ok": True, "sent": message})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/status")
def api_server_status():
    """Detect whether a Minecraft server process is running inside our tmux pane.

    `ok` distinguishes "the server is stopped" from "we can't see the pane at all" —
    both report running=False, and conflating them makes an unreachable session look
    like an idle one.
    """
    target = _session_target()
    try:
        return jsonify({**_pane_java_info(target), "ok": True})
    except subprocess.CalledProcessError:
        return jsonify({"running": False, "ok": False,
                        "error": f"tmux target '{target}' not found"})
    except Exception as e:
        return jsonify({"running": False, "ok": False, "error": str(e)})


@app.route("/api/server/jars")
def api_server_jars():
    """List .jar files available in the configured jars directory."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503
    try:
        jars_path = os.path.join(gdir, JARS_DIR)
        os.makedirs(jars_path, exist_ok=True)
        jars = sorted(f for f in os.listdir(jars_path) if f.endswith(".jar"))
        return jsonify({"ok": True, "jars": jars, "jars_dir": JARS_DIR,
                        "last_jar": _last_jar(gdir, target.split(":")[0])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/start", methods=["POST"])
def api_server_start():
    """Send the java start command to the tmux session."""
    target = _session_target()
    data   = request.get_json(force=True, silent=True) or {}
    jar    = str(data.get("jar", "")).strip()
    mem    = str(data.get("mem", "1024M")).strip().upper()

    if not re.match(r'^[\w][\w\-\.]*\.jar$', jar):
        return jsonify({"ok": False, "error": "Invalid jar name"}), 400
    if not re.match(r'^\d+[MG]$', mem):
        return jsonify({"ok": False, "error": "Invalid memory value — use e.g. 1024M or 2G"}), 400

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    jar_path = os.path.join(gdir, JARS_DIR, jar)
    if not os.path.isfile(jar_path):
        return jsonify({"ok": False, "error": f"Jar not found: {jar}"}), 404

    if _is_running(target):
        return jsonify({"ok": False, "error": "Server is already running"}), 409

    # Unlike the console commands, this line is *meant* for a shell. jar_path is
    # built from the pane's CWD and JARS_DIR, and SERVER_DIR comes from config —
    # none of which we control, so quote them. Also makes paths containing spaces
    # work, which they previously did not.
    cmd = f"java -Xmx{mem} -Xms{mem} -jar {shlex.quote(jar_path)} nogui"
    if SERVER_DIR:
        cmd = f"cd {shlex.quote(SERVER_DIR)} && {cmd}"

    try:
        session = target.split(":")[0]
        has = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True)
        if has.returncode == 0:
            tmux_send(cmd, target)
        else:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session, cmd],
                check=True, capture_output=True,
            )
        # Also remember on start, so stops that happen outside the panel
        # (console 'stop', crash) still leave a sensible default behind.
        _remember_last_jar(gdir, session, jar)
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/stop", methods=["POST"])
def api_server_stop():
    """Send the 'stop' command to the Minecraft server console via tmux."""
    target = _session_target()
    if not _is_running(target):
        return jsonify({"ok": False, "error": "Server is not running"}), 409
    # Remember the jar this server was running so the UI can preselect it
    # next time. Best-effort: never block the stop.
    try:
        jar = _pane_java_info(target).get("jar")
        _remember_last_jar(tmux_pane_path(target), target.split(":")[0], jar)
    except Exception:
        pass
    try:
        tmux_send("stop", target)
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/api/server/download-fabric", methods=["POST"])
def api_download_fabric():
    """Run get-me-fabric.sh to download a Fabric server jar into JARS_DIR."""
    target  = _session_target()
    data    = request.get_json(force=True, silent=True) or {}
    version = str(data.get("version", "")).strip()
    app.logger.debug(f"Requested Fabric download for version: '{version}' ({type(version)})")
    app.logger.debug(f"Request JSON data: {json.dumps(data)}")
    if version == "None":
        version = None

    if version and not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.\-]*$', version):
        return jsonify({"ok": False, "error": "Invalid version string"}), 400

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    script = os.path.join(gdir, "get-me-fabric.sh")
    if not os.path.isfile(script):
        bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "get-me-fabric.sh")
        if not os.path.isfile(bundled):
            return jsonify({"ok": False, "error": f"Script not found: {script}"}), 404
        try:
            shutil.copy2(bundled, script)
            os.chmod(script, 0o755)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Could not install get-me-fabric.sh: {e}"}), 500

    jars_path = os.path.join(gdir, JARS_DIR)
    os.makedirs(jars_path, exist_ok=True)
    cmd = [script, jars_path]
    if version:
        cmd.append(version)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, cwd=gdir,
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return jsonify({"ok": True, "output": output})
        return jsonify({
            "ok":    False,
            "error": f"Script exited with code {result.returncode}",
            "output": output,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Download timed out after 120 s"}), 504
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/identity")
def api_server_identity():
    """Return server icon availability and cleaned MOTD from server.properties."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    has_icon = os.path.isfile(os.path.join(gdir, "server-icon.png"))

    motd = None
    port = None
    props = os.path.join(gdir, "server.properties")
    if os.path.isfile(props):
        try:
            with open(props, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    key, _, val = line.strip().partition("=")
                    if key == "motd":
                        motd = val
                    elif key == "server-port":
                        try:
                            port = int(val.strip())
                        except ValueError:
                            pass
            if motd is not None:
                # Resolve \uXXXX escapes first (§ = § is common in MOTDs)
                motd = re.sub(r'\\u([0-9a-fA-F]{4})',
                              lambda m: chr(int(m.group(1), 16)), motd)
                # Strip § colour/formatting codes
                motd = re.sub(r'§.', '', motd)
                # Convert remaining Java property escapes
                motd = motd.replace('\\n', '\n').replace('\\t', '\t') \
                           .replace('\\\\', '\\')
                motd = motd.strip() or None
        except Exception:
            pass

    # public_ip is host-wide, so every session's Server page gets the same value.
    return jsonify({"ok": True, "has_icon": has_icon, "motd": motd,
                    "port": port, "public_ip": PUBLIC_IP})


@app.route("/api/server/icon")
def api_server_icon():
    """Serve the server-icon.png from the tmux pane's working directory."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    icon = os.path.join(gdir, "server-icon.png")
    if not os.path.isfile(icon):
        return jsonify({"ok": False, "error": "No server-icon.png"}), 404
    return send_file(icon, mimetype="image/png")


def _latest_minecraft_version() -> str | None:
    """Fetch the latest stable Minecraft version from the Fabric meta API."""
    url = "https://meta.fabricmc.net/v2/versions/game"
    with urllib.request.urlopen(url, timeout=8) as resp:
        data = json.loads(resp.read())
    versions = [
        v["version"] for v in data
        if v.get("stable") and "." in v["version"] and "rc" not in v["version"].lower()
    ]
    if not versions:
        return None
    def _ver_key(s):
        try:
            return tuple(int(x) for x in s.split("."))
        except ValueError:
            return (0,)
    return max(versions, key=_ver_key)


@app.route("/api/server/latest-minecraft")
def api_latest_minecraft():
    """Return the latest stable Minecraft version according to the Fabric meta API."""
    try:
        ver = _latest_minecraft_version()
        if ver:
            return jsonify({"ok": True, "version": ver})
        return jsonify({"ok": False, "error": "No stable version found"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/worlds/list")
def api_worlds_list():
    """List .tgz world saves in WORLDS_DIR with per-file and total sizes."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    saves_path = os.path.join(gdir, WORLDS_DIR)
    os.makedirs(saves_path, exist_ok=True)

    try:
        saves = []
        total = 0
        for f in sorted(
            (f for f in os.listdir(saves_path) if f.endswith(".tgz")),
            reverse=True,
        ):
            fp   = os.path.join(saves_path, f)
            size = os.path.getsize(fp)
            total += size
            saves.append({"name": f, "size": size})
        return jsonify({"ok": True, "saves": saves, "total_bytes": total})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/worlds/save", methods=["POST"])
def api_worlds_save():
    """Tar the 'world' directory into a timestamped archive in WORLDS_DIR."""
    target = _session_target()
    if _is_running(target):
        return jsonify({"ok": False, "error": "Server must be stopped before saving a world"}), 409

    data = request.get_json(force=True, silent=True) or {}
    name = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get("name", "")).strip())[:50]

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    world_path = os.path.join(gdir, "world")
    if not os.path.isdir(world_path):
        return jsonify({"ok": False, "error": "No 'world' directory found"}), 404

    saves_path = os.path.join(gdir, WORLDS_DIR)
    os.makedirs(saves_path, exist_ok=True)

    ts       = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"world-{ts}-{name}.tgz" if name else f"world-{ts}.tgz"
    out_path = os.path.join(saves_path, filename)

    try:
        subprocess.run(
            ["tar", "-czf", out_path, "-C", gdir, "world"],
            check=True, capture_output=True, text=True,
        )
        return jsonify({"ok": True, "filename": filename, "size": os.path.getsize(out_path)})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": e.stderr or str(e)}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/worlds/load", methods=["POST"])
def api_worlds_load():
    """Autosave current world, delete it, then extract the selected archive."""
    target = _session_target()
    if _is_running(target):
        return jsonify({"ok": False, "error": "Server must be stopped before loading a world"}), 409

    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()

    if not _WORLD_SAVE_RE.match(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    saves_path   = os.path.join(gdir, WORLDS_DIR)
    archive_path = os.path.join(saves_path, filename)
    if not os.path.isfile(archive_path):
        return jsonify({"ok": False, "error": f"Save not found: {filename}"}), 404

    world_path = os.path.join(gdir, "world")
    autosaved  = None

    if os.path.isdir(world_path):
        os.makedirs(saves_path, exist_ok=True)
        ts        = datetime.now().strftime("%Y%m%d-%H%M%S")
        autosaved = f"world-{ts}-autosave.tgz"
        auto_path = os.path.join(saves_path, autosaved)
        try:
            subprocess.run(
                ["tar", "-czf", auto_path, "-C", gdir, "world"],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            return jsonify({"ok": False, "error": f"Autosave failed: {e.stderr or str(e)}"}), 500
        try:
            shutil.rmtree(world_path)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed to remove current world: {e}"}), 500

    try:
        subprocess.run(
            ["tar", "-xzf", archive_path, "-C", gdir],
            check=True, capture_output=True, text=True,
        )
        return jsonify({"ok": True, "autosaved": autosaved})
    except subprocess.CalledProcessError as e:
        return jsonify({
            "ok":        False,
            "error":     f"Extract failed: {e.stderr or str(e)}",
            "autosaved": autosaved,
        }), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "autosaved": autosaved}), 500


@app.route("/api/worlds/delete", methods=["POST"])
def api_worlds_delete():
    """Delete a single world save archive."""
    target   = _session_target()
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()

    if not _WORLD_SAVE_RE.match(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    file_path = os.path.join(gdir, WORLDS_DIR, filename)
    if not os.path.isfile(file_path):
        return jsonify({"ok": False, "error": "File not found"}), 404

    try:
        os.remove(file_path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/worlds/delete-autosaves", methods=["POST"])
def api_worlds_delete_autosaves():
    """Delete all autosave archives from WORLDS_DIR."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    saves_path = os.path.join(gdir, WORLDS_DIR)
    if not os.path.isdir(saves_path):
        return jsonify({"ok": True, "deleted": 0})

    deleted = 0
    errors  = []
    for f in os.listdir(saves_path):
        if re.match(r'^world-\d{8}-\d{6}-autosave\.tgz$', f):
            try:
                os.remove(os.path.join(saves_path, f))
                deleted += 1
            except Exception as e:
                errors.append(str(e))

    if errors:
        return jsonify({"ok": False, "error": "; ".join(errors), "deleted": deleted}), 500
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/mods/list")
def api_mods_list():
    """List active (mods/) and inactive (mods-saves/) mod files with sizes."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    os.makedirs(os.path.join(gdir, MODS_DIR), exist_ok=True)
    os.makedirs(os.path.join(gdir, MODS_SAVES_DIR), exist_ok=True)

    def _scan(path):
        if not os.path.isdir(path):
            return []
        entries = []
        for f in sorted(os.listdir(path), key=str.lower):
            if _MOD_FILE_RE.match(f):
                try:
                    entries.append({"name": f, "size": os.path.getsize(os.path.join(path, f))})
                except OSError:
                    pass
        return entries

    try:
        return jsonify({
            "ok":       True,
            "active":   _scan(os.path.join(gdir, MODS_DIR)),
            "inactive": _scan(os.path.join(gdir, MODS_SAVES_DIR)),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/mods/activate", methods=["POST"])
def api_mods_activate():
    """Move a mod from mods-saves/ into mods/."""
    target = _session_target()
    if _is_running(target):
        return jsonify({"ok": False, "error": "Server must be stopped before changing mods"}), 409
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()
    if not _validate_mod_filename(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503
    return _do_mod_move(
        os.path.join(gdir, MODS_SAVES_DIR),
        os.path.join(gdir, MODS_DIR),
        filename,
    )


@app.route("/api/mods/deactivate", methods=["POST"])
def api_mods_deactivate():
    """Move a mod from mods/ into mods-saves/."""
    target = _session_target()
    if _is_running(target):
        return jsonify({"ok": False, "error": "Server must be stopped before changing mods"}), 409
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()
    if not _validate_mod_filename(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503
    return _do_mod_move(
        os.path.join(gdir, MODS_DIR),
        os.path.join(gdir, MODS_SAVES_DIR),
        filename,
    )


@app.route("/api/mods/delete", methods=["POST"])
def api_mods_delete():
    """Delete a mod from 'active', 'inactive', or 'both' locations."""
    target   = _session_target()
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()
    location = str(data.get("location", "")).strip()

    if not _validate_mod_filename(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    if location not in ("active", "inactive", "both"):
        return jsonify({"ok": False, "error": "Invalid location"}), 400

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    file_targets = []
    if location in ("active", "both"):
        file_targets.append(os.path.join(gdir, MODS_DIR, filename))
    if location in ("inactive", "both"):
        file_targets.append(os.path.join(gdir, MODS_SAVES_DIR, filename))

    errors  = []
    deleted = 0
    for path in file_targets:
        if os.path.isfile(path):
            try:
                os.remove(path)
                deleted += 1
            except Exception as e:
                errors.append(str(e))

    if errors:
        return jsonify({"ok": False, "error": "; ".join(errors), "deleted": deleted}), 500
    return jsonify({"ok": True, "deleted": deleted})


def _get_ram_stats() -> dict | None:
    """Return {total, used, available} in bytes without psutil."""
    # Linux: /proc/meminfo
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, val = line.partition(":")
                parts = val.strip().split()
                if parts:
                    info[key.strip()] = int(parts[0]) * 1024  # kB → bytes
        total = info["MemTotal"]
        available = info.get(
            "MemAvailable",
            info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0),
        )
        return {"total": total, "used": total - available, "available": available}
    except (FileNotFoundError, ValueError, KeyError):
        pass

    # macOS fallback
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page
        vm = subprocess.check_output(["vm_stat"], text=True)
        pages = {}
        for line in vm.splitlines():
            if ":" in line and not line.startswith("Mach"):
                k, v = line.split(":", 1)
                pages[k.strip()] = int(v.strip().rstrip("."))
        available = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)) * page
        return {"total": total, "used": total - available, "available": available}
    except Exception:
        return None


@app.route("/api/system/stats")
def api_system_stats():
    """Host-level CPU, RAM, and disk stats (no session param needed)."""
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    cores = os.cpu_count() or 1

    ram = _get_ram_stats()

    try:
        du = shutil.disk_usage("/")
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = None

    return jsonify({
        "ok": True,
        "cpu":  {"load_1m": round(load1, 2), "load_5m": round(load5, 2),
                 "load_15m": round(load15, 2), "cores": cores},
        "ram":  ram,
        "disk": disk,
    })


@app.route("/api/console/stream")
def api_console_stream():
    target = _session_target()

    def generate():
        yield "retry: 3000\n\n"
        last = ""
        while True:
            try:
                content = tmux_capture(300, target)
                if content != last:
                    yield f"data: {json.dumps({'content': content})}\n\n"
                    last = content
                else:
                    yield ": heartbeat\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VibePanel — Minecraft web frontend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--session", action="append", dest="sessions", default=None,
                        help="tmux target (session, session:window, or session:window.pane); "
                             "repeat for multiple servers: --session mc1 --session mc2")
    parser.add_argument("--jars-dir", default=None,
                        help="path to server-jars directory (default: ./server-jars)")
    parser.add_argument("--server-dir", default=None,
                        help="working directory to cd into before starting the server")
    parser.add_argument("--worlds-dir", default=None,
                        help="path to world-saves directory (default: ./world-saves)")
    parser.add_argument("--mods-dir", default=None,
                        help="path to active mods directory (default: ./mods)")
    parser.add_argument("--mods-saves-dir", default=None,
                        help="path to inactive mods directory (default: ./mods-saves)")
    args = parser.parse_args()

    if args.jars_dir:
        JARS_DIR = args.jars_dir
    if args.server_dir:
        SERVER_DIR = _abs_dir(args.server_dir)
    if args.worlds_dir:
        WORLDS_DIR = args.worlds_dir
    if args.mods_dir:
        MODS_DIR = args.mods_dir
    if args.mods_saves_dir:
        MODS_SAVES_DIR = args.mods_saves_dir

    raw_sessions = args.sessions or [TMUX_TARGET]
    if len(raw_sessions) == 1:
        SESSIONS = [_resolve_tmux_target(raw_sessions[0])]
    else:
        SESSIONS = list(raw_sessions)
    TMUX_TARGET = SESSIONS[0]

    PUBLIC_IP = _fetch_public_ip()
    if PUBLIC_IP:
        print(f"Public IP: {PUBLIC_IP}")

    if SERVER_DIR:
        # Say which directory we resolved to, and warn now rather than leaving a
        # failed `cd` to surface as a mysteriously-not-starting server later.
        print(f"Server dir: {SERVER_DIR}"
              + ("" if os.path.isdir(SERVER_DIR) else "  (does not exist yet)"))

    session_display = ', '.join(SESSIONS)
    print(f"VibePanel starting on http://{args.host}:{args.port}  "
          f"(sessions: {session_display}, jars: {JARS_DIR})")
    app.run(host=args.host, port=args.port, threaded=True)
