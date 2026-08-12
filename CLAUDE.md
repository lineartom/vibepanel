# CLAUDE.md

## Running the dev server

```bash
python server.py                                        # defaults: 0.0.0.0:8080, tmux session "minecraft"
python server.py --port 5000 --session mc               # custom port and session name
python server.py --session survival --session creative  # multiple servers; shows tab switcher
```

All config can also be set via environment variables (see below).

`SERVER_DIR` is the one configured path that is made absolute at startup, because
it lands in a `cd` that runs *inside the tmux pane* — where the working directory
is the game dir, not the panel's — so a relative value would resolve against a
base the admin never chose. The `*_DIR` settings stay relative: they are joined
onto the game dir with `os.path.join`, which still accepts an absolute override.

## CLI flags / environment variables

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--session` | `TMUX_TARGET` | `minecraft` | tmux target; **repeat** for multiple servers: `--session mc1 --session mc2` |
| `--jars-dir` | `JARS_DIR` | `server-jars` | dir (relative to game dir) where .jar files live |
| `--server-dir` | `SERVER_DIR` | *(none)* | cd here before starting the server; resolved to an absolute path (and `~` expanded) at startup |
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

## Pane width, and why `capture-pane` needs `-J`

The panel never attaches — every tmux call is a one-shot subprocess, so we are
never a client and never influence the pane's size. It is whatever tmux decided:
the attached admin's terminal, or `default-size` (80x24) for a session created
detached and never attached.

That matters because `capture-pane` returns *display* lines, already hard-wrapped
at the pane's width. Without `-J` this silently truncates readback: `list` answers
on one line naming every player online, and on an 80-column pane a 10-player
server parsed as the single name `Not` while the header still read 10. `-J`
rejoins the pieces. It also preserves trailing spaces, which `-p` alone strips, so
`tmux_capture()` re-strips them to keep the console view as it was.

Note tmux does not reflow scrollback on resize, so one capture can still mix
lines wrapped at the widths in effect when they were written.

## Server running detection

`_is_running()` / `_pane_java_info()` use `#{pane_tty}` + `ps -t <tty> -o pid=,args=` to find a `java` process on the pane's tty. This works regardless of process tree depth — a server started as `bash start.sh` (where java is a grandchild of the shell) is detected correctly. Do **not** use `#{pane_current_command}` for this; it only returns the foreground process group leader name, which is `bash` in the wrapper-script case.

### …unless the server is started through su/sudo

The tty is inherited by descendants, but a privilege wrapper severs exactly that.
Measured with util-linux 2.41 and sudo 1.9 (a real tmux pane, java on `pts/0`):

| started as | where java ends up |
|---|---|
| `su mc -c 'java …'` | `tty ?` — su calls `setsid()`, so java has no controlling terminal |
| `su --pty mc -c 'java …'` | `pts/2` — a pty of its own |
| `sudo -u mc java …` | `pts/1` — sudo's `use_pty` |

In every case `ps -t <pane_tty>` shows only the wrapper, so the panel reported
Stopped for a server that was plainly up. The fallback: a `su`/`sudo`/`doas` on
the pane's tty **that was handed a command** counts as the server, and the jar
name is dug out of the wrapper's own arguments when it's there. This is crude on
purpose — we take the wrapper at its word rather than walking process trees or
parsing `/proc` for another user's children, which `hidepid` can hide anyway.

`_wrapper_runs_a_command()` is what keeps it from being *too* crude: an admin
sitting in `su - mc` or `sudo -i` is a shell, not a server, and counting one
would peg the panel at Running with no way to start anything. `su` needs `-c`;
`sudo`/`doas` need a trailing command and no `-i`/`-s`.

Seeing a real `java` always wins over the wrapper, so the jar reported for a
normally-started server is unchanged.

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
command; ban reasons go through `pane_text()` — see the section below.

### Log scraping for add-suggestions

`_scan_latest_log()` pulls `UUID of player <name> is <uuid>` lines out of the last
512 KB of **`logs/latest.log` only**. Rotated logs are ignored, `.gz` and plain alike:
stitching the tails of several files together would mean searching a history with holes
in it, which is both slower and confusing to reason about.

Log lines carry only `[HH:MM:SS]`, so dates are reconstructed: within one `latest.log`
the clock runs forward (a restart rotates the file away), so each backwards jump is a
midnight rollover. Count them, then date each hit by counting back from the file's
mtime, which is the date of its last line.

## Geyser's Bedrock port

`/api/server/identity` reports `bedrock_port` from `config/Geyser-Fabric/config.yml`
when that file exists, and the Server page shows it beside the Java port. Most
servers have no Geyser, so a missing file is the normal case, not an error — the
field is simply absent.

`_read_bedrock_port()` scans for the key instead of parsing YAML: Flask is the only
runtime dependency and one integer does not justify a second. The scan is scoped to
the top-level `bedrock:` block, because the same file's `remote:` block also has a
`port:` — the Java server Geyser forwards to — which is emphatically not the port a
Bedrock player types in.

## Directory layout (relative to game dir)

```
<game-dir>/
  server-jars/         # .jar files for starting the server  (JARS_DIR)
  mods/                # active Fabric mods                   (MODS_DIR)
  mods-saves/          # inactive/stashed mods                (MODS_SAVES_DIR)
  world-saves/         # .tgz world backups                   (WORLDS_DIR)
  logs/latest.log      # scraped for player name→UUID suggestions (read-only)
  config/Geyser-Fabric/config.yml   # bedrock.port, if Geyser is installed (read-only)
  whitelist.json       # read + written by the Players page
  ops.json             #   "
  banned-players.json  #   "
  get-me-fabric.sh     # auto-installed from repo root if missing
  .vibepanel.json      # panel state: last-used jar per session (written on start/stop)
```

## Multiple sessions share one DOM — two rules

Every page exists once in `index.html` and is reused by all sessions; switching
servers only changes `currentSession` and re-fetches. That makes cross-session
bleed the easiest bug to write in this codebase, so:

**1. Session-scoped requests go through `sessionJson()`, and callers bail on `STALE`.**
`api()` stamps the session at *request* time, but rendering happens whenever the
reply lands — and `/api/players` sleeps ~0.8 s server-side, so switching servers
mid-request is routine, not a rare race. `sessionJson()` records `sessionEpoch`
when it fires and returns the `STALE` sentinel if the epoch moved by the time the
reply arrives; the caller must `return` without touching the DOM. Note the action
itself already went to the right server — only the UI feedback is discarded.
Overview fetches build their own `?s=` URLs and write into `overviewCache[session]`,
so they deliberately do *not* use this and must keep rendering after a switch.

**2. Anything that renders a per-session result gets cleared in `resetSessionUi()`.**
Feedback lines, output blocks, drafts, and list contents all persist across a switch
otherwise. This is not only cosmetic: the mods conflict notice carries a live
"Delete Both" button, and left on screen it would delete the *new* server's files
for a conflict raised on the old one. When adding UI that holds a result, add it
to `resetSessionUi()` in the same commit. Same goes for in-flight guard flags —
`loadingPlayers` is reset there, otherwise a load still running for the old server
makes the new one skip as "already loading" and the page sticks on Loading….

Per-session data that should survive a switch (rather than be cleared) is keyed by
session name — see `sayHistory`, so a broadcast sent to one server reappears under
that server's tab and never under another's.

## Anything typed into the pane must be inert as a shell command

`tmux_send()` types text at whatever is reading the pane. That is *usually* the
Minecraft console, but the server may have stopped a moment earlier — and we
accept that race rather than trying to close it — so a shell may be reading
instead. `say hi; rm -rf ~` typed at a shell runs `rm`. This is not theoretical;
it was verified with canary files before being fixed.

So: **free text goes through `pane_text()`**, which removes control characters
(they reach the foreground process through the pty and can signal it) and the
shell metacharacters `` ` $ ( ) ; & | < > \ ' " ! ``. Two endpoints supply free
text — `/api/say` (message) and `/api/players/ban` (reason). Everything else we
send is either a constant (`list`, `stop`), a value matched against a strict
pattern (player names `[A-Za-z0-9_]{1,16}$`, memory `\d+[MG]`), or — better still
— a value *selected from the filesystem* rather than validated: the jar to launch
must be one of the entries `_list_jars()` just read out of the jars dir, so the
request picks an entry and never contributes to the path.

Use `re.fullmatch`, not `re.match`, for these patterns. Python's `$` also matches
just before a trailing newline, so `re.match(r'^\d+[MG]$', "1024M\n")` succeeds —
and a newline reaching `send-keys` is typed as Enter, ending our command line and
running whatever follows it. Today `.strip()` removes it first; `fullmatch` means
the safety doesn't depend on that staying put.

That set is wider than the strict minimum — with the separators gone, a lone `(`
only yields a bash syntax error — but the safety of the narrower list depends on
our line always beginning with `say `/`ban `, which is a fragile thing to rely on.
Prefer the blunt version.

The cost is that apostrophes, `!` and brackets are dropped from chat, so
`/api/say` returns the text it actually sent as `sent`, and the UI records *that*
in its history and says "some characters removed" rather than silently
misreporting what was broadcast.

`/api/server/start` is the exception that is genuinely meant for a shell: it uses
`shlex.quote()` on the jar path and `SERVER_DIR`, both of which come from the
pane's CWD or config rather than from us. That also makes paths containing spaces
work, which they previously did not.

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
