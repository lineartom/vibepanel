#!/usr/bin/env python3
import os
import re
import time
import json
import argparse
import subprocess
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, send_file

app = Flask(__name__)

TMUX_TARGET = os.environ.get("TMUX_TARGET", "minecraft")
JARS_DIR    = os.environ.get("JARS_DIR", "server-jars")
SERVER_DIR  = os.environ.get("SERVER_DIR", "")

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
    """Return the current working directory of the tmux pane."""
    result = subprocess.run(
        ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_current_path}"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


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
        # Is the foreground process in our pane actually java?
        current = subprocess.run(
            ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_current_command}"],
            capture_output=True, text=True,
        )
        if current.returncode != 0:
            return jsonify({"running": False})
        if current.stdout.strip().lower() != "java":
            return jsonify({"running": False})

        # Get the shell PID that owns this pane, then find its java child.
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-t", TMUX_TARGET, "-p", "#{pane_pid}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        children = subprocess.run(
            ["pgrep", "-P", pane_pid],
            capture_output=True, text=True,
        ).stdout.splitlines()

        for child_pid in children:
            child_pid = child_pid.strip()
            if not child_pid:
                continue
            args = subprocess.run(
                ["ps", "-ww", "-o", "args=", "-p", child_pid],
                capture_output=True, text=True,
            ).stdout.strip()
            if "java" not in args:
                continue
            m = re.search(r"-jar\s+(\S+\.jar)", args)
            return jsonify({
                "running": True,
                "jar": os.path.basename(m.group(1)) if m else None,
            })

        # pane_current_command was java but child lookup raced — still running
        return jsonify({"running": True, "jar": None})

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


@app.route("/api/server/download-fabric", methods=["POST"])
def api_download_fabric():
    """Run get-me-fabric.sh to download a Fabric server jar into JARS_DIR."""
    data    = request.get_json(force=True, silent=True) or {}
    version = str(data.get("version", "")).strip()

    if version and not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.\-]*$', version):
        return jsonify({"ok": False, "error": "Invalid version string"}), 400

    try:
        gdir = tmux_pane_path()
    except subprocess.CalledProcessError:
        return jsonify({"ok": False, "error": f"tmux target '{TMUX_TARGET}' not found"}), 503

    script = os.path.join(gdir, "get-me-fabric.sh")
    if not os.path.isfile(script):
        return jsonify({"ok": False, "error": f"Script not found: {script}"}), 404

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
    args = parser.parse_args()

    if args.session:
        TMUX_TARGET = args.session
    if args.jars_dir:
        JARS_DIR = args.jars_dir
    if args.server_dir:
        SERVER_DIR = args.server_dir

    print(f"VibePanel starting on http://{args.host}:{args.port}  "
          f"(tmux: {TMUX_TARGET}, jars: {JARS_DIR})")
    app.run(host=args.host, port=args.port, threaded=True)
