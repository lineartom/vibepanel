# VibePanel

A lightweight web frontend for a Minecraft server running in a tmux session. Manage the server from your phone or browser without SSH-ing in.

![Console, Players, Mods, Worlds, and Server pages](.github/screenshot.png)

## Features

- **Live console** — streams tmux output in real time via Server-Sent Events
- **Players** — online players via `/list`, plus the full roster from `whitelist.json` / `ops.json` / `banned-players.json` with names and UUIDs; op/de-op, whitelist, remove, and ban/unban from the UI, with add-suggestions scraped from `logs/latest.log`
- **Say** — broadcasts a message to the server as `[Server]`
- **Mods** — toggle Fabric mods on/off (moves files between `mods/` and `mods-saves/`); detects byte-for-byte conflicts
- **Worlds** — save, load, and delete world backups as `.tgz` archives; autosaves before loading, and optionally on every stop
- **Server** — start/stop the server (from a jar with a memory setting, or from the game's own start script), download Fabric jars, view MOTD, server icon, port, Geyser's Bedrock port if it's installed, and the host's public IP; a per-server policy for starting it when the panel starts (never / always / unless it was stopped on purpose) and a checkbox to back up its world whenever it stops

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

### VibePanel remembers your servers

You only need to name your sessions once. `--session` **declares** which servers you
have, and VibePanel writes that list to `vibepanel-state.json` in its own working
directory, so the next run is simply `python server.py`:

```bash
python server.py --session survival --session creative   # first time
python server.py                                          # every time after
```

To forget a server, just stop passing it. To swap the set, declare the new one.

Alongside each session it remembers **where that server lives**, which it works out
by watching rather than by being told. While a server is stopped it notes the pane's
current directory as a best guess; the moment it sees the server actually running it
takes that process's own working directory, which is the game directory by
definition, and trusts it from then on. Once VibePanel knows where a server lives it
can reopen that tmux session for you — after a reboot, or if something kills it —
with a shell already in the right place.

It only ever opens a shell. Starting the server is yours to do, unless you ask for
it explicitly:

### Jar, or your own start script

The **Server** tab offers two ways to start, and you pick one:

- **Server jar** — choose a `.jar` from `server-jars/` and a memory figure, and
  VibePanel runs `java -Xmx… -Xms… -jar … nogui`.
- **Custom start script** — name a script that lives in the server directory, and
  VibePanel runs `./that-script` from there. Memory and every other flag come from
  the script; VibePanel doesn't add any.

The script has to be an executable ordinary file sitting directly in the server
directory: no slashes in the name, and no pointing outside it. The name box
suggests what's there. Whichever you used last is what the page comes back to, and
what starts it at boot if you've asked for that.

### Starting a server automatically

On the **Server** tab there's a checkbox: *Start this server when VibePanel starts*.
It's off by default and it's per server.

Ticked, that server starts every time the VibePanel process starts — after a reboot,
after `systemctl restart vibepanel`, whenever. There's no cleverness behind it: it
does not try to work out whether the server was running before, or why the panel
started. It repeats whatever that server last started with — the same jar and memory,
or the same start script — and it won't start a second copy of one that's already up.
Unticked, nothing ever happens.

Whatever it does, it says so in the log (`journalctl -u vibepanel`).

## Configuration

Most people need none of these — session names and game directories are remembered.

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--session` | `TMUX_TARGET` | `minecraft` | tmux target; repeat for several servers. Declares the set and is remembered |
| `--port` | — | `8080` | HTTP port |
| `--state-file` | `STATE_FILE` | `./vibepanel-state.json` | where VibePanel keeps what it remembers |
| `--jars-dir` | `JARS_DIR` | `server-jars` | where downloaded `.jar` files are stored |
| `--worlds-dir` | `WORLDS_DIR` | `world-saves` | where world `.tgz` backups are stored |
| `--mods-dir` | `MODS_DIR` | `mods` | active Fabric mods directory |
| `--mods-saves-dir` | `MODS_SAVES_DIR` | `mods-saves` | inactive mods directory |
| `--server-dir` | `SERVER_DIR` | *(none)* | fallback game directory for a session VibePanel has never seen; normally learned instead |

All paths are relative to the Minecraft server's working directory (auto-detected from the tmux pane).

## Running as a service

A systemd unit file is included. Edit `vibepanel.service` to set `User`, `WorkingDirectory`, and your `--session`, then:

`WorkingDirectory` is where VibePanel keeps what it remembers, so it needs to be
writable by `User` and to stay put. `enable` (not just `start`) is what makes any of
the boot-time behaviour happen at all.

```bash
sudo cp vibepanel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now vibepanel
```

## How it works

VibePanel attaches to your existing tmux pane and interacts with it directly — it sends keystrokes to start/stop the server, issues commands like `/list`, and streams pane output to the browser. It does not run the Minecraft server itself.

Server-running detection uses `ps -t <pane_tty>` rather than checking the foreground process name, so servers launched via wrapper scripts (`bash start.sh`) are detected correctly.

Player edits take one of two routes depending on whether the server is up. While it's running they go out as console commands (`op`, `whitelist add`, `ban`, …) because the server owns those json files in memory and would overwrite anything written behind its back. While it's stopped VibePanel edits `whitelist.json`, `ops.json`, and `banned-players.json` directly. The UI says which mode it's in.

Nothing about player management reaches the network: UUIDs come from the json files, from `logs/latest.log`, or from whatever the admin pastes into the UUID field. There is no account lookup service involved.

The panel only ever calls out for three things: downloading a Fabric jar, checking the latest Minecraft version, and looking up the host's public IP via `api.ipify.org`. That last one happens once at startup, not per page view; if it fails, the Server page simply shows no IP.

## License

MIT
