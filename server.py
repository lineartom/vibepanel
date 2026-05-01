#!/usr/bin/env python3
import os
import re
import time
import json
import argparse
import shutil
import subprocess
import urllib.request
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file

app = Flask(__name__)

TMUX_TARGET = os.environ.get("TMUX_TARGET", "minecraft")
JARS_DIR    = os.environ.get("JARS_DIR", "server-jars")
SERVER_DIR  = os.environ.get("SERVER_DIR", "")
WORLDS_DIR  = os.environ.get("WORLDS_DIR", "world-saves")
MODS_DIR    = os.environ.get("MODS_DIR", "mods")
MODS_SAVES_DIR = os.environ.get("MODS_SAVES_DIR", "mods-saves")

_ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
_MC_FMT = re.compile(r'§[0-9a-fklmnorABCDEFKLMNOR]')


def clean(text: str) -> str:
    return _MC_FMT.sub('', _ANSI.sub('', text))


def tmux_send(command: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", TMUX_TARGET, command, "Enter"],
        check=True, capture_output=True,
    )


def tmux_capture(lines: int = 300) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", TMUX_TARGET, "-S", f"-{lines}"],
        capture_output=True, text=True, check=True,
    )
    return clean(result.stdout)


def tmux_pane_path() -> str:
    """Return the CWD of the foreground process in the tmux pane.

    Uses /proc to find the terminal's foreground process group (tpgid from
    /proc/<shell_pid>/stat) and resolves /proc/<tpgid>/cwd.  This correctly
    follows nested shells, manual cd after session creation, etc.
    Falls back to tmux's #{pane_current_path} on non-Linux hosts.
    """
    shell_pid = subprocess.run(
        ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_pid}"],
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
        ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_current_path}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


_WORLD_SAVE_RE = re.compile(r'^world-\d{8}-\d{6}(?:-[a-zA-Z0-9_-]+)?\.tgz$')


def _is_running() -> bool:
    """Return True if a Minecraft server (java) is running in our tmux pane."""
    result = subprocess.run(
        ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_current_command}"],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "java"


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
            "ok":      False,
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
    return render_template("index.html", tmux_target=TMUX_TARGET)


@app.route("/api/status")
def api_status():
    try:
        tmux_capture(1)
        return jsonify({"ok": True, "target": TMUX_TARGET})
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503


@app.route("/api/players")
def api_players():
    try:
        tmux_send("list")
        time.sleep(0.8)
        output = tmux_capture(50)
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


@app.route("/api/say", methods=["POST"])
def api_say():
    try:
        data = request.get_json(force=True, silent=True) or {}
        message = str(data.get("message", "")).strip()
        if not message:
            return jsonify({"ok": False, "error": "Empty message"}), 400
        if len(message) > 256:
            return jsonify({"ok": False, "error": "Message too long (max 256 chars)"}), 400
        # Strip all C0 and C1 control characters. Leaving any in (e.g. \x03 Ctrl+C,
        # \x1a Ctrl+Z, \x04 EOF) would send signals to the tmux pane's foreground
        # process via the pty line discipline, potentially killing the server.
        message = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', message)
        if not message:
            return jsonify({"ok": False, "error": "Message was empty after stripping control characters"}), 400
        tmux_send(f"say {message}")
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/status")
def api_server_status():
    """Detect whether a Minecraft server process is running inside our tmux pane."""
    try:
        # Quick gate: is the pane's foreground command java at all?
        current = subprocess.run(
            ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        if current.returncode != 0 or current.stdout.strip().lower() != "java":
            return jsonify({"running": False})

        # Use the same tpgid trick as tmux_pane_path(): read the kernel-maintained
        # foreground process group ID from /proc/<shell_pid>/stat, then read
        # /proc/<tpgid>/cmdline.  This finds java however it was launched —
        # typed directly, run via a wrapper script, or started with exec.
        shell_pid = subprocess.run(
            ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_pid}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        jar = None
        try:
            with open(f"/proc/{shell_pid}/stat") as f:
                stat = f.read()
            after_comm = stat[stat.rindex(')') + 2:]
            tpgid = after_comm.split()[5]
            if tpgid != "-1":
                with open(f"/proc/{tpgid}/cmdline", "rb") as f:
                    args = f.read().rstrip(b"\x00").split(b"\x00")
                args = [a.decode("utf-8", errors="replace") for a in args]
                if "-jar" in args:
                    jar_path = args[args.index("-jar") + 1]
                    if jar_path.endswith(".jar"):
                        jar = os.path.basename(jar_path)
        except (FileNotFoundError, OSError, IndexError, ValueError):
            # Non-Linux or /proc unavailable — fall back to scanning direct children.
            children = subprocess.run(
                ["pgrep", "-P", shell_pid], capture_output=True, text=True,
            ).stdout.splitlines()
            for child_pid in children:
                child_pid = child_pid.strip()
                if not child_pid:
                    continue
                ps_args = subprocess.run(
                    ["ps", "-ww", "-o", "args=", "-p", child_pid],
                    capture_output=True, text=True,
                ).stdout.strip()
                if "java" not in ps_args:
                    continue
                m = re.search(r"-jar\s+(\S+\.jar)", ps_args)
                if m:
                    jar = os.path.basename(m.group(1))
                break

        return jsonify({"running": True, "jar": jar})

    except subprocess.CalledProcessError:
        return jsonify({"running": False})
    except Exception as e:
        return jsonify({"running": False, "error": str(e)})


@app.route("/api/server/jars")
def api_server_jars():
    """List .jar files available in the configured jars directory."""
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503
    try:
        jars_path = os.path.join(gdir, JARS_DIR)
        if not os.path.isdir(jars_path):
            return jsonify({"ok": True, "jars": [], "jars_dir": JARS_DIR})
        jars = sorted(f for f in os.listdir(jars_path) if f.endswith(".jar"))
        return jsonify({"ok": True, "jars": jars, "jars_dir": JARS_DIR})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/start", methods=["POST"])
def api_server_start():
    """Send the java start command to the tmux session."""
    data = request.get_json(force=True, silent=True) or {}
    jar = str(data.get("jar", "")).strip()
    mem = str(data.get("mem", "1024M")).strip().upper()

    if not re.match(r'^[\w][\w\-\.]*\.jar$', jar):
        return jsonify({"ok": False, "error": "Invalid jar name"}), 400
    if not re.match(r'^\d+[MG]$', mem):
        return jsonify({"ok": False, "error": "Invalid memory value — use e.g. 1024M or 2G"}), 400

    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

    jar_path = os.path.join(gdir, JARS_DIR, jar)
    if not os.path.isfile(jar_path):
        return jsonify({"ok": False, "error": f"Jar not found: {jar}"}), 404

    # Guard: don't type a start command into a running server's console.
    current = subprocess.run(
        ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_current_command}"],
        capture_output=True, text=True,
    )
    if current.returncode == 0 and current.stdout.strip().lower() == "java":
        return jsonify({"ok": False, "error": "Server is already running"}), 409

    cmd = f"java -Xmx{mem} -Xms{mem} -jar {jar_path} nogui"
    if SERVER_DIR:
        cmd = f"cd {SERVER_DIR} && {cmd}"

    try:
        session = TMUX_TARGET.split(":")[0]
        has = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True)
        if has.returncode == 0:
            tmux_send(cmd)
        else:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session, cmd],
                check=True, capture_output=True,
            )
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/stop", methods=["POST"])
def api_server_stop():
    """Send the 'stop' command to the Minecraft server console via tmux."""
    current = subprocess.run(
        ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_current_command}"],
        capture_output=True, text=True,
    )
    if current.returncode != 0 or current.stdout.strip().lower() != "java":
        return jsonify({"ok": False, "error": "Server is not running"}), 409
    try:
        tmux_send("stop")
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/api/server/download-fabric", methods=["POST"])
def api_download_fabric():
    """Run get-me-fabric.sh to download a Fabric server jar into JARS_DIR."""
    data    = request.get_json(force=True, silent=True) or {}
    version = str(data.get("version", "")).strip()
    app.logger.debug(f"Requested Fabric download for version: '{version}' ({type(version)})")
    app.logger.debug(f"Request JSON data: {json.dumps(data)}")
    if version == "None":
        version = None

    if version and not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.\-]*$', version):
        return jsonify({"ok": False, "error": "Invalid version string"}), 400

    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

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

    cmd = [script, os.path.join(gdir, JARS_DIR)]
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
            "ok": False,
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
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

    has_icon = os.path.isfile(os.path.join(gdir, "server-icon.png"))

    motd = None
    props = os.path.join(gdir, "server.properties")
    if os.path.isfile(props):
        try:
            with open(props, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    key, _, val = line.strip().partition("=")
                    if key == "motd":
                        motd = val
                        break
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

    return jsonify({"ok": True, "has_icon": has_icon, "motd": motd})


@app.route("/api/server/icon")
def api_server_icon():
    """Serve the server-icon.png from the tmux pane's working directory."""
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

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
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

    saves_path = os.path.join(gdir, WORLDS_DIR)
    if not os.path.isdir(saves_path):
        return jsonify({"ok": True, "saves": [], "total_bytes": 0})

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
    if _is_running():
        return jsonify({"ok": False, "error": "Server must be stopped before saving a world"}), 409

    data = request.get_json(force=True, silent=True) or {}
    name = re.sub(r'[^a-zA-Z0-9_-]', '', str(data.get("name", "")).strip())[:50]

    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

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
    if _is_running():
        return jsonify({"ok": False, "error": "Server must be stopped before loading a world"}), 409

    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()

    if not _WORLD_SAVE_RE.match(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

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
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()

    if not _WORLD_SAVE_RE.match(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400

    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

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
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

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
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

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
    if _is_running():
        return jsonify({"ok": False, "error": "Server must be stopped before changing mods"}), 409
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()
    if not _validate_mod_filename(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503
    return _do_mod_move(
        os.path.join(gdir, MODS_SAVES_DIR),
        os.path.join(gdir, MODS_DIR),
        filename,
    )


@app.route("/api/mods/deactivate", methods=["POST"])
def api_mods_deactivate():
    """Move a mod from mods/ into mods-saves/."""
    if _is_running():
        return jsonify({"ok": False, "error": "Server must be stopped before changing mods"}), 409
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()
    if not _validate_mod_filename(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503
    return _do_mod_move(
        os.path.join(gdir, MODS_DIR),
        os.path.join(gdir, MODS_SAVES_DIR),
        filename,
    )


@app.route("/api/mods/delete", methods=["POST"])
def api_mods_delete():
    """Delete a mod from 'active', 'inactive', or 'both' locations."""
    data     = request.get_json(force=True, silent=True) or {}
    filename = str(data.get("filename", "")).strip()
    location = str(data.get("location", "")).strip()

    if not _validate_mod_filename(filename):
        return jsonify({"ok": False, "error": "Invalid filename"}), 400
    if location not in ("active", "inactive", "both"):
        return jsonify({"ok": False, "error": "Invalid location"}), 400

    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

    targets = []
    if location in ("active", "both"):
        targets.append(os.path.join(gdir, MODS_DIR, filename))
    if location in ("inactive", "both"):
        targets.append(os.path.join(gdir, MODS_SAVES_DIR, filename))

    errors  = []
    deleted = 0
    for path in targets:
        if os.path.isfile(path):
            try:
                os.remove(path)
                deleted += 1
            except Exception as e:
                errors.append(str(e))

    if errors:
        return jsonify({"ok": False, "error": "; ".join(errors), "deleted": deleted}), 500
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/console/stream")
def api_console_stream():
    def generate():
        yield f"retry: 3000\n\n"
        last = ""
        while True:
            try:
                content = tmux_capture(300)
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
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VibePanel — Minecraft web frontend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--session", default=None,
                        help="tmux target (session, session:window, or session:window.pane)")
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

    if args.session:
        TMUX_TARGET = args.session
    if args.jars_dir:
        JARS_DIR = args.jars_dir
    if args.server_dir:
        SERVER_DIR = args.server_dir
    if args.worlds_dir:
        WORLDS_DIR = args.worlds_dir
    if args.mods_dir:
        MODS_DIR = args.mods_dir
    if args.mods_saves_dir:
        MODS_SAVES_DIR = args.mods_saves_dir

    print(f"VibePanel starting on http://{args.host}:{args.port}  "
          f"(tmux: {TMUX_TARGET}, jars: {JARS_DIR})")
    app.run(host=args.host, port=args.port, threaded=True)
