# CLAUDE.md

## Running the dev server

```bash
python server.py                          # defaults: 0.0.0.0:8080, tmux session "minecraft"
python server.py --port 5000 --session mc # custom port and session name
```

All config can also be set via environment variables (see below).

## CLI flags / environment variables

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--session` | `TMUX_TARGET` | `minecraft` | tmux target (session, session:window, or session:window.pane) |
| `--jars-dir` | `JARS_DIR` | `server-jars` | dir (relative to game dir) where .jar files live |
| `--server-dir` | `SERVER_DIR` | *(none)* | cd here before starting the server |
| `--worlds-dir` | `WORLDS_DIR` | `world-saves` | dir for world .tgz backups |
| `--mods-dir` | `MODS_DIR` | `mods` | active mods directory |
| `--mods-saves-dir` | `MODS_SAVES_DIR` | `mods-saves` | inactive (stashed) mods directory |
| `--host` | — | `0.0.0.0` | bind address |
| `--port` | — | `8080` | bind port |

## tmux session detection

VibePanel attaches to a tmux pane and reads/writes to it:

- If `--session` names a session that exists, that session is used.
- If the named session is not found **and there is exactly one tmux session visible**, that sole session is adopted automatically (useful when the user hasn't named their session).
- If no tmux is reachable at all, status endpoints return 503.

The "game directory" is resolved from the **foreground process group's CWD** inside the pane, not the tmux session's startup directory. This is done via `/proc/<shell_pid>/stat` (tpgid field) + `/proc/<tpgid>/cwd` on Linux, with a fallback to `#{pane_current_path}` on macOS/other.

## Server running detection

`_is_running()` / `_pane_java_info()` use `#{pane_tty}` + `ps -t <tty> -o pid=,args=` to find a `java` process on the pane's tty. This works regardless of process tree depth — a server started as `bash start.sh` (where java is a grandchild of the shell) is detected correctly. Do **not** use `#{pane_current_command}` for this; it only returns the foreground process group leader name, which is `bash` in the wrapper-script case.

## Directory layout (relative to game dir)

```
<game-dir>/
  server-jars/       # .jar files for starting the server  (JARS_DIR)
  mods/              # active Fabric mods                   (MODS_DIR)
  mods-saves/        # inactive/stashed mods                (MODS_SAVES_DIR)
  world-saves/       # .tgz world backups                   (WORLDS_DIR)
  get-me-fabric.sh   # auto-installed from repo root if missing
```

## Dependencies

```bash
pip install flask>=3.0.0   # only runtime dependency
```

`tmux` must be installed and on `PATH`. `wget` is used inside `get-me-fabric.sh`.

## Systemd deployment

See `vibepanel.service` — drop it in `/etc/systemd/system/`, adjust `User` / `WorkingDirectory` / `--session`, then:

```bash
systemctl daemon-reload
systemctl enable --now vibepanel
```
