# VibePanel

A lightweight web frontend for a Minecraft server running in a tmux session. Manage the server from your phone or browser without SSH-ing in.

![Console, Players, Mods, Worlds, and Server pages](.github/screenshot.png)

## Features

- **Live console** — streams tmux output in real time via Server-Sent Events
- **Players** — lists online players via `/list`
- **Say** — broadcasts a message to the server as `[Server]`
- **Mods** — toggle Fabric mods on/off (moves files between `mods/` and `mods-saves/`); detects byte-for-byte conflicts
- **Worlds** — save, load, and delete world backups as `.tgz` archives; autosaves before loading
- **Server** — start/stop the server, download Fabric jars, view MOTD and server icon

## Requirements

- Python 3.10+
- Flask 3.x (`pip install flask`)
- tmux (server must be running in a tmux session)
- Linux (for CWD detection via `/proc`; macOS works with reduced accuracy)

## Quick start

```bash
git clone <this-repo> vibepanel
cd vibepanel
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py --session minecraft --port 8080
```

Open `http://<host>:8080` in a browser. The `--session` value should match the name of your tmux session (`tmux ls` to check). If you only have one tmux session, the name doesn't matter — VibePanel will find it automatically.

## Configuration

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--session` | `TMUX_TARGET` | `minecraft` | tmux target for the Minecraft pane |
| `--port` | — | `8080` | HTTP port |
| `--jars-dir` | `JARS_DIR` | `server-jars` | where downloaded `.jar` files are stored |
| `--worlds-dir` | `WORLDS_DIR` | `world-saves` | where world `.tgz` backups are stored |
| `--mods-dir` | `MODS_DIR` | `mods` | active Fabric mods directory |
| `--mods-saves-dir` | `MODS_SAVES_DIR` | `mods-saves` | inactive mods directory |
| `--server-dir` | `SERVER_DIR` | *(none)* | cd here before starting the server |

All paths are relative to the Minecraft server's working directory (auto-detected from the tmux pane).

## Running as a service

A systemd unit file is included. Edit `vibepanel.service` to set `User`, `WorkingDirectory`, and your `--session`, then:

```bash
sudo cp vibepanel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vibepanel
```

## How it works

VibePanel attaches to your existing tmux pane and interacts with it directly — it sends keystrokes to start/stop the server, issues commands like `/list`, and streams pane output to the browser. It does not run the Minecraft server itself.

Server-running detection uses `ps -t <pane_tty>` rather than checking the foreground process name, so servers launched via wrapper scripts (`bash start.sh`) are detected correctly.

## License

MIT
