# CLAUDE.md

## Running the dev server

```bash
python server.py                                        # defaults: 0.0.0.0:8080, tmux session "minecraft"
python server.py --port 5000 --session mc               # custom port and session name
python server.py --session survival --session creative  # multiple servers; shows tab switcher
```

All config can also be set via environment variables (see below).

## CLI flags / environment variables

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--session` | `TMUX_TARGET` | `minecraft` | tmux target; **repeat** for multiple servers: `--session mc1 --session mc2` |
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

## Player management (whitelist / ops / bans)

`/api/players/roster` merges `whitelist.json`, `ops.json`, and `banned-players.json`
into one list keyed by UUID (falling back to a lowercased name key when an entry has
no usable UUID), so each player appears once with `whitelisted` / `op` / `banned` flags.

Writes take one of **two paths**, chosen by `_is_running()`:

- **server running** → console commands via tmux (`op`, `deop`, `whitelist add|remove`,
  `ban`, `pardon`). The running server holds those json files in memory and rewrites
  them on change, so editing them directly would be clobbered.
- **server stopped** → the json files are edited in place (atomic write via `.tmp` +
  `os.replace`). New entries use the vanilla shapes, including `level: 4` /
  `bypassesPlayerLimit` for ops and `created` / `source` / `expires` / `reason` for bans.

Responses carry `via: "console" | "file"` so the frontend knows to wait ~1.4 s for the
server to rewrite its files before re-reading the roster.

`_resolve_uuid()` supplies UUIDs for stopped-mode writes, in this order: the UUID the
admin typed/pasted → existing json entries → `logs/latest.log`. **Never add an online
lookup here.** The panel talks only to its own tmux server and its own files, so a
player who has never joined can be added only by pasting their UUID or by starting the
server and letting it resolve the name over the console. The Add form reflects that:
with the server stopped the UUID field is required and validated client-side; with it
running the field is disabled and its placeholder says the server resolves the UUID.

### Diagnosing an empty roster

`/api/players/roster` returns `game_dir`, `files` (which of the three json files exist),
and `log_found`, and the empty state renders them. An empty roster is far more often a
game dir resolved to the wrong place than a genuinely empty whitelist — note that
`tmux_pane_path()` follows the *foreground* process, so a pane whose shell sits
somewhere other than the game dir resolves differently while the server is stopped than
it does while java is running. `/api/server/status` carries an `ok` flag for the same
reason: an unreachable tmux pane and an idle one both report `running: false`, and the
UI has to be able to tell them apart.

Names are validated against `^[A-Za-z0-9_]{1,16}$` before ever reaching a console
command; ban reasons are stripped of control characters like `/api/say` does.

### Log scraping for add-suggestions

`_scan_latest_log()` pulls `UUID of player <name> is <uuid>` lines out of the last
512 KB of **`logs/latest.log` only**. Rotated logs are ignored, `.gz` and plain alike:
stitching the tails of several files together would mean searching a history with holes
in it, which is both slower and confusing to reason about.

Log lines carry only `[HH:MM:SS]`, so dates are reconstructed: within one `latest.log`
the clock runs forward (a restart rotates the file away), so each backwards jump is a
midnight rollover. Count them, then date each hit by counting back from the file's
mtime, which is the date of its last line.

## Directory layout (relative to game dir)

```
<game-dir>/
  server-jars/         # .jar files for starting the server  (JARS_DIR)
  mods/                # active Fabric mods                   (MODS_DIR)
  mods-saves/          # inactive/stashed mods                (MODS_SAVES_DIR)
  world-saves/         # .tgz world backups                   (WORLDS_DIR)
  logs/latest.log      # scraped for player name→UUID suggestions (read-only)
  whitelist.json       # read + written by the Players page
  ops.json             #   "
  banned-players.json  #   "
  get-me-fabric.sh     # auto-installed from repo root if missing
  .vibepanel.json      # panel state: last-used jar per session (written on start/stop)
```

## External network access

The panel reaches the internet in exactly **three** places, and the list is meant to
stay short — default to reading our own files and talking to our own tmux server:

1. `get-me-fabric.sh` downloading a server jar (the whole point of that feature).
2. `_latest_minecraft_version()` asking the Fabric meta API which version is current.
3. `_fetch_public_ip()` asking `api.ipify.org` for our public-facing address.

The IP lookup runs **once, from `__main__` at startup** — never per request — and the
result is cached in the `PUBLIC_IP` global and handed out by `/api/server/identity`.
Keep it that way: the Server page hits that endpoint on every visit and refresh, so
resolving lazily there would turn ordinary browsing into traffic against ipify. The
response is parsed with `ipaddress.ip_address()`, so an error page or junk body yields
`None` rather than something odd rendered into the UI. If the lookup fails the panel
simply shows no IP until the next restart.

Notably **not** on the list: any player-account lookup. See the roster section above.

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
