#!/usr/bin/env python3
import os
import re
import glob
import time
import json
import argparse
import ipaddress
import shlex
import shutil
import struct
import subprocess
import threading
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
    """Read the pane's contents, with lines the pane wrapped put back together.

    A pane is whatever width tmux gave it — 80 columns for a session created
    detached, and nothing about the panel changes that: we never attach, so we
    are never a client and never contribute to sizing. `capture-pane` returns
    *display* lines, so without -J everything long comes back sliced at that
    width. That is not just cosmetic for the console view: `list` answers on one
    line naming every player online, and at 80 columns a 10-player server parsed
    as the single name "Not" while the header still said 10 (verified against
    tmux 3.4). -J also preserves trailing spaces, so put those back the way -p
    had them.
    """
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-J", "-t", target or TMUX_TARGET,
         "-S", f"-{lines}"],
        capture_output=True, text=True, check=True,
    )
    return "\n".join(line.rstrip() for line in clean(result.stdout).split("\n"))


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


_PRIV_WRAPPERS = ("su", "sudo", "doas")


def _wrapper_runs_a_command(argv: list) -> bool:
    """True if this su/sudo/doas was handed a command, not asked for a shell.

    An admin sitting in `su - minecraft` is not a running server, and counting
    one would leave the panel stuck on Running with no way to start anything.
    A wrapper given a command to run is the case we want.
    """
    base, rest = os.path.basename(argv[0]), argv[1:]
    if base == "su":
        # su runs a command only when told to: `su mc -c '…'`. Anything else
        # (`su - mc`) is an interactive shell.
        return any(t in ("-c", "--command") or t.startswith("--command=") for t in rest)
    # sudo/doas take the command as trailing words; -i/-s ask for a shell instead.
    if any(t in ("-i", "-s", "--login", "--shell") for t in rest):
        return False
    return any(not t.startswith("-") for t in rest)


def _proc_argv(pid: int) -> list | None:
    """A process's real argv from /proc, or None where that can't be read.

    Preferred over `ps -o args=`, which hands back one space-joined string: a
    jar path containing a space is indistinguishable from two arguments there,
    and a long java command line can be truncated. Kernel threads have an empty
    cmdline, which reads as None here — nothing we care about looks like that.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = [a for a in fh.read().decode("utf-8", "replace").split("\0") if a]
    except (OSError, ValueError):
        return None
    return argv or None


def _proc_ppid(pid: int) -> int | None:
    """A process's parent from /proc/<pid>/stat.

    The comm field is in parentheses and may itself contain spaces and
    parentheses, so the line is split *after* the last ')' — the usual trap
    with this file. ppid is then the second field of what remains.
    """
    try:
        with open(f"/proc/{pid}/stat") as fh:
            after = fh.read().rpartition(")")[2].split()
        return int(after[1])
    except (OSError, IndexError, ValueError):
        return None


def _proc_descends_from(pid: int, ancestor: int) -> bool:
    """Walk pids up from `pid` looking for `ancestor`. Bounded, never loops."""
    for _ in range(32):
        pid = _proc_ppid(pid)
        if pid is None or pid <= 1:
            return False
        if pid == ancestor:
            return True
    return False


def _java_under(wrapper_pid: int) -> dict | None:
    """Find the java a privilege wrapper started: {"pid": int, "argv": list}.

    The wrapper severs the tty (see _pane_java_info), so `ps -t` cannot reach
    the JVM and all the pane can show us is the wrapper's own arguments. Those
    describe the server only when the admin spelled the whole java line out at
    the prompt; `sudo -u mc ./start.sh` says nothing about jar or heap. So walk
    /proc instead and read the command line off the process itself.

    /proc/<pid>/cmdline is world-readable, so this works for another user's JVM
    from an unprivileged panel — but `hidepid=2` hides the entry entirely and
    macOS has no /proc at all, hence None being an ordinary answer rather than
    an error. A java that answers here is still only *evidence*: its cwd stays
    unreadable without privilege, which is why the caller marks it `wrapped`.
    """
    try:
        pids = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return None

    fallback = None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/comm") as fh:
                if fh.read().strip() != "java":
                    continue
        except OSError:
            continue        # gone between listdir and open, or hidden from us
        if not _proc_descends_from(pid, wrapper_pid):
            continue
        argv = _proc_argv(pid)
        if not argv:
            continue
        # A start script may run helper JVMs of its own; the one that names a
        # jar is the server. Anything else is only used if nothing better turns
        # up, so we still report a pid for the heap card.
        if _java_jar(argv):
            return {"pid": pid, "argv": argv}
        if fallback is None:
            fallback = {"pid": pid, "argv": argv}
    return fallback


def _java_jar(argv: list) -> str | None:
    """The jar a java command line runs, as a bare filename."""
    for i, tok in enumerate(argv[:-1]):
        if tok == "-jar" and argv[i + 1].endswith(".jar"):
            return os.path.basename(argv[i + 1])
    return None


def _java_mem(argv: list) -> str | None:
    """The -Xmx a java command line carries, in the form the panel writes.

    Only the shapes the start form can express are returned: `-Xmx4096m`
    becomes "4096M", while `-Xmx2048k` and a bare byte count become nothing at
    all. Whatever comes back here is remembered as last_mem and put straight
    into the memory field, so a figure _start_server() would then refuse is
    worse than no figure — the admin gets a box that looks filled in and a
    Start that says "Invalid memory value".
    """
    for tok in argv:
        m = re.fullmatch(r"-Xmx(\d+)([MmGg])", tok)
        if m:
            return f"{m.group(1)}{m.group(2).upper()}"
    return None


def _pane_java_info(target: str = None) -> dict:
    """Scan the pane's tty for a java process.

    Returns {"running", "jar", "mem", "pid", "wrapped"}.

    Uses #{pane_tty} + ps -t so it finds java regardless of how it was started:
    typed directly, via exec, or as a grandchild of a wrapper script
    (e.g. `bash start.sh` → java).  The tty is inherited by all descendants
    of the pane's shell, so process-tree depth doesn't matter.

    That last part is exactly what a privilege wrapper breaks. Measured with
    util-linux 2.41 and sudo 1.9, java started from a pane on pts/0 ends up:
    `su mc -c 'java …'` → no controlling terminal at all (su calls setsid);
    `su --pty mc -c …` → pts/2; `sudo -u mc java …` → pts/1. None of them are
    on our tty, and hidepid would hide another user's processes anyway, so all
    we get to see is the wrapper itself.

    So a wrapper that was handed a command is taken at its word and counted as
    the server — crude, but the alternative is reporting Stopped for a server
    that is plainly up. Where it goes further than taking its word is in *what*
    is running: `_java_under()` walks /proc for the JVM the wrapper started and
    reads jar and heap off that process, because the wrapper's own arguments
    only describe the server when the admin typed the whole java line at the
    prompt — `sudo -u mc ./start.sh` describes nothing. Its arguments remain the
    fallback for when /proc can't answer.

    `pid` is the java process itself, and stays None when the wrapper hid it
    from us — a wrapper's own pid is no use to anything wanting to talk to a JVM.

    `wrapped` says the evidence came through su/sudo/doas. It matters to the
    caller even when we did find the JVM: /proc/<pid>/cmdline is world-readable
    but /proc/<pid>/cwd is not, so a wrapped pid may be one we can name the jar
    of and still not locate the game dir of. See _observe_session().

    Raises subprocess.CalledProcessError if the tmux target is unreachable.
    """
    def stopped() -> dict:
        return {"running": False, "jar": None, "mem": None,
                "pid": None, "wrapped": False}

    t = target or TMUX_TARGET
    pane_tty = subprocess.run(
        ["tmux", "display-message", "-t", t, "-p", "#{pane_tty}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not pane_tty:
        return stopped()

    tty = pane_tty.removeprefix("/dev/")
    ps = subprocess.run(
        ["ps", "-t", tty, "-o", "pid=,args="],
        capture_output=True, text=True,
    )

    wrapper = None
    for line in ps.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        args = parts[1].strip()
        argv = args.split()
        if not argv:
            continue
        base = os.path.basename(argv[0])
        if base == "java":
            pid = int(parts[0]) if parts[0].isdigit() else None
            # ps joins argv with spaces, so a path containing one is already
            # ambiguous by the time we see it. Ask /proc for the real argv when
            # we can, and keep the split string for when we can't.
            real = _proc_argv(pid) if pid else None
            argv = real or argv
            return {"running": True, "jar": _java_jar(argv),
                    "mem": _java_mem(argv), "pid": pid, "wrapped": False}
        # Remember it, but keep looking: seeing java itself is always better.
        if wrapper is None and base in _PRIV_WRAPPERS and _wrapper_runs_a_command(argv):
            wrapper = (int(parts[0]) if parts[0].isdigit() else None, argv)

    if wrapper:
        wpid, wargv = wrapper
        # The JVM itself if /proc will show it to us, the wrapper's own command
        # line if not — `su mc -c 'java … -jar server.jar'` still says plenty.
        found = _java_under(wpid) if wpid else None
        argv  = found["argv"] if found else wargv
        return {"running": True, "jar": _java_jar(argv), "mem": _java_mem(argv),
                "pid": found["pid"] if found else None, "wrapped": True}
    return stopped()


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


# ── The panel store ──────────────────────────────────────────────────────────
#
# Three words, used consistently from here on: a *server* is one game instance,
# a *session* is the tmux session it lives in, and the *panel* is this process.
#
# There are two state files, split by who owns the facts:
#
#   this one, in the panel's own working directory
#       which sessions we expect, where each one's game dir is, and whether it
#       should be started when the panel starts. Panel configuration — and a
#       session's directory could not live in the game dir anyway, since you
#       need it to find the game dir in the first place.
#
#   .vibepanel.json, in each game dir (STATE_FILE above)
#       last_jar / last_mem. Facts about that particular game, which travel
#       with it if the directory is moved.
#
# Held in memory and flushed only when something actually changes: a status
# poll observing nothing new must not rewrite the file every five seconds.

# Resolved against the CWD at import — that is the panel's working directory
# (systemd's WorkingDirectory=), and pinning it now means nothing later in this
# process can move the store by chdir-ing. --state-file replaces it at startup.
PANEL_STATE_FILE = os.path.abspath(
    os.path.expanduser(os.environ.get("STATE_FILE", "vibepanel-state.json")))

_PANEL_STATE = {"sessions": {}}
_PANEL_LOCK = threading.Lock()
_PANEL_STATE_WRITABLE = True


def _load_panel_state() -> None:
    """Read the store into memory. A missing or corrupt file starts empty."""
    global _PANEL_STATE
    try:
        with open(PANEL_STATE_FILE) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    sessions = data.get("sessions")
    sessions = sessions if isinstance(sessions, dict) else {}
    # Each entry has to be a dict too, not just the map holding them:
    # _session_state() copies one with dict(), which raises on anything else, so
    # a single hand-mangled entry would otherwise take out every read of the
    # store rather than just its own session.
    _PANEL_STATE = {"sessions": {k: v for k, v in sessions.items()
                                 if isinstance(v, dict)}}

    # autostart (a bool) became start_policy (one of three). Migrate in memory and
    # pop the old key rather than leaving both: this file is the quickest way to
    # see what the panel believes, and two keys that can disagree destroy that.
    # No write is needed here — _set_expected_sessions() runs moments later and
    # flushes unconditionally, so the new shape lands on disk for free.
    for entry in _PANEL_STATE["sessions"].values():
        if "autostart" in entry:
            entry.setdefault("start_policy",
                             "always" if entry.pop("autostart") else "never")


def _write_panel_state_locked() -> None:
    """Persist the store. Caller holds _PANEL_LOCK."""
    global _PANEL_STATE_WRITABLE
    path = PANEL_STATE_FILE
    tmp  = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(_PANEL_STATE, fh, indent=2)
        os.replace(tmp, path)
        _PANEL_STATE_WRITABLE = True
    except OSError as e:
        # Say it once, then carry on from memory. A panel that cannot persist
        # is still a working panel; one that refuses to start is not.
        if _PANEL_STATE_WRITABLE:
            _PANEL_STATE_WRITABLE = False
            print(f"cannot write {path}: {e}  (continuing without persistence)")


def _session_state(session: str) -> dict:
    """Everything the store knows about one session (a copy; never the live dict)."""
    with _PANEL_LOCK:
        return dict(_PANEL_STATE["sessions"].get(session, {}))


def _confirmed_dir(session: str) -> str | None:
    """This session's game dir, but only if it was read off a running server.

    For callers that are about to *write* into the directory: an unconfirmed dir
    is a pane's CWD, which is a fine guess to show and a poor place to leave
    files.
    """
    st = _session_state(session)
    return st.get("dir") if st.get("dir_confirmed") else None


# What the panel does with a server when the panel process starts.
#
#   never           nothing, ever. The default, and what an unrecognised value
#                   falls back to.
#   always          start it, whatever happened last time.
#   unless-stopped  start it unless the last run was asked to stop — see
#                   _last_run_ending(), which reads that off the game's own log.
_START_POLICIES = ("never", "always", "unless-stopped")


def _start_policy(session: str) -> str:
    """This session's start policy, defaulting to "never".

    Whitelisted on the way out, like _last_mode(): a store that has been hand
    edited, or written by a newer panel, must never read as something *stronger*
    than the admin asked for. Unknown means the panel leaves the server alone.
    """
    policy = _session_state(session).get("start_policy")
    return policy if policy in _START_POLICIES else "never"


def _update_session_state(session: str, **fields) -> bool:
    """Fold fields into a session's entry, writing only if something changed.

    Returns whether anything changed, so callers can log an actual transition
    rather than every poll that re-observed the same thing.
    """
    with _PANEL_LOCK:
        entry = _PANEL_STATE["sessions"].setdefault(session, {})
        if all(entry.get(k) == v for k, v in fields.items()):
            return False
        entry.update(fields)
        _write_panel_state_locked()
        return True


def _set_expected_sessions(names: list) -> None:
    """Record the session set, dropping entries for sessions no longer expected.

    Passing --session *declares* the set rather than adding to it, which is what
    gives an admin a way to forget one: stop passing it. Everything we knew about
    a dropped session goes with it — keeping a stale directory around to be
    silently re-adopted later is worse than re-learning it.
    """
    with _PANEL_LOCK:
        sessions = _PANEL_STATE["sessions"]
        for gone in [s for s in sessions if s not in names]:
            del sessions[gone]
        for name in names:
            sessions.setdefault(name, {})
        _write_panel_state_locked()


def _stored_sessions() -> list:
    with _PANEL_LOCK:
        return list(_PANEL_STATE["sessions"])


def _remember_run(gdir: str, session: str, **fields) -> None:
    """Persist how a session last ran (jar, memory), keyed by session name.

    Best-effort: a game dir we cannot write to must not stop a server starting.
    """
    fields = {k: v for k, v in fields.items() if v}
    if not fields:
        return
    try:
        state = _load_state(gdir)
        for key, value in fields.items():
            state.setdefault(key, {})[session] = value
        path = os.path.join(gdir, STATE_FILE)
        tmp  = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _last_jar(gdir: str, session: str) -> str | None:
    return _load_state(gdir).get("last_jar", {}).get(session)


def _last_mem(gdir: str, session: str) -> str | None:
    return _load_state(gdir).get("last_mem", {}).get(session)


# The two ways a server can be started, and they are exclusive: a jar with a
# memory figure the panel supplies, or the game's own start script, which owns
# its flags and leaves the panel with nothing to say about memory.
_START_MODES = ("jar", "script")


def _last_script(gdir: str, session: str) -> str | None:
    return _load_state(gdir).get("last_script", {}).get(session)


def _last_mode(gdir: str, session: str) -> str:
    """Which of the two start forms this server last used: "jar" or "script".

    Defaults to "jar" — that is what every state file written before custom
    scripts existed means, and it is the mode the panel had until now.
    """
    mode = _load_state(gdir).get("last_mode", {}).get(session)
    return mode if mode in _START_MODES else "jar"


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


# Sessions whose creation failed, so a tmux that cannot start at all doesn't
# reprint the same error on every status poll. Cleared as soon as one exists.
_AUTOSTART_FAILED = set()


def _session_dir(target: str) -> str:
    """Where a session's shell belongs, or '' if we have no idea.

    The store's learned directory first — it is per session and, once confirmed,
    was read off a server that was actually running there. SERVER_DIR is only a
    fallback for a session we have never seen: it is a single global covering
    every session, which is exactly the assumption the store exists to replace.
    """
    return _session_state(target.split(":")[0]).get("dir") or SERVER_DIR


def _shell_is_ready(target: str) -> bool:
    """Is the pane's shell up and reading, rather than mid-fork?

    `new-session -d` returns as soon as tmux has the pane, which is before the
    child has chdir'd and exec'd. A shell that owns its terminal is its own
    foreground process group, so tpgid == pane_pid is exactly the "the shell is
    there and idle" signal — the same /proc field tmux_pane_path() reads, used
    for the thing it is genuinely good for. Anything unexpected reads as ready:
    this gates a short wait, and waiting forever is worse than typing early.
    """
    try:
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-t", target, "-p", "#{pane_pid}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        with open(f"/proc/{pane_pid}/stat") as fh:
            stat = fh.read()
        return stat[stat.rindex(')') + 2:].split()[5] == pane_pid
    except FileNotFoundError:
        return True             # no /proc (macOS): nothing to wait on
    except Exception:
        return True


def _wait_for_shell(target: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not _shell_is_ready(target) and time.monotonic() < deadline:
        time.sleep(0.05)


def _ensure_session(target: str) -> str:
    """Create a missing tmux session in its game directory.

    Returns "exists" (it was already there), "created", or "failed" — not a
    bool, because the difference between the first two decides how the caller
    may resolve the game dir. On a session we just created it is the directory
    we passed to -c, *by construction*; asking tmux_pane_path() to tell us back
    would be both a round trip to rediscover what we just asserted and a race,
    since new-session returns before the child has chdir'd and exec'd, and
    /proc/<tpgid>/cwd inside that window is the *tmux server's* CWD. That is
    not an error, so nothing raises — it is a plausible wrong path, which is
    the worst kind.

    A session is only ever created when we know where it belongs and that
    directory exists. Without that we would be guessing, and a session opened
    in the wrong place is worse than no session at all: every path the panel
    resolves comes from the pane's CWD, so the panel would look healthy while
    listing someone else's directory.

    What gets created is a plain shell, never a server. `new-session -c` puts
    that shell in the right directory to begin with, so the pane reports the
    game dir immediately and Start has somewhere to type.
    """
    session = target.split(":")[0]
    try:
        if subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0:
            _AUTOSTART_FAILED.discard(session)
            return "exists"

        gdir = _session_dir(target)
        if not gdir or not os.path.isdir(gdir):
            return "failed"

        r = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", gdir],
            capture_output=True, text=True,
        )
    except OSError as e:            # no tmux on PATH
        err = str(e)
    else:
        if r.returncode == 0:
            _AUTOSTART_FAILED.discard(session)
            print(f"created tmux session '{session}' in {gdir}")
            _wait_for_shell(target)
            return "created"
        err = r.stderr.strip() or f"tmux exited {r.returncode}"

    if session not in _AUTOSTART_FAILED:
        _AUTOSTART_FAILED.add(session)
        print(f"could not create tmux session '{session}': {err}")
    return "failed"


def _game_dir(target: str, ensured: str = None) -> str:
    """The game directory for a session.

    Pass `ensured` — the _ensure_session() result — when a session may have just
    been created: that case answers from the directory we created it with rather
    than probing a pane whose shell may not have exec'd yet. Otherwise this is
    the ordinary rule, the CWD of the pane's foreground process, which follows an
    admin who has cd'd somewhere and is what every other game-dir read uses.
    """
    if ensured == "created":
        return _session_dir(target)
    return tmux_pane_path(target)


# Last running state we saw per session, so _observe_session() can spot the
# stopped → running edge. Empty at startup, which makes the first sighting of a
# running server an edge — exactly when we most want to learn where it lives.
_LAST_RUNNING = {}


def _running_game_dir(target: str, pid: int, allow_pane: bool = True) -> str | None:
    """The CWD of a running server: the one directory we can be sure of.

    /proc/<pid>/cwd is the java process itself, which beats the pane's
    foreground process for the `bash start.sh` case — there the foreground group
    leader is the wrapper script, not the server. Falls back to the pane when
    that read is not available: another user's process under hidepid, or no /proc
    at all.

    `allow_pane=False` withdraws that fallback, and the su/sudo case needs it:
    there the pane's foreground process is the privilege wrapper, whose CWD is
    wherever the admin's shell happened to be. Locating the JVM through /proc
    does not change that — cwd is the one thing about another user's process we
    still cannot read — so a wrapped server either tells us its own directory or
    tells us nothing.
    """
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    if not allow_pane:
        return None
    try:
        return tmux_pane_path(target) or None
    except Exception:
        return None


# ── Backing up the world when a server stops ─────────────────────────────────
#
# A per-server standing policy, like the start policy and stored beside it: ticked
# means every running → stopped transition archives the world, whoever caused
# the stop. There is deliberately only *one* trigger — the running → stopped
# edge in _observe_session() — rather than one in /api/server/stop and another
# for stops we merely noticed. The Stop button only asks the console to stop;
# what it produces a moment later is that same edge, so hanging the backup off
# the edge covers the button, a `stop` typed at the pane, a crash and a kill
# with one piece of code, and covers each of them at the only moment the world
# is actually at rest. Backing up from the endpoint would tar a world the
# server was still writing.
#
# The edge is seen when status is polled, which every open page does every 5 s.
# With no page open nothing is observed at all, so a server that stops overnight
# is backed up when someone next opens the panel — late, but from a world that
# has been sitting still since, so the archive is the same one. What is lost is
# only the timestamp's meaning, and _STOP_BACKUPS records the real one.

# What the last stop-backup did, per session, for the Server page to show:
#   {session: {"state": "running"|"done"|"failed"|"skipped", "filename": …,
#              "size": …, "error": …, "at": "HH:MM:SS"}}
# In memory only: it describes this panel's run, and a stale line read back
# after a restart would claim a backup for a stop nobody here saw.
_STOP_BACKUPS     = {}
_STOP_BACKUP_LOCK = threading.Lock()


def _archive_world(gdir: str, suffix: str = None) -> tuple:
    """Tar <gdir>/world into WORLDS_DIR as world-<ts>[-<suffix>].tgz.

    The one place a world archive is written — the Worlds page's Save, the
    autosave before a Load, and the on-stop backup all come through here, so
    all three produce the same shape of file in the same place.

    Tarred to a hidden `.tmp` and only then linked into place, the same
    discipline as the state files: a tar that dies half way — the panel killed
    mid-backup, a full disk — must not leave a truncated archive sitting in the
    list looking loadable, because Load deletes the current world before
    extracting one.

    The name is claimed with os.link() rather than os.replace() because these
    names collide: they are second-resolution, and an automatic backup can land
    in the same second as a hand-pressed Save or the autosave before a Load.
    replace() would silently destroy the older archive — the one failure this
    whole feature exists to prevent — so instead the link fails, and we take the
    next free second. Nothing is ever overwritten.

    Raises FileNotFoundError when there is no world to archive, and
    CalledProcessError when tar fails. Returns (filename, size).
    """
    world_path = os.path.join(gdir, "world")
    if not os.path.isdir(world_path):
        raise FileNotFoundError("No 'world' directory found")

    saves_path = os.path.join(gdir, WORLDS_DIR)
    os.makedirs(saves_path, exist_ok=True)

    stamp    = datetime.now()
    def named(when):
        ts = when.strftime("%Y%m%d-%H%M%S")
        return f"world-{ts}-{suffix}.tgz" if suffix else f"world-{ts}.tgz"

    # Hidden, and made unique by writer rather than by name: the whole reason
    # for the loop below is that two archivings can pick the same name in the
    # same second, and those two must not then also share a scratch file and tar
    # over each other. The server is stopped while an on-stop backup runs, so
    # the Worlds page's Save is live at the time and this overlap is reachable.
    tmp_path = os.path.join(
        saves_path, f".world-{os.getpid()}-{threading.get_ident()}.tgz.tmp")
    try:
        subprocess.run(
            ["tar", "-czf", tmp_path, "-C", gdir, "world"],
            check=True, capture_output=True, text=True,
        )
        for _ in range(60):
            filename = named(stamp)
            out_path = os.path.join(saves_path, filename)
            try:
                os.link(tmp_path, out_path)
                break
            except FileExistsError:
                stamp += timedelta(seconds=1)
            except OSError:
                # A filesystem without hard links. Fall back to the rename, and
                # keep the no-clobber promise as best a check can.
                if os.path.exists(out_path):
                    stamp += timedelta(seconds=1)
                    continue
                os.replace(tmp_path, out_path)
                break
        else:
            raise FileExistsError(f"no free name for {named(stamp)}")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return filename, os.path.getsize(out_path)


def _stop_backup_state(session: str) -> dict:
    with _STOP_BACKUP_LOCK:
        st = _STOP_BACKUPS.get(session)
        return dict(st) if st else None


def _stop_backup_running(session: str) -> bool:
    """Whether a stop-backup is tarring this session's world right now.

    Callers that would disturb the directory being read — Start, and the Worlds
    page's Load — check this and refuse for the moment. A tar of a world the
    server has begun writing again is a corrupt archive of a fine world, which
    is worse than the archive simply not existing.
    """
    st = _stop_backup_state(session)
    return bool(st and st.get("state") == "running")


def _run_stop_backup(session: str, gdir: str) -> None:
    """Tar the world, then record what happened. Runs on its own thread.

    A world is gigabytes and tar takes as long as it takes; this is reached from
    /api/server/status, which every page polls, so it cannot be done inline.
    Every outcome is printed and kept, for the same reason the start policy
    prints:
    somebody who finds an archive they did not ask for should be able to see in
    the panel's log that the panel made it.
    """
    try:
        filename, size = _archive_world(gdir, "autosave")
        with _STOP_BACKUP_LOCK:
            _STOP_BACKUPS[session] = {
                "state": "done", "filename": filename, "size": size,
                "at": datetime.now().strftime("%H:%M:%S"),
            }
        print(f"stop-backup: '{session}' wrote {filename} ({size} bytes) in {gdir}")
    except Exception as e:
        error = getattr(e, "stderr", None) or str(e)
        with _STOP_BACKUP_LOCK:
            _STOP_BACKUPS[session] = {
                "state": "failed", "error": error,
                "at": datetime.now().strftime("%H:%M:%S"),
            }
        print(f"stop-backup: '{session}' failed — {error}")


def _stop_backup_pass(session: str) -> None:
    """Start a backup for a session that has just stopped, if it asked for one.

    The directory must be a *confirmed* one: this writes files, and an
    unconfirmed dir is a pane's CWD, which is a fine guess to show and a poor
    place to leave a gigabyte. In practice the server was running a moment ago,
    which is exactly what confirms it — the case that survives is the su/sudo
    server whose cwd we never got to read, and there we say so rather than
    archiving some other directory's world.
    """
    if not _session_state(session).get("backup_on_stop"):
        return

    gdir = _confirmed_dir(session)
    if not gdir:
        with _STOP_BACKUP_LOCK:
            _STOP_BACKUPS[session] = {
                "state": "skipped",
                "error": "the panel has not confirmed this server's game directory",
                "at": datetime.now().strftime("%H:%M:%S"),
            }
        print(f"stop-backup: '{session}' skipped — game directory not confirmed")
        return

    with _STOP_BACKUP_LOCK:
        if (_STOP_BACKUPS.get(session) or {}).get("state") == "running":
            return
        _STOP_BACKUPS[session] = {"state": "running",
                                  "at": datetime.now().strftime("%H:%M:%S")}
    print(f"stop-backup: '{session}' backing up the world in {gdir}")
    threading.Thread(target=_run_stop_backup, args=(session, gdir),
                     daemon=True).start()


def _stop_backup_plan(session: str) -> dict:
    """The policy and whether it could be carried out, for the Server page.

    Same shape as _start_plan(): report a broken link as itself, so an admin
    who ticked the box and got nothing can see which step is missing rather than
    an empty world-saves directory.
    """
    st   = _session_state(session)
    plan = {"backup_on_stop": bool(st.get("backup_on_stop")),
            "dir": st.get("dir"), "dir_confirmed": bool(st.get("dir_confirmed")),
            "last": _stop_backup_state(session), "problem": None}

    gdir = _confirmed_dir(session)
    if not gdir:
        plan["problem"] = ("the game directory is not confirmed yet — it is, the "
                           "first time the panel sees this server running")
    elif not os.path.isdir(os.path.join(gdir, "world")):
        plan["problem"] = f"there is no 'world' directory in {gdir} yet"
    return plan


def _observe_session(target: str, info: dict, ensured: str = None) -> None:
    """Learn where a session's game directory is, by watching it.

    tmux_pane_path() reports the CWD of the pane's *foreground* process, and how
    much that is worth depends entirely on what is in the foreground:

      server stopped   it is the admin's shell, which may be anywhere. A guess —
                       good enough to use until something better comes along.
      server running   it is the server, so its CWD *is* the game dir. A fact.

    So a directory learned from a running server is marked confirmed and is only
    ever replaced by another confirmed sighting. An admin who cd's out of the
    game dir with the server down is not evidence against something we watched
    java do, and must not be allowed to un-learn it.

    Deliberately not folded into _pane_java_info(), which stays a pure query;
    this is an explicit side effect of *serving status*, the same shape as
    _record_peak().

    Never raises: a panel that 500s because it could not take a note would be a
    poor trade.
    """
    session = target.split(":")[0]
    running = bool(info.get("running"))
    was     = _LAST_RUNNING.get(session)
    _LAST_RUNNING[session] = running

    try:
        if running:
            # Only on the edge: re-reading /proc every poll buys nothing, since a
            # process cannot change its own CWD out from under itself here, nor
            # rewrite the command line it was started with.
            if was is True:
                return
            pid = info.get("pid")
            gdir = None
            if pid is not None:
                # Under a privilege wrapper the pane's foreground process is the
                # wrapper, whose CWD is wherever the admin's shell happened to
                # be — so no pane fallback here. Recording that would poison the
                # store with a confident-looking wrong path.
                gdir = _running_game_dir(target, pid,
                                         allow_pane=not info.get("wrapped"))
            if gdir and _update_session_state(session, dir=gdir, dir_confirmed=True):
                print(f"session '{session}': game dir is {gdir} (seen running)")

            # A server that came up outside the panel — started by hand at the
            # pane, or by something else entirely — is the only account of what
            # it was started with, and this edge is the moment to take it down:
            # the jar and heap now on screen are the ones actually running, and
            # they are what the start form offers once it stops. A dir we merely
            # guessed is not written to; there is no sense recording a game's
            # habits into a directory that may not be that game.
            gdir = gdir or _confirmed_dir(session)
            if gdir:
                _remember_run(gdir, session,
                              last_jar=info.get("jar"), last_mem=info.get("mem"))
            return

        # Stopped. The edge into it is the one moment the world is both complete
        # and no longer being written, so it is where the backup goes — and it
        # is the same edge whether the Stop button caused it, someone typed
        # `stop` at the pane, or the JVM died on its own. Before the dir checks
        # below, which are about *learning* a directory; this one needs an
        # already-confirmed dir and takes no guesses.
        if was is True:
            _stop_backup_pass(session)

        # Track the pane while the directory is still only a guess.
        if _session_state(session).get("dir_confirmed"):
            return
        gdir = _game_dir(target, ensured)
        if gdir:
            _update_session_state(session, dir=gdir, dir_confirmed=False)
    except Exception:
        pass


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

# Geyser (Bedrock clients on a Java server) if the admin has installed it.
GEYSER_CONFIG = os.path.join("config", "Geyser-Fabric", "config.yml")
_YAML_KEY_RE  = re.compile(r'^(\s*)([A-Za-z0-9_.-]+):\s*(.*?)\s*$')

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


def _read_bedrock_port(gdir: str) -> int:
    """Return Geyser's bedrock.port, or None when Geyser isn't installed.

    Scanned by hand rather than with a YAML parser: Flask is this panel's only
    dependency and one integer under one known key is not worth a second one.
    The scan is scoped to the top-level `bedrock:` block, because Geyser's
    config carries a `remote:` block with its own `port:` — the Java server it
    forwards to — and that is not the port a Bedrock player types in.
    """
    path       = os.path.join(gdir, GEYSER_CONFIG)
    in_bedrock = False
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _YAML_KEY_RE.match(line.split("#", 1)[0].rstrip())
                if not m:
                    continue
                indent, key, val = m.groups()
                if not indent:                      # a top-level key: enters or
                    in_bedrock = key == "bedrock"   # ends the block we want
                    continue
                if in_bedrock and key == "port":
                    try:
                        port = int(val.strip("'\""))
                    except ValueError:
                        return None
                    return port if 1 <= port <= 65535 else None
    except OSError:
        pass
    return None


def _read_log_tail(gdir: str) -> tuple:
    """The last LOG_SCAN_MAX_BYTES of logs/latest.log, and the file's mtime.

    Raises OSError rather than swallowing it: _last_run_ending() needs to tell
    "there is no log here" from "there is one and we could not read it", and
    those are different answers.
    """
    path = os.path.join(gdir, LOGS_DIR, LATEST_LOG)
    mtime = os.path.getmtime(path)
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size > LOG_SCAN_MAX_BYTES:
            fh.seek(size - LOG_SCAN_MAX_BYTES)
        return fh.read().decode("utf-8", "replace"), mtime


def _scan_latest_log(gdir: str) -> list:
    """Scrape name→UUID pairs from the tail of logs/latest.log.

    Reads at most LOG_SCAN_MAX_BYTES from the end of that one file — no rotated
    archives — so the result is one contiguous slice of history.
    """
    try:
        blob, mtime = _read_log_tail(gdir)
    except OSError:
        return []

    # Log lines carry only [HH:MM:SS], so dates are reconstructed by counting
    # backwards clock jumps as midnight rollovers and dating each hit back from
    # the file's mtime, which is the date of its last line. Note the file is
    # rotated both at server start *and* at midnight (log4j2.xml carries
    # OnStartupTriggeringPolicy and a TimeBasedTriggeringPolicy on a yyyy-MM-dd
    # pattern), so on a stock config there is rarely a rollover to count — but a
    # config without the time-based trigger can still produce one.
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

    This is also where a missing session gets put back. Every page is 503 until
    one exists, and this endpoint is what each of them polls, so it is the first
    thing to notice — on startup before anyone has made the session, and later if
    something kills it. The recreate hangs off the failure path rather than a
    has-session check up front, so a session that is present costs nothing extra.

    It is likewise where the panel learns each session's game directory: every
    page polls it, for every session, so it sees the stopped → running edge that
    _observe_session() needs.
    """
    target  = _session_target()
    session = target.split(":")[0]
    # A stop-backup is started by the observation just above and then runs for
    # minutes on its own thread, so its state rides along on the poll that every
    # page is already making rather than needing one of its own.
    try:
        info = _pane_java_info(target)
        _observe_session(target, info)
        return jsonify({**info, "ok": True, "stop_backup": _stop_backup_state(session)})
    except subprocess.CalledProcessError:
        ensured = _ensure_session(target)
        if ensured != "failed":
            try:
                info = _pane_java_info(target)
                _observe_session(target, info, ensured)
                return jsonify({**info, "ok": True,
                                "stop_backup": _stop_backup_state(session)})
            except subprocess.CalledProcessError:
                pass    # a target naming a window/pane we did not create
        return jsonify({"running": False, "ok": False,
                        "error": f"tmux target '{target}' not found"})
    except Exception as e:
        return jsonify({"running": False, "ok": False, "error": str(e)})


# ── Heap usage (prototype) ───────────────────────────────────────────────────
#
# Read straight out of the JVM's own performance-counter file rather than by
# asking the JVM anything. Every HotSpot JVM mmaps one at
# /tmp/hsperfdata_<user>/<pid> and keeps it current as a matter of course; it is
# what jstat reads. Measured against an idle JVM, 50 samples:
#
#     jcmd GC.heap_info   3.07 s wall, 4.69 s CPU   (0.01 s of it in the server)
#     reading this file   0.02 s wall, 0.02 s CPU   (0.00 s of it in the server)
#
# jcmd is not expensive because the JVM works hard for it — the diagnostic costs
# the server ~0.2 ms. It is expensive because jcmd is itself a Java program, so
# each sample booted a second JVM just to speak the attach protocol. Reading the
# file costs a 32 KB read, needs no JDK installed, and touches the server not at
# all. There is deliberately no fallback to jcmd: see the note on `used` below.

# Highest `used` seen per session, keyed by pid so a restart starts a fresh
# peak: {session: {"pid": int, "peak": bytes}}. Deliberately in memory only —
# "since the server was last started" is a fact about the current JVM, and this
# is sampled solely by the Overview page's existing refresh, never polled.
HEAP_PEAKS = {}

_PERF_MAGIC = 0xCAFEC0C0

# The counters we want, matched whole so that e.g. the per-age breakdown in
# sun.gc.generation.0.agetable.* can never be mistaken for a space.
_GEN_USED_RE  = re.compile(r"sun\.gc\.generation\.\d+\.space\.\d+\.used")
_GEN_CAP_RE   = re.compile(r"sun\.gc\.generation\.\d+\.capacity")
_GEN_MAX_RE   = re.compile(r"sun\.gc\.generation\.\d+\.maxCapacity")
_GC_INVOC_RE  = re.compile(r"sun\.gc\.collector\.\d+\.invocations")


def _hsperf_path(pid: int) -> str | None:
    """Locate a JVM's performance-counter file, or None if it has none.

    HotSpot hardcodes /tmp for this on Linux — it ignores TMPDIR — so /tmp is
    what matters in deployment; $TMPDIR is checked as well only because macOS
    puts it there and the panel gets developed on one. The directory is named
    for the *JVM's* user rather than ours, so it is globbed, not assumed: that
    also lets a panel running as root read a server running as someone else.
    """
    roots = ["/tmp"]
    tmp = os.environ.get("TMPDIR")
    if tmp:
        roots.append(tmp)
    for root in roots:
        for path in glob.glob(os.path.join(root, "hsperfdata_*", str(pid))):
            return path
    return None


def _read_perf_counters(path: str) -> dict:
    """Parse an hsperfdata file into {counter name: value}.

    The format is a documented binary that has been at version 2.0 since Java 5:
    a 32-byte prologue, then `num_entries` variable-length entries, each holding
    its own name and value at offsets relative to the entry. Only the long
    counters are kept — every figure we want is one, and skipping the strings
    means never having to think about their encoding.

    The file is live and memory-mapped, so a sample can in principle mix values
    written a few microseconds apart. Individual counters are aligned 64-bit
    words and can't tear; a gauge does not care about the rest.
    """
    with open(path, "rb") as fh:
        buf = fh.read()
    if len(buf) < 32 or struct.unpack_from(">I", buf, 0)[0] != _PERF_MAGIC:
        raise ValueError("not an hsperfdata file")
    byte_order, major, _minor, accessible = buf[4:8]
    if major != 2:
        raise ValueError(f"unsupported hsperfdata version {major}")
    if not accessible:
        # Set once the JVM has finished initialising its counters.
        raise ValueError("counters not ready yet")

    e = "<" if byte_order else ">"
    entry_offset, num_entries = struct.unpack_from(e + "ii", buf, 24)

    counters = {}
    off = entry_offset
    for _ in range(num_entries):
        if off + 20 > len(buf):
            break
        length, name_offset, vector_length = struct.unpack_from(e + "iii", buf, off)
        if length <= 0 or off + length > len(buf):
            break
        data_type = buf[off + 12]
        data_offset = struct.unpack_from(e + "i", buf, off + 16)[0]
        if vector_length == 0 and data_type == ord("J"):
            name = buf[off + name_offset:off + length].split(b"\0", 1)[0]
            counters[name.decode("utf-8", "replace")] = \
                struct.unpack_from(e + "q", buf, off + data_offset)[0]
        off += length
    return counters


def _heap_from_counters(c: dict) -> dict | None:
    """Fold the gc counters into {used, committed, reserved, collections} bytes."""
    used      = sum(v for k, v in c.items() if _GEN_USED_RE.fullmatch(k))
    committed = sum(v for k, v in c.items() if _GEN_CAP_RE.fullmatch(k))
    maxes     = [v for k, v in c.items() if _GEN_MAX_RE.fullmatch(k)]
    if not maxes:
        return None

    # No counter states the whole heap's maximum, so it is derived. G1 and ZGC
    # hand every generation the same region pool, so each reports the entire
    # heap as its own max (1024M + 1024M for -Xmx1g); Parallel and Serial split
    # a fixed budget between young and old (341M + 683M), which must be summed.
    reserved = maxes[0] if len(set(maxes)) == 1 else sum(maxes)
    # A heap cannot be committed beyond its reservation. If it reads that way,
    # the generations only *happened* to share a maximum — Parallel with -Xmn at
    # exactly half of -Xmx — and the real budget is the sum after all.
    if committed > reserved:
        reserved = sum(maxes)

    return {"used": used, "committed": committed, "reserved": reserved,
            "collections": sum(v for k, v in c.items() if _GC_INVOC_RE.fullmatch(k))}


def _read_heap(pid: int) -> tuple[dict | None, str | None]:
    """Heap figures for a JVM from its counter file; (heap, error message).

    `used` is the live set as of the last collection, not occupancy at this
    instant: HotSpot refreshes these counters at GC boundaries. That is a real
    difference from what jcmd reports — mid-cycle, a churning Parallel heap held
    a steady 161 MB here while jcmd swung between 281 MB and 501 MB as eden
    filled and emptied — but it is the more useful of the two figures, and the
    only one worth a peak marker. Instantaneous occupancy on a healthy server
    spends much of its time near the maximum by design, so a peak taken from it
    saturates within minutes and stops saying anything.
    """
    path = _hsperf_path(pid)
    if not path:
        return None, ("no counter file for this JVM — started with "
                      "-XX:+PerfDisableSharedMem or -XX:-UsePerfData?")
    try:
        counters = _read_perf_counters(path)
    except PermissionError:
        return None, "counter file is not readable — the server runs as another user"
    except (OSError, ValueError, struct.error) as e:
        return None, f"unreadable counter file: {e}"

    heap = _heap_from_counters(counters)
    if not heap:
        return None, "no heap counters in this JVM's counter file"
    if not heap["collections"]:
        # Before the first collection the live-set counters are all still zero,
        # which would render as a heap using nothing at all.
        return None, "waiting for the first GC to publish a live-set figure"
    return heap, None


@app.route("/api/server/heap")
def api_server_heap():
    """Heap utilisation of the JVM in our pane, from its hsperfdata counters.

    Sampled by the Overview page only, and `peak` is recorded here on each
    sample: "highest seen" means highest at the moments we happened to look.

    Every way of failing answers with `ok: false` and a sentence saying which,
    because there is no second mechanism to fall back to — a server whose
    counters we cannot read simply has no heap bar, and the reason is shown.
    """
    target  = _session_target()
    session = target.split(":")[0]
    try:
        info = _pane_java_info(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    if not info["running"]:
        HEAP_PEAKS.pop(session, None)
        return jsonify({"ok": False, "running": False, "error": "server not running"})

    pid = info.get("pid")
    if not pid:
        # A privilege wrapper took the JVM off our tty, and the /proc walk that
        # normally still finds it came back empty — see _pane_java_info.
        return jsonify({"ok": False, "running": True,
                        "error": "java process not visible (started via su/sudo)"})

    heap, err = _read_heap(pid)
    if not heap:
        return jsonify({"ok": False, "running": True, "pid": pid, "error": err})

    rec = HEAP_PEAKS.get(session)
    if not rec or rec["pid"] != pid:
        rec = HEAP_PEAKS[session] = {"pid": pid, "peak": 0}
    rec["peak"] = max(rec["peak"], heap["used"])

    return jsonify({"ok": True, "running": True, "pid": pid, "peak": rec["peak"], **heap})


def _list_jars(gdir: str) -> list:
    """The .jar files we are willing to launch, read straight off disk.

    Single source of truth for both the list the UI offers and the set
    /api/server/start will accept, so the two can never disagree.
    """
    jars_path = os.path.join(gdir, JARS_DIR)
    try:
        entries = os.listdir(jars_path)
    except OSError:
        return []
    # isfile, not just the suffix: a *directory* named foo.jar would otherwise be
    # offered in the UI and accepted by start, and java would fail on it.
    return sorted(f for f in entries
                  if f.endswith(".jar") and os.path.isfile(os.path.join(jars_path, f)))


@app.route("/api/server/jars")
def api_server_jars():
    """Everything the start form needs: the jars, the start-script suggestions,
    and which of the two this server last used."""
    target = _session_target()
    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503
    try:
        os.makedirs(os.path.join(gdir, JARS_DIR), exist_ok=True)
        session = target.split(":")[0]
        return jsonify({"ok": True, "jars": _list_jars(gdir), "jars_dir": JARS_DIR,
                        "last_jar": _last_jar(gdir, session),
                        "last_mem": _last_mem(gdir, session),
                        "scripts": _list_scripts(gdir),
                        "last_mode": _last_mode(gdir, session),
                        "last_script": _last_script(gdir, session)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


class StartError(ValueError):
    """A start that was refused, carrying the status the endpoint should return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _resolve_start_script(gdir: str, name: str) -> str:
    """Check a custom start script and return the path we would run.

    A server that is launched by its own start.sh — for the JVM flags, a
    pre-launch backup, a restart loop — cannot be described by a jar and a
    memory figure, so the panel takes the script's *name* instead. That name
    comes from the admin rather than off a list we read, which is the whole
    difference from the jar path, so every claim it makes is checked here:

      no slashes        it is a filename, not a path. `--server-dir/../..` and
                        `/etc/init.d/anything` are refused outright rather than
                        normalised into something that might resolve.
      no control chars  the line is typed at a pane, and a newline arrives as
                        Enter — ending our command and running the remainder.
                        shlex.quote() would keep the shell happy with it, which
                        is exactly why it can't be the only check.
      inside gdir       the *resolved* path's parent must be the game dir, so a
                        symlink pointing out of it is refused too.
      a plain file      isfile(), so directories, fifos and sockets are out.
      executable        we run it as ./name; without +x that is a shell error
                        in the pane the admin then has to go and read.

    Raises StartError for anything refused; returns the resolved path.
    """
    if not name:
        raise StartError("No start script named")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise StartError("Start script must be a plain filename in the game "
                         "directory, with no slashes")
    if any(ch < " " or ch == "\x7f" for ch in name):
        raise StartError("Start script name contains control characters")

    real = os.path.realpath(os.path.join(gdir, name))
    if os.path.dirname(real) != os.path.realpath(gdir):
        raise StartError(f"Start script resolves outside the game directory: {name}")
    if not os.path.isfile(real):
        raise StartError(f"No such start script in the game directory: {name}", 404)
    if not os.access(real, os.X_OK):
        raise StartError(f"Start script is not executable — chmod +x {name}")
    return real


def _list_scripts(gdir: str) -> list:
    """Names the game dir offers as start scripts, for the UI's name field.

    Only a *suggestion* list, unlike _list_jars(): the admin may type a name
    that isn't here — a script written since the page loaded — so this is not
    the gate. _resolve_start_script() is, and every entry is put through it, so
    the list can never offer a name that Start would then refuse.
    """
    try:
        entries = sorted(f for f in os.listdir(gdir) if not f.startswith("."))
    except OSError:
        return []
    out = []
    for name in entries:
        try:
            _resolve_start_script(gdir, name)
        except (StartError, OSError):
            continue
        out.append(name)
    return out


def _start_server(target: str, gdir: str, jar: str = None, mem: str = None,
                  mode: str = "jar", script: str = None) -> str:
    """Type a start command into the session's pane. Returns what it launched.

    The single start path: the Start button and the startup policy pass both
    come through here, so what happens at boot is exactly what happens on a click.
    Raises StartError for anything refused.

    Two mutually exclusive forms, and `mode` picks one rather than the presence
    of a field doing it: a UI that sends both (a script named, a jar still
    selected from before) must not be silently resolved one way here, and the
    admin's last choice is what the store remembers.
    """
    mode = str(mode or "jar").strip().lower()
    if mode not in _START_MODES:
        raise StartError(f"Unknown start mode: {mode}")

    # Each branch settles on what to run and what to remember about it; the two
    # then share one tail, so a script-started server is launched, guarded and
    # recorded by exactly the same code a jar-started one is.
    #
    # Both lines are *meant* for a shell, unlike the console commands. gdir
    # comes from the pane's CWD or the store, never from us, so quote it —
    # which also makes paths containing spaces work, as they previously did not.
    #
    # The `cd` goes to gdir, the same directory the jar or script was just
    # resolved in, so the server's CWD and what it runs can never disagree. It
    # used to go to SERVER_DIR instead, which only lined up when the pane
    # already happened to be there: with the pane elsewhere it ran a jar from
    # one game dir with the working directory of another. Where the pane is
    # already gdir this is a no-op, and a harmless one — it also closes the gap
    # between reading the pane's path and typing the line.
    if mode == "script":
        script = str(script or "").strip()
        _resolve_start_script(gdir, script)
        # ./name, the way the admin runs it by hand — and having cd'd first, the
        # script gets the working directory it is entitled to assume.
        cmd      = f"cd {shlex.quote(gdir)} && {shlex.quote('./' + script)}"
        launched = script
        remember = {"last_mode": "script", "last_script": script}
    else:
        mem = str(mem or "").strip().upper()
        if not jar:
            raise StartError("No jar selected")
        # fullmatch, not match: Python's `$` also matches just before a trailing
        # newline, so `^\d+[MG]$` accepts "1024M\n". The .strip() above removes
        # it today, but a newline reaching send-keys would be typed as Enter —
        # ending the java line and running whatever came after it. Don't leave
        # that hanging on a .strip() staying put.
        if not re.fullmatch(r'\d+[MG]', mem):
            raise StartError("Invalid memory value — use e.g. 1024M or 2G")

        # The caller *selects* a jar; it never contributes to the path we build.
        # Enumerate what is actually in the jars dir and require an exact match,
        # then use the entry we listed. No pattern-matching on the client's
        # string can be as trustworthy as "this is one of the files we just read
        # off disk", and it is the same list the UI offered, so anything else is
        # a stale or forged pick. A script name cannot be handled this way — the
        # admin types it — which is what _resolve_start_script() is for.
        selected = next((f for f in _list_jars(gdir) if f == jar), None)
        if selected is None:
            raise StartError(f"No such jar in {JARS_DIR}: {jar}", 404)
        jar_path = os.path.join(gdir, JARS_DIR, selected)

        cmd      = (f"cd {shlex.quote(gdir)} && "
                    f"java -Xmx{mem} -Xms{mem} -jar {shlex.quote(jar_path)} nogui")
        launched = selected
        remember = {"last_mode": "jar", "last_jar": selected, "last_mem": mem}

    if _is_running(target):
        raise StartError("Server is already running", 409)

    # The world is being tarred right now, following the stop that just
    # happened. Starting into it would have the server writing the directory
    # tar is reading, and the archive — the whole point of the backup — would be
    # a broken copy of a perfectly good world. It is a wait of seconds to
    # minutes, not a refusal: the Start button comes back by itself.
    if _stop_backup_running(target.split(":")[0]):
        raise StartError("Backing up the world after the last stop — "
                         "try again when it finishes", 409)

    # This used to create the session with `cmd` as its process when one was
    # missing, which tied the session's life to the server's: stopping the
    # server took the pane with it, and the panel reads that pane afterwards.
    # It was also unreachable — resolving gdir above already requires a pane.
    # _ensure_session only covers the gap between those two, where something
    # killed the session while this request was in flight.
    _ensure_session(target)
    tmux_send(cmd, target)
    # Remember on start, not just on stop, so stops that happen outside the
    # panel (console 'stop', a crash) still leave a sensible default behind —
    # and so the startup pass knows what to launch and with how much.
    _remember_run(gdir, target.split(":")[0], **remember)
    return launched


@app.route("/api/server/start", methods=["POST"])
def api_server_start():
    """Send the java start command to the tmux session."""
    target = _session_target()
    data   = request.get_json(force=True, silent=True) or {}

    try:
        gdir = tmux_pane_path(target)
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{target}' not found"}), 503

    try:
        # `or ""` rather than a get() default: the UI sends every field every
        # time, so the one the chosen mode does not use arrives as null, and
        # str(None) is the string "None" — a filename we would then go looking
        # for and report as missing, instead of as absent.
        _start_server(target, gdir,
                      str(data.get("jar") or "").strip(),
                      str(data.get("mem") or "1024M"),
                      mode=str(data.get("mode") or "jar"),
                      script=str(data.get("script") or ""))
        return jsonify({"ok": True})
    except StartError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── How the last run ended, and whether to bring it back ─────────────────────
#
# This is what "unless-stopped" is made of. It has to work whether or not the
# panel's Stop button was used and whether or not any page was open to watch, so
# it cannot lean on _LAST_RUNNING — that is in memory, is wiped by a panel
# restart, and only advances when somebody polls. It reads the evidence the
# stopped server left behind instead: the tail of its own log.
#
# Minecraft writes two different lines on the way down, and they mean different
# things:
#
#   "Stopping the server"   the /stop *command's* own feedback (lang key
#                           commands.stop.stopping, through sendSuccess) — so
#                           somebody asked for this
#   "Stopping server"       MinecraftServer.stopServer(), on the way out of the
#                           run loop — an orderly shutdown happened, and the line
#                           says nothing at all about who caused it
#
# A SIGTERM at host shutdown runs the shutdown hook, so it produces the second
# and never the first. That difference is the whole discriminator, and it is
# *content* rather than timing — which is why it still gets "stopped, then
# rebooted three hours later" right, where any grace window would fail.
#
# The strong line is matched against the message *body*, anchored. latest.log
# carries chat, so a substring test fires on a player typing "Stopping the
# server" — which would let anyone on the server pin it down permanently. The
# optional bracket group admits the wrapped forms a non-console command source
# produces: "[Rcon: Stopping the server]", "[Notch: Stopping the server]".
#
# Note "All dimensions are saved" is deliberately *not* a marker: `/save-all
# flush` takes the same branch, and backup plugins run it on a timer, so a crash
# after one would read as a deliberate stop.
_STOP_ASKED_RE = re.compile(r'^(?:\[[^\[\]]{1,64}: )?Stopping the server\.{0,3}\]?$')
_STOP_ORDERLY  = "Stopping server"
_RUN_STARTED   = "Starting minecraft server version"

# A crash is *also* an orderly shutdown, which is the trap here: runServer()
# catches Throwable, writes the crash report, and then its `finally` calls
# stopServer() — which logs "Stopping server" unconditionally. So a heap OOM at
# 3am looks exactly like a `kill` on a live host unless we notice the wreckage
# above it, and the panel would refuse to restart a crashed server while
# reporting that somebody signalled it.
#
# Vanilla also exits 0 in that case (there is no System.exit in MinecraftServer),
# so the exit status cannot tell us either — the log is the only witness.
_CRASH_MARKERS = (
    "Encountered an unexpected exception",                     # the fatal catch
    "Considering it to be crashed, server will forcibly shutdown",  # ServerWatchdog
)

_BOOT_TIME      = None
_BOOT_TIME_READ = False


def _boot_time() -> float | None:
    """When this host last booted, as a unix timestamp, or None if we cannot tell.

    Cached: it cannot change while we run, and on macOS it costs a subprocess.
    Read without a new dependency, the same rule _disk_usage() follows.
    """
    global _BOOT_TIME, _BOOT_TIME_READ
    if _BOOT_TIME_READ:
        return _BOOT_TIME
    _BOOT_TIME_READ = True
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    _BOOT_TIME = float(line.split()[1])
                    return _BOOT_TIME
    except (OSError, ValueError, IndexError):
        pass
    try:
        # macOS: "{ sec = 1755300000, usec = 123456 } Sat Aug 16 09:00:00 2025"
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=5).stdout
        hit = re.search(r'sec\s*=\s*(\d+)', out)
        if hit:
            _BOOT_TIME = float(hit.group(1))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return _BOOT_TIME


def _last_run_ending(gdir: str) -> tuple:
    """How the last run in this game dir ended, and when. See the note above.

        "asked"       a /stop was issued — the Stop button, a console stop or
                      rcon; all three log the same line
        "crashed"     it died of its own accord and said so: an exception, or
                      the watchdog giving up on a hung tick
        "orderly"     it shut down cleanly and nothing asked it to: a signal
        "crash"       it simply stopped, saying nothing: SIGKILL, an OOM kill,
                      power loss
        "absent"      no logs/latest.log — nothing has run here
        "unreadable"  there is one, and we could not read it

    Ranked asked > crashed > orderly, because a crash logs the orderly line too
    (see _CRASH_MARKERS) and a run whose operator typed `stop` and *then* hit an
    exception on the way down was still asked to stop.

    The scan runs forward and lets the *last* verdict win, resetting at each
    "Starting minecraft server version". latest.log normally rotates away at the
    next server start, but that is a log4j config an admin can change, and a
    stale marker left by the previous run would otherwise report this run's crash
    as deliberate. "What does the end of this log say happened last" is true
    however many runs share the file.
    """
    try:
        blob, mtime = _read_log_tail(gdir)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None

    asked = orderly = crashed = False
    for line in blob.splitlines():
        # Vanilla's "[12:34:56] [Server thread/INFO]: msg" and Forge's longer
        # "[12:34:56] [Server thread/INFO] [net.minecraft…/]: msg" both put the
        # message after the first "]: " — the timestamp is followed by " [".
        msg = line.partition("]: ")[2]
        if msg.startswith(_RUN_STARTED):
            asked = orderly = crashed = False
        elif _STOP_ASKED_RE.match(msg):
            asked = True
        elif msg == _STOP_ORDERLY:
            orderly = True
        elif msg.startswith(_CRASH_MARKERS):
            crashed = True

    return ("asked" if asked else "crashed" if crashed else
            "orderly" if orderly else "crash"), mtime


def _log_when(mtime) -> str:
    """" on 15 Aug at 21:04", for a sentence, or "" if we have no timestamp."""
    return datetime.fromtimestamp(mtime).strftime(" on %d %b at %H:%M") if mtime else ""


def _would_restart(gdir: str) -> tuple:
    """Whether "unless-stopped" would start this server, and why, in one sentence.

    The verdict and the words come out of the same place on purpose: the line
    printed at boot and the note on the Server page must never explain the panel
    two different ways.
    """
    ending, mtime = _last_run_ending(gdir)

    if ending == "asked":
        return False, f"the last run was asked to stop{_log_when(mtime)}"
    if ending == "unreadable":
        # The direction that surprises nobody. A disk that did not come back is
        # not evidence that the admin wanted this server up.
        return False, "the game's log could not be read, so how it last ended is unknown"
    if ending == "absent":
        return True, "there is no log of a previous run here"
    if ending == "crashed":
        # Named separately from the silent case below. "It crashed" is the one
        # thing on this card an admin will act on, and it is worth saying rather
        # than leaving them to infer it from "ended without being asked to".
        return True, f"the last run crashed{_log_when(mtime)}"
    if ending == "crash":
        return True, f"the last run ended without being asked to{_log_when(mtime)}"

    # Orderly, but nothing asked for it — so a signal. Which one is settled by
    # whether it landed inside this boot: a run that ended *before* the machine
    # came up was ended by the machine going down, and that is exactly the case
    # to come back from. One that ended while the host was up was killed by hand
    # (`kill`, `tmux kill-session`, a service manager), which is deliberate.
    boot = _boot_time()
    if boot is None:
        return False, (f"the last run shut down cleanly{_log_when(mtime)} and this "
                       f"host's boot time is unknown")
    if mtime < boot:
        return True, "the last run was still going when the host went down"
    return False, f"the last run was shut down by a signal{_log_when(mtime)}"


def _start_chain(plan: dict, session: str) -> str | None:
    """Fill in what the panel would start for a session; return what is missing.

    The chain is panel store → dir → the game dir's .vibepanel.json → the start
    form it last used, so a broken link is reported as itself rather than as a
    blank field: "we have never seen this session" and "its disk did not come
    back after the reboot" are different problems with different fixes.

    The script mode's last link is re-checked rather than merely read back: a
    start.sh that lost its +x, or was renamed, is a boot-time failure worth
    seeing on the Server page now instead of discovering after a reboot.
    """
    if not plan["dir"]:
        return "no game directory known for this session yet"
    if not os.path.isdir(plan["dir"]):
        return f"game directory is not readable: {plan['dir']}"

    plan["mode"] = _last_mode(plan["dir"], session)
    if plan["mode"] == "script":
        plan["script"] = _last_script(plan["dir"], session)
        try:
            _resolve_start_script(plan["dir"], plan["script"] or "")
        except StartError as e:
            return (str(e) if plan["script"] else
                    "no start script remembered — start this server "
                    "from the panel once")
        return None

    plan["jar"] = _last_jar(plan["dir"], session)
    plan["mem"] = _last_mem(plan["dir"], session) or "1024M"
    if not plan["jar"]:
        return "no jar remembered — start this server from the panel once"
    return None


def _start_plan(session: str) -> dict:
    """What the panel would do with this session when it next starts, and why.

    `problem` and `reason` answer different questions and both are wanted:
    `problem` is a broken link in the chain — something to go and fix — while
    `reason` is the policy's own account of the decision, which under
    "unless-stopped" is a reading of how the last run ended. A server set to
    "never" still reports a missing jar, because that is worth fixing before the
    admin changes their mind.

    `would_start`, not `will_start`: this is what the policy and the evidence
    come to, and it deliberately does not consult whether the server is running.
    That check belongs to _start_policy_pass() alone — it is the one thing there
    that is not policy — and asking tmux here would spend a round trip on every
    page load answering a question the page never asks.
    """
    st   = _session_state(session)
    plan = {"start_policy": _start_policy(session),
            "dir": st.get("dir"), "dir_confirmed": bool(st.get("dir_confirmed")),
            "mode": "jar", "jar": None, "mem": None, "script": None,
            "problem": None, "would_start": False, "reason": ""}

    plan["problem"] = _start_chain(plan, session)

    if plan["start_policy"] == "never":
        plan["reason"] = "this server is never started by the panel"
    elif plan["problem"]:
        plan["reason"] = plan["problem"]
    elif plan["start_policy"] == "always":
        plan["would_start"] = True
        plan["reason"] = "set to start whenever the panel does"
    else:
        # Only this policy pays for the log read, and only it shows a reason
        # drawn from evidence the other two ignore.
        plan["would_start"], plan["reason"] = _would_restart(plan["dir"])
    return plan


def _start_policy_pass() -> None:
    """Act on every session's start policy. Called once, at startup.

    Nothing here infers anything the policy did not say: not why the panel
    started, not how long the host has been up. "always" starts it, "never" does
    nothing ever, and "unless-stopped" asks _would_restart() to read the game's
    log — an admin who chose one of them owns the consequence.

    Every outcome prints, "never" included. One line per session makes the
    panel's own log a complete account of what it decided at boot, which is what
    somebody who finds a server running — or finds one that did not come back —
    needs to be able to read.

    Sessions are independent: one failing must not stop the others.
    """
    for target in SESSIONS:
        session = target.split(":")[0]
        try:
            plan = _start_plan(session)
            if plan["start_policy"] == "never":
                print(f"start policy: '{session}' — {plan['reason']}")
                continue
            # Not a policy check: `systemctl restart vibepanel` on a healthy host
            # must not type a second JVM into a running server's pane.
            if _is_running(target):
                print(f"start policy: '{session}' is already running — left alone")
                continue
            if not plan["would_start"]:
                print(f"start policy: '{session}' not started — {plan['reason']}")
                continue
            _start_server(target, plan["dir"], plan["jar"], plan["mem"],
                          mode=plan["mode"], script=plan["script"])
            what = (f"./{plan['script']}" if plan["mode"] == "script"
                    else f"{plan['jar']} with {plan['mem']}")
            print(f"start policy: '{session}' starting {what} in {plan['dir']} "
                  f"— {plan['reason']}")
        except Exception as e:
            print(f"start policy: '{session}' failed — {e}")


@app.route("/api/server/start-policy")
def api_server_start_policy_get():
    return jsonify({"ok": True, **_start_plan(_session_target().split(":")[0])})


@app.route("/api/server/start-policy", methods=["POST"])
def api_server_start_policy_set():
    """Set what the panel does with this server when the panel starts.

    Lives in the panel store rather than the game dir, so reading it never
    depends on resolving a directory first — which is exactly the condition the
    startup pass runs under, before any session is guaranteed to exist.

    Validated against _START_POLICIES here as well as on the way out, so the
    store never holds a value the reader would then have to refuse.
    """
    session = _session_target().split(":")[0]
    data = request.get_json(force=True, silent=True) or {}
    policy = data.get("start_policy")
    if policy not in _START_POLICIES:
        return jsonify({"ok": False,
                        "error": "start_policy must be one of "
                                 + ", ".join(_START_POLICIES)}), 400
    _update_session_state(session, start_policy=policy)
    return jsonify({"ok": True, **_start_plan(session)})


@app.route("/api/server/backup-on-stop")
def api_server_backup_on_stop_get():
    return jsonify({"ok": True, **_stop_backup_plan(_session_target().split(":")[0])})


@app.route("/api/server/backup-on-stop", methods=["POST"])
def api_server_backup_on_stop_set():
    """Set whether stopping this server archives its world.

    In the panel store beside the start policy, and for the same reason: it is a
    standing policy about a session rather than a fact about a game, and the
    panel has to be able to read it in the one situation where it applies —
    a server that has just stopped — without that read depending on anything
    the stopped server can no longer tell it.
    """
    session = _session_target().split(":")[0]
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data.get("backup_on_stop"), bool):
        return jsonify({"ok": False, "error": "backup_on_stop must be true or false"}), 400
    _update_session_state(session, backup_on_stop=data["backup_on_stop"])
    return jsonify({"ok": True, **_stop_backup_plan(session)})


@app.route("/api/server/stop", methods=["POST"])
def api_server_stop():
    """Send the 'stop' command to the Minecraft server console via tmux."""
    target = _session_target()
    if not _is_running(target):
        return jsonify({"ok": False, "error": "Server is not running"}), 409
    # Remember the jar this server was running, and the heap it was running
    # with, so the UI can put both back next time. _start_server() already
    # records them when the panel is what started the server; this is the path
    # that covers one started by hand at the pane, where the process itself is
    # the only account of what was chosen. _remember_run() drops whichever of
    # the two we could not read, leaving the previous value rather than
    # blanking it. Best-effort throughout: never block the stop.
    try:
        info = _pane_java_info(target)
        _remember_run(tmux_pane_path(target), target.split(":")[0],
                      last_jar=info.get("jar"), last_mem=info.get("mem"))
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

    # fullmatch for the same reason as the memory value: `$` would otherwise let a
    # trailing newline through. The version is passed to get-me-fabric.sh as argv
    # (never through a shell), but the script interpolates it into a URL, so keep
    # it to characters that can appear in a version number.
    if version and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.\-]*', version):
        return jsonify({"ok": False, "error": "Invalid version string"}), 400
    if version and len(version) > 40:
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
                    "port": port, "bedrock_port": _read_bedrock_port(gdir),
                    "public_ip": PUBLIC_IP})


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

    try:
        filename, size = _archive_world(gdir, name or None)
        return jsonify({"ok": True, "filename": filename, "size": size})
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
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
    # Load deletes the current world, and the on-stop backup may still be
    # reading it — that would leave the backup a truncated archive of the world
    # it was meant to preserve, at the exact moment the world itself is being
    # replaced. Wait for it.
    if _stop_backup_running(target.split(":")[0]):
        return jsonify({"ok": False,
                        "error": "Backing up the world after the last stop — "
                                 "try again when it finishes"}), 409

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
        try:
            autosaved, _ = _archive_world(gdir, "autosave")
        except subprocess.CalledProcessError as e:
            return jsonify({"ok": False, "error": f"Autosave failed: {e.stderr or str(e)}"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": f"Autosave failed: {e}"}), 500
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


# Highest host figures seen, on the same terms as HEAP_PEAKS: sampled only when
# something asks for stats, never on a timer of the panel's own.
SYSTEM_PEAKS = {"cpu": None, "ram": None, "disk": None}


def _record_peak(key: str, value) -> float | int | None:
    """Fold `value` into SYSTEM_PEAKS[key] and hand back the running peak."""
    if value is None:
        return SYSTEM_PEAKS[key]
    cur = SYSTEM_PEAKS[key]
    if cur is None or value > cur:
        SYSTEM_PEAKS[key] = value
    return SYSTEM_PEAKS[key]


@app.route("/api/system/stats")
def api_system_stats():
    """Host-level CPU, RAM, and disk stats (no session param needed).

    Each block carries a `peak`, in that block's own unit — load average for
    cpu, bytes used for ram and disk — so the bars can mark the high-water
    point next to the current one. Peaks live in memory and are reset together
    with the per-server heap peaks by /api/peaks/reset.
    """
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

    cpu = {"load_1m": round(load1, 2), "load_5m": round(load5, 2),
           "load_15m": round(load15, 2), "cores": cores}
    cpu["peak"] = _record_peak("cpu", cpu["load_1m"])
    if ram:
        ram = {**ram, "peak": _record_peak("ram", ram.get("used"))}
    if disk:
        disk = {**disk, "peak": _record_peak("disk", disk.get("used"))}

    return jsonify({"ok": True, "cpu": cpu, "ram": ram, "disk": disk})


@app.route("/api/peaks/reset", methods=["POST"])
def api_peaks_reset():
    """Forget every recorded peak: host stats and all sessions' heaps.

    Deliberately global rather than per-session — the button is on the Overview,
    which is the page that shows all of them at once.
    """
    HEAP_PEAKS.clear()
    for k in SYSTEM_PEAKS:
        SYSTEM_PEAKS[k] = None
    return jsonify({"ok": True})


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
                        help="fallback game directory for a session the panel has "
                             "never seen; normally learned and remembered instead")
    parser.add_argument("--state-file", default=None,
                        help=f"panel state file (default: ./{PANEL_STATE_FILE and os.path.basename(PANEL_STATE_FILE)})")
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

    if args.state_file:
        PANEL_STATE_FILE = os.path.abspath(os.path.expanduser(args.state_file))
    _load_panel_state()

    # --session *declares* the expected set. Passing it says "these are the
    # servers", replacing whatever was stored, which is what gives an admin a way
    # to forget one: stop passing it. Passing nothing reuses the last declared
    # set, so the usual invocation after the first is just `server.py`.
    raw_sessions = args.sessions or _stored_sessions() or [TMUX_TARGET]
    if len(raw_sessions) == 1 and not _session_state(raw_sessions[0].split(":")[0]).get("dir"):
        # Adoption is the fallback for not knowing what to do. Once the store
        # knows where this session lives we can recreate it ourselves, and
        # silently repointing at whichever session happens to be up would drop
        # what we learned and manage the wrong server.
        SESSIONS = [_resolve_tmux_target(raw_sessions[0])]
    else:
        SESSIONS = list(raw_sessions)
    TMUX_TARGET = SESSIONS[0]
    _set_expected_sessions([s.split(":")[0] for s in SESSIONS])

    PUBLIC_IP = _fetch_public_ip()
    if PUBLIC_IP:
        print(f"Public IP: {PUBLIC_IP}")

    if SERVER_DIR:
        # Say which directory we resolved to, and warn now rather than leaving a
        # failed `cd` to surface as a mysteriously-not-starting server later.
        print(f"Server dir: {SERVER_DIR}"
              + ("" if os.path.isdir(SERVER_DIR) else "  (does not exist yet)"))

    # Open any session that isn't there yet — after adoption has had its say, so
    # we never create 'minecraft' next to the 'mc' we were about to adopt — then
    # look at each one, which is what teaches the store where a server lives.
    for s in SESSIONS:
        ensured = _ensure_session(s)
        try:
            _observe_session(s, _pane_java_info(s), ensured)
        except Exception:
            pass
        st = _session_state(s.split(":")[0])
        if st.get("dir"):
            print(f"session '{s.split(':')[0]}': {st['dir']}"
                  + ("" if st.get("dir_confirmed") else "  (unconfirmed)"))

    _start_policy_pass()

    session_display = ', '.join(SESSIONS)
    print(f"VibePanel starting on http://{args.host}:{args.port}  "
          f"(sessions: {session_display}, jars: {JARS_DIR})")
    app.run(host=args.host, port=args.port, threaded=True)
