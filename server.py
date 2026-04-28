#!/usr/bin/env python3
import os
import re
import time
import json
import argparse
import subprocess
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

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
        # Strip anything that could inject additional tmux keystrokes
        message = message.replace('\n', ' ').replace('\r', '').replace('\x00', '')
        tmux_send(f"say {message}")
        return jsonify({"ok": True})
    except subprocess.CalledProcessError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/server/status")
def api_server_status():
    """Detect whether a Minecraft server process is running and which jar it uses."""
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        abs_jars = os.path.abspath(JARS_DIR)
        jars_name = os.path.basename(abs_jars)
        for line in result.stdout.splitlines():
            if "java" not in line or "grep" in line:
                continue
            m = re.search(r"-jar\s+(\S+\.jar)", line)
            if not m:
                continue
            jar_in_cmd = m.group(1)
            if (jars_name + "/" in jar_in_cmd
                    or abs_jars in jar_in_cmd
                    or "nogui" in line):
                return jsonify({"running": True, "jar": os.path.basename(jar_in_cmd)})
        return jsonify({"running": False})
    except Exception as e:
        return jsonify({"running": False, "error": str(e)})


@app.route("/api/server/jars")
def api_server_jars():
    """List .jar files available in the configured jars directory."""
    try:
        jars_path = os.path.abspath(JARS_DIR)
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

    jar_path = os.path.abspath(os.path.join(JARS_DIR, jar))
    if not os.path.isfile(jar_path):
        return jsonify({"ok": False, "error": f"Jar not found: {jar}"}), 404

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
