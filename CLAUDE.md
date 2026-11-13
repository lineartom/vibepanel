# CLAUDE.md

## Running the dev server

```bash
python server.py                                        # defaults: 127.0.0.1:8080, tmux session "minecraft"
python server.py --port 5000 --session mc               # custom port and session name
python server.py --session survival --session creative  # multiple servers; shows tab switcher
python server.py                                        # ...and after that, just this
```

All config can also be set via environment variables (see below).

Three words are used consistently throughout the code and these notes: a **server**
is one game instance, a **session** is the tmux session it lives in, and the
**panel** is the VibePanel process.

`SERVER_DIR` is the one configured path that is made absolute at startup, because
it lands in a `cd` that runs *inside the tmux pane* — where the working directory
is the game dir, not the panel's — so a relative value would resolve against a
base the admin never chose. The `*_DIR` settings stay relative: they are joined
onto the game dir with `os.path.join`, which still accepts an absolute override.
`PANEL_STATE_FILE` is absolute for a related reason: it is resolved against the
CWD *at import*, which is the panel's working directory, so nothing that chdirs
later can move the store.

## CLI flags / environment variables

| Flag | Env var | Default | Purpose |
|---|---|---|---|
| `--session` | `TMUX_TARGET` | `minecraft` | tmux target; **repeat** for multiple servers: `--session mc1 --session mc2`. **Declares** the expected set and is remembered — see below |
| `--state-file` | `STATE_FILE` | `./vibepanel-state.json` | the panel store; relative to the panel's own working directory |
| `--jars-dir` | `JARS_DIR` | `server-jars` | dir (relative to game dir) where .jar files live |
| `--server-dir` | `SERVER_DIR` | *(none)* | **fallback** game dir for a session the panel has never seen; normally learned instead. Resolved to an absolute path (and `~` expanded) at startup |
| `--worlds-dir` | `WORLDS_DIR` | `world-saves` | dir for world .tgz backups |
| `--mods-dir` | `MODS_DIR` | `mods` | active mods directory |
| `--mods-saves-dir` | `MODS_SAVES_DIR` | `mods-saves` | inactive (stashed) mods directory |
| `--host` | — | `127.0.0.1` | bind address. Loopback by default: there is no login and no TLS, so being reachable from anywhere else has to be something the admin asked for — `install.sh` passes the host's Tailscale address |
| `--port` | — | `8080` | bind port |
| `--allowed-host` | `ALLOWED_HOSTS` (comma-separated) | *(none)* | a DNS name the panel is reached by, when that is neither this machine's hostname nor its Tailscale name (both are found without being told); **repeat** for several. See "Who may talk to the panel" |

## The panel never runs as root

`_refuse_root()` is the first thing `__main__` does: with an effective uid of 0 it
prints why and exits 78, which `vibepanel.service` lists under
`RestartPreventExitStatus` so systemd does not retry a refusal every five seconds.

Everything the panel does, it does as whoever it runs as — and several of those
things take their instructions from the game dir. Download Fabric *executes*
`<game-dir>/get-me-fabric.sh`; Worlds → Load unpacks an archive from there; the
Players page writes json there. As root, each is a way for anyone who can write to
the game dir to become root, and "anyone" includes a mod running inside the server.
Nothing the panel does needs root: a server running as another user is reached by
typing at a pane, which takes no privilege at all.

So the su/sudo support below is for a server that runs as a different *unprivileged*
user from the panel, and its limits are stated as limits rather than worked around:
the panel cannot read such a server's `cwd` or its heap counters, and does not try
to acquire the means to.

A panel that is not root but is still more privileged than the game dir's owner has
the same exposure in miniature. That is treated as the admin's deliberate choice and
is not checked for.

## tmux session detection

VibePanel attaches to a tmux pane and reads/writes to it:

- If `--session` names a session that exists, that session is used.
- If the named session is not found **and there is exactly one tmux session visible**, that sole session is adopted automatically (useful when the user hasn't named their session) — **unless the store already knows that session's directory**, in which case we can recreate it ourselves and adoption would repoint at the wrong server.
- If it is still not found and we know where it belongs, `_ensure_session()` creates it — see below.
- If no tmux is reachable at all, status endpoints return 503.

The "game directory" is resolved from the **foreground process group's CWD** inside the pane, not the tmux session's startup directory. This is done via `/proc/<shell_pid>/stat` (tpgid field) + `/proc/<tpgid>/cwd` on Linux, with a fallback to `#{pane_current_path}` on macOS/other.

## The panel store

There are **two** state files, split by who owns the facts:

| state | file | why there |
|---|---|---|
| expected sessions, each one's `dir` / `dir_confirmed`, `start_policy`, `backup_on_stop` | `vibepanel-state.json` in the **panel's** working directory | panel configuration — and a session's directory cannot live inside the directory it identifies |
| `last_jar`, `last_mem`, `last_mode`, `last_script` | `.vibepanel.json` in each **game** dir | facts about that particular game, which travel with it |

The store is held in memory and **flushed only when something changes**
(`_update_session_state()` returns whether it did): a status poll that re-observes
the same thing must not rewrite the file every five seconds. Writes take
`_PANEL_LOCK` — `app.run(threaded=True)` and there are now several writers — and use
the same atomic `.tmp` + `os.replace` as the game-dir file. A directory we cannot
write to prints once and the panel carries on from memory; a panel that cannot
persist is still a working panel, one that refuses to start is not.

`--session` **declares** the expected set rather than adding to it: passing it
replaces what was stored, which is what gives an admin a way to forget a session —
stop passing it. Passing nothing reuses the last declared set, so every run after
the first is just `server.py`. `_set_expected_sessions()` drops the entries for
sessions no longer named, directory and all: keeping a stale directory around to be
silently re-adopted later is worse than re-learning it.

### Learning a session's directory, and when to believe it

`tmux_pane_path()` reports the CWD of the pane's **foreground** process, and what
that is worth depends entirely on what is in the foreground:

| | foreground process | worth |
|---|---|---|
| server stopped | the admin's shell, which may be anywhere | a guess — see the empty-roster note below, this is the classic wrong-dir trap |
| server running | the server itself | a fact: its CWD **is** the game dir |

So `_observe_session()` records a directory plus how much we trust it. It is called
from `/api/server/status` — which every page polls, for every session, so it sees
the stopped → running edge — and once per session at startup.

- **stopped → running**: read the server's CWD and store it `dir_confirmed: True`.
- **stopped, dir absent or unconfirmed**: store the pane's CWD, `dir_confirmed: False`.
- **stopped, dir already confirmed**: do nothing.

A confirmed directory is **never** downgraded by a wandering shell. An admin who
`cd`s out of the game dir with the server down is not evidence against something we
watched java do, and letting that un-learn it would throw away the only observation
we were sure of.

Two things the edge handling gets right on purpose:

- **Prefer `/proc/<java_pid>/cwd`** (`_running_game_dir()`). `_pane_java_info()`
  already returns `pid`, and reading java directly beats the pane for the
  `bash start.sh` case, where the foreground group leader is the wrapper script.
  Falls back to `tmux_pane_path()` when that read isn't available (`hidepid`,
  another user, no `/proc`).
- **Never learn from the pane in the su/sudo case.** The pane's foreground process
  there is the privilege wrapper, whose CWD is wherever the admin's shell was, and
  recording it would poison the store with a confident-looking wrong path. That
  used to be expressed as "`pid` is `None`, so return"; now that `_java_under()`
  finds the JVM through `/proc`, `pid` often isn't, so the rule is carried by
  `wrapped` instead — `_running_game_dir(..., allow_pane=False)`. A wrapped server
  either gives up its own `cwd` — it turned out to run as the panel's own user — or
  teaches us nothing. Another user's `cwd` is not ours to read, and the panel is
  never root.
  Reading its **command line** is a separate question, and that we do — see the
  su/sudo section.

Only the *edge* triggers a read; a server cannot change its own CWD, nor rewrite
the command line it was started with, so re-reading `/proc` on every poll would
buy nothing. This is deliberately **not** folded into `_pane_java_info()`, which
stays a pure query — it is a side effect of *serving status*, the same shape as
`_record_peak()`.

### Creating a missing session

`_ensure_session()` opens a session that isn't there, with `new-session -d -c <dir>`
so its shell starts in the right place. It runs at startup for every expected
session, and again from `/api/server/status` whenever the pane turns out to be
unreachable — that endpoint is what every page polls, so it is where a session
killed mid-life gets noticed. Hanging the recreate off the *failure* path rather
than a `has-session` check up front keeps the ordinary case free.

The directory is the store's learned `dir`, falling back to `SERVER_DIR`, and
**nothing is created when neither is known**. That condition is the whole design:
a session opened in the wrong place is worse than no session, because the panel
would look healthy while listing someone else's files.

**It returns `"exists"` / `"created"` / `"failed"`, not a bool**, and the difference
between the first two decides how a caller may resolve the game dir — that is what
`_game_dir(target, ensured)` is for. On a session we just created the game dir is
the directory we passed to `-c`, *by construction*. Asking `tmux_pane_path()` to
tell us back would be both a round trip to rediscover what we just asserted and a
race: `new-session -d` returns before the child has chdir'd and exec'd, and
`/proc/<tpgid>/cwd` inside that window is the **tmux server's** CWD. Nothing raises
— it is a plausible wrong path, which then reads an absent `.vibepanel.json`,
gets `{}`, and leaves the start policy quietly not firing with nothing saying why.

Before typing into a pane we just made, `_wait_for_shell()` polls until the pane's
foreground process group is the shell itself (`tpgid == pane_pid`), bounded at 2 s.
A readiness check rather than a magic sleep; anything unexpected reads as ready,
since waiting forever is worse than typing early.

What gets created is a **plain shell, never a server** — the panel does not decide
on its own to run a jar. `/api/server/start` therefore no longer creates a session
with the java command as its process. That path was unreachable anyway (resolving
the game dir above it already requires a pane), and it built a session whose life
was tied to the server's: stopping the server took the pane with it, and the panel
reads that pane afterwards.

## Starting a server, and starting one at boot

`_start_server()` is the single start path — the Start button and the startup policy pass
both come through it, so what happens at boot is exactly what happens on a click. It
raises `StartError`, which carries the status the endpoint should return.

The `cd` goes to **`gdir`, the same directory the jar was just listed from**, so the
server's CWD and its jar can never disagree. It used to go to `SERVER_DIR`, which
only lined up when the pane already happened to be there; with the pane elsewhere it
ran a jar from one game dir with the working directory of another. The two callers
supply `gdir` differently, and the difference is the point:

- **the endpoint** passes `tmux_pane_path()` — if an admin `cd`s the pane to a
  different game and presses Start, they get *that* game. The store is for creating
  a session, not for overriding a live one.
- **the startup pass** passes the stored `dir`, because there is no admin at a pane to
  follow.

### What the panel remembers about the last run

`_start_server()` records the whole form on the way out — mode, and then either
jar + `last_mem` or script — so a stop that happens *outside* the panel (console
`stop`, a crash) still leaves a usable default, and the startup pass knows what to
launch and with how much.

The other two writers read the same pair off the **running process** instead,
via `_java_jar()` / `_java_mem()` on its argv, and exist for the server the panel
did not start — typed at the pane by hand, or brought up by something else
entirely. There the process is the only account of what was chosen:

- **`_observe_session()`, on the stopped → running edge.** The moment the panel
  can see what is running is the moment to write it down; waiting for the stop
  would lose it to a crash, and `/api/server/status` carries `jar` and `mem` so
  the page can show them while it runs.
- **`/api/server/stop`**, which re-reads immediately before sending `stop`.

Both go through `_remember_run()`, which drops whichever field it could not read,
so an unparseable command line leaves the previous value standing rather than
blanking it. Both also refuse to write into a directory the panel only *guessed*
(`_confirmed_dir()`): recording one game's habits into what may be another game's
directory is worse than not recording them. The stop writer used to write to the
pane's CWD instead, which for a server behind su/sudo is the wrapper's and for one
whose start script `cd`s is the script's — it now calls `_observe_session()` first,
so a server nobody has polled since it came up is confirmed there and then, and the
stop it is about to cause is seen as a running → stopped edge.

`_java_mem()` returns only what the start form can express (`-Xmx4096m` →
`4096M`); a `-Xmx` in kilobytes, in bytes, or replaced by `MaxRAMPercentage`
yields nothing. Whatever comes back is remembered and typed straight into the
memory field, so a figure `_start_server()` would then refuse is worse than no
figure at all — the admin would get a box that looks filled in and a Start that
says "Invalid memory value".

Neither reader touches `last_mode`. A script-started server whose script sets
`-Xmx` refreshes the jar form's defaults underneath, but the page still comes
back on the script form it was last used on, and `_start_plan()` still runs
the script.

On the client, both edges of a run call `reloadStartForm()`, which is
`loadJars()` with `jarsLoaded` **and `selectedJar`** cleared. Clearing both is
the point: `loadJars()` loads once per visit and treats an existing
`selectedJar` as the admin's own choice, so re-fetching alone would leave the
old pick highlighted next to a status line naming a different jar. There is no
choice to protect on either edge — the whole form is inert while the server runs.
The running edge fires on `wasRunning === false`, a real transition, not on the
`null` of a first load: `srvStartPolling()` already calls `loadJars()` there, and
the startup observation has already put the values in the store by then.

**The edges are measured against `srvLastRunning`, the Server page's own record of
what it last drew — not the shared `serverRunning`.** Whether the server is up needs
no edge at all; every poll redraws that from the answer in hand. The edges exist
only as the cue to re-fetch three things that are loaded once rather than per poll:
the start form and the two policy notes. `serverRunning` cannot be that yardstick
because every caller of `fetchServerRunning()` writes it, including the 15 s poller
that keeps the *other* pages' controls honest; when that one saw a transition first,
this page compared the new state with itself and the cue was never sent — about one
transition in six. `srvLastRunning` is read and replaced in one step after the
`await`, so of two overlapping polls exactly one sees a given transition; it is kept
across page visits, so a stop that happened while the admin was elsewhere still
counts on the way back; and it is cleared only on a session switch.

**A pressed Stop is remembered too, in `srvStopping`.** A stop takes as long as the
world takes to save, and the server is running for all of it — so every poll in
between redraws the card, and a card drawn from the status alone carries a fresh,
live Stop button. The button went inert on the click, came back at the next poll,
and vanished with the server. That the admin clicked is something only the page
knows, so `stopServer()` records it and the redraw draws "Stopping…" while it
holds. It is keyed by session, the `sayHistory` rule rather than the
`resetSessionUi()` one: the stop is still under way on the server that was left.
It ends when the server is seen stopped, when the request is refused, or after
`SRV_STOPPING_MAX_MS` (60 s) — a JVM that never goes down must not leave the admin
with nothing to press. A `stop` typed at the console never showed this, having no
click to forget.

### Two start forms: a jar, or the game's own script

Plenty of servers are launched by a `start.sh` of their own — for JVM flags the
panel has no field for, a pre-launch backup, a restart loop. Those cannot be
described by "a jar and a memory figure", so `_start_server()` takes a `mode` of
`"jar"` or `"script"` (`_START_MODES`) and the Server page offers the two as
radio buttons, one set of fields live at a time. Memory belongs to the jar form
only: a script owns its own flags, and a `-Xmx` box beside it would be a lie.

**`mode` decides, not which field arrived populated.** The UI sends both every
time — an admin who names a script still has a jar selected underneath — so
picking "whichever one is filled in" would start the wrong thing the first time
somebody switched forms without clearing the other. It is also what the store
remembers (`last_mode`), so the page comes back on the form it was last used on.

The jar path never trusts the name it is given: it matches the request against
what `_list_jars()` just read off disk and uses *that* entry. A script name
cannot work that way — the admin types it, and may type one created a second
ago — so `_resolve_start_script()` checks every claim the name makes instead:

| check | why |
|---|---|
| no `/` or `\`, and not `.`/`..` | it is a filename, not a path; nothing is normalised into something that might resolve |
| no control characters (`_has_control_chars()`) | the line is typed at a pane, where a control character is a *key*: a newline arrives as **Enter**, `\x03` as Ctrl+C — abandoning our line and leaving the rest to be read as a new one. `shlex.quote()` keeps the shell happy with either, which is exactly why quoting cannot be the only check |
| `dirname(realpath(...)) == realpath(gdir)` | the *resolved* path must sit in the game dir, so a symlink pointing out of it is refused too |
| `isfile()` | directories, fifos and sockets are out |
| `os.access(X_OK)` | we run it as `./name`; without `+x` that is a shell error in a pane the admin then has to go and read |

It then runs as `cd <gdir> && ./<name>`, both parts `shlex.quote()`d, so a script
with a space in its name works and the script gets the working directory it is
entitled to assume.

The same check covers everything else on that line. `_list_jars()` leaves a jar
whose name holds a control character off the list — and since the list is the gate,
what is not listed cannot be started — and `_start_server()` refuses a game
directory path that holds one. Neither is reachable without write access to the game
dir, which is why they were missing; they are there because "selected from the
filesystem" makes a name trustworthy as text, not as keystrokes.

`_list_scripts()` fills the name field's `<datalist>` with the game dir's
executables. It is a **suggestion** list, not the gate — a name typed by hand is
equally acceptable — but every entry is put through `_resolve_start_script()`
anyway, so the list can never offer something Start would then refuse.

### The start policy

Per-server, on the Server tab, three radio buttons, stored as `start_policy` in the
panel store and **defaulting to `never`**:

| | what the panel does when the panel process starts |
|---|---|
| `never` | nothing, ever. The default, and where an unrecognised stored value lands |
| `always` | starts it, whatever happened last time |
| `unless-stopped` | starts it unless the last run was asked to stop — see below |

It lives in the panel store rather than the game dir precisely so that reading it
never depends on resolving a directory first, which is the condition the startup
pass runs under. `_start_policy()` whitelists on the way out, like `_last_mode()`: a
store that was hand-edited, or written by a newer panel, must never read as
something *stronger* than the admin asked for.

The old boolean `autostart` is migrated in `_load_panel_state()` — `true` → `always`,
`false` → `never` — and the old key is **popped**, not left beside the new one. This
file is the quickest way to see what the panel believes, and two keys that can
disagree destroy that. No write is needed there: `_set_expected_sessions()` runs
moments later and flushes unconditionally, so the new shape lands on disk for free.

`_start_plan()` walks the chain **panel store → dir → `.vibepanel.json` → jar+mem or
script** and reports a broken link as itself: "we have never seen this session" and
"its disk did not come back after the reboot" are different problems with different
fixes, and a blank field would say neither. Every outcome prints, `never` included —
one line per session makes the panel's own log a complete account of what it decided
at boot, which is what somebody who finds a server running (or finds one that did
*not* come back) needs to read.

It reports `problem` and `reason` separately because they answer different
questions: `problem` is a broken link in that chain, something to go and fix, while
`reason` is the policy's account of the decision. A server set to `never` still
reports a missing jar — worth fixing before the admin changes their mind.

Only `problem` reaches the page. `reason` is a diagnostic — it is what the boot log
prints, and `vibepanel-state.json` holds the rest — and on the card it was noise:
each option is a whole sentence that already says what the policy does, so a line
restating it earned nothing. The endpoint still returns `reason` and `would_start`,
which is where to look when a server does not come back.

The three options are full-width rows in the jar picker's shape (`.policy-list` /
`.policy-item`, sharing `.jar-list`'s container rule) rather than a labelled row of
radios. The width is the point: it is what lets each option explain itself — "Never
start automatically", "Always start automatically on boot", "Only start automatically
if we weren't stopped on purpose" — which in turn is why there is no heading, since a
label would have nothing left to add. The whole row is the hit target, and the whole
row goes green, via `:has(input:checked)` rather than a JS-toggled `selected` class so
the highlight cannot drift out of step with the control it describes — including in
the moment before a rejected write is rolled back.

`would_start`, not `will_start`: the plan deliberately does not consult whether the
server is running. That check belongs to `_start_policy_pass()` alone, and asking
tmux inside the plan would spend a round trip on every page load answering a
question the page never asks — as well as making the GET endpoint and the boot pass
capable of disagreeing.

In script mode the last link is **re-checked, not just read back**: the plan runs
the remembered name through `_resolve_start_script()`, so a `start.sh` that lost its
`+x` or was renamed shows up as a problem on the Server page now, rather than as a
server that quietly fails to come back after the next reboot.

The one check in `_start_policy_pass()` that is **not** policy is `_is_running()`:
`systemctl restart vibepanel` on a healthy host must not type a second JVM into a
running server's pane.

A tmux that cannot start at all would otherwise reprint its error on every poll,
so failures are logged once per session (`_AUTOSTART_FAILED`) and the note is
cleared as soon as the session exists.

### `unless-stopped`, and why it reads the game's log

The requirement is that it work **whether or not the panel's Stop button was used,
and whether or not any page was open to witness the stop**. That rules out the
obvious implementation — persisting an intent flag on the running → stopped edge in
`_observe_session()` — because `_LAST_RUNNING` is in memory, is wiped by a panel
restart, and only advances when somebody polls. A server stopped at the console with
no browser open produces no edge at all, and a host reboot destroys the memo along
with everything else.

So there is **no new persisted state**. The verdict is a pure function of the
evidence the stopped server left behind: the tail of its own `logs/latest.log`,
read by `_last_run_ending()`. Minecraft writes two different lines on the way down,
and they mean different things:

| line | where it comes from | what it means |
|---|---|---|
| `Stopping the server` | the `/stop` **command's own feedback** — lang key `commands.stop.stopping`, through `sendSuccess` | **somebody asked** |
| `Stopping server` | `MinecraftServer.stopServer()`, on the way out of the run loop | an orderly shutdown happened; says nothing about who |

A SIGTERM at host shutdown runs the shutdown hook, so it produces the second and
never the first. That is the whole discriminator, and being *content* rather than
timing is what makes it right for "stopped, then rebooted three hours later" — the
case no grace window can reach. It also means the Stop button needs no special
handling: it types the same `stop` a console user does, so both leave identical
evidence.

These four strings are the whole mechanism, so they were checked against the shipped
server jar rather than against memory or a decompiler mirror: `commands.stop.stopping`
is literally `Stopping the server` in `assets/minecraft/lang/en_us.json`, and
`stopServer()`'s `Stopping server` is a constant in `MinecraftServer.class`, in both a
current version and one five years older. Worth redoing if a future version moves them
— the failure would be silent, and it would look like every server suddenly crashing.
Note also that the log4j config is no longer in the server jar; it ships inside
`META-INF/libraries/com/mojang/logging/*/logging-*.jar!/log4j2.xml`.

| verdict | what it saw | what happens |
|---|---|---|
| `asked` | the strong line | stays down |
| `crashed` | it said it was dying — see below | starts |
| `orderly` | the weak line only | see below |
| `crash` | nothing at all — SIGKILL, an OOM kill, power loss | starts |
| `absent` | no `logs/latest.log` | starts; nothing says it was stopped |
| `unreadable` | there is one and we could not read it | stays down |

`unreadable` is deliberately separated from `absent`: a disk that did not come back
is not evidence that the admin wanted this server up, and collapsing the two would
restart a deliberately-stopped server on exactly the host with a problem.

**A crash is also an orderly shutdown, and that is the subtlest thing here.**
`runServer()` catches `Throwable`, writes the crash report, and then its `finally`
calls `stopServer()` — which logs the weak line unconditionally. So a heap OOM at
3am ends the log with `Stopping server` and is indistinguishable from a `kill`,
unless the wreckage above it is noticed. That cost a real bug: the panel refused to
restart a crashed server *and* told the admin a signal had done it. Hence
`_CRASH_MARKERS` and the `crashed` verdict, ranked **`asked` > `crashed` > `orderly`**:

| marker | who logs it |
|---|---|
| `Encountered an unexpected exception` | vanilla's fatal catch |
| `Considering it to be crashed, server will forcibly shutdown` | `ServerWatchdog`, on a hung tick |

`asked` outranks `crashed` because a run whose operator typed `stop` and *then* threw
on the way down was still asked to stop. And note the exit status cannot substitute
for any of this: vanilla **exits 0 after a crash** — there is no `System.exit` in
`MinecraftServer` — so a wrapper watching `$?` learns nothing.

An **orderly** shutdown nobody commanded is a signal, and one comparison says which:
if `mtime(latest.log) >= _boot_time()` the run ended during *this* boot, so something
killed it on a live host (`kill`, `tmux kill-session`, a service manager) — deliberate,
stays down. If it ended *before* this boot, it was the reboot's own SIGTERM — starts.
An unknown boot time resolves to staying down, because the surprising direction for
this feature is the panel starting a server the admin had stopped.

Three traps in the scan, all of them load-bearing:

- **Markers are matched against the message *body*, anchored.** `latest.log` carries
  chat, so a substring test fires on a player typing `Stopping the server` — which
  would let anyone on the server pin it down permanently. Split on the first `"]: "`
  (the field separator in vanilla, Paper *and* Forge's longer prefix); the strong
  line's optional bracket group admits the wrapped forms a non-console source
  produces, `[Rcon: Stopping the server]` and `[Notch: Stopping the server]`. The
  crash markers are safe under `startswith` for the same reason — chat always arrives
  prefixed, as `<Bob> …` or `/say`'s `[Server] …`.
- **The body may carry a `System chat: ` label, which `_log_broadcast()` strips
  first.** See the section below — it applies to the strong line (a player op's
  `/stop` broadcasts its feedback) but never to the other three markers.
- **`All dimensions are saved` is not a marker.** `/save-all flush` takes the same
  branch and backup plugins run it on a timer, so a crash after one would read as a
  deliberate stop.
- **The scan runs forward and the last verdict wins**, resetting at each
  `Starting minecraft server version`. `latest.log` normally rotates away at the next
  server start, but that is a log4j config an admin can change, and a stale marker
  from the previous run would otherwise report this run's crash as deliberate.

### The `System chat: ` label, and which lines wear it

As of **26.3** the game logs every message it *broadcasts to players* behind a
`System chat: ` label; 26.2 and earlier logged the bare text. So the same line has
two shapes depending on the version that wrote it:

```
[19:24:21] [Server thread/INFO]: msternow left the game                  ← 26.2
[19:24:21] [Server thread/INFO]: System chat: msternow left the game      ← 26.3
```

Both shapes are permanently in service and they turn up **in adjacent files of one
`logs/` dir** — a 26.2 run rotates out, the 26.3 run that replaced it rotates in —
so the readers strip the label with `_log_broadcast()` rather than picking a format.
Detecting the version per file would be worse: a rotated `.gz` need not contain the
`Starting minecraft server version` line at all.

Which lines wear it is exactly "is this broadcast to players":

| wears the label | does not |
|---|---|
| `joined the game` / `left the game` | `Stopping server` (`stopServer()`'s own log) |
| advancements and goals | `Starting minecraft server version` |
| the `list` answer | the crash markers |
| an op's command feedback, `[Bob: Teleported …]` | `UUID of player X is …` |
| `Stopping the server` **when a player op typed `/stop`** | …and it from the console |

The right-hand column is the server's own logger output, which never passes through
chat, so stripping is scoped to the readers of the left — notably *not* to
`_STOP_ORDERLY`, `_RUN_STARTED` or `_CRASH_MARKERS`.

**Stripping does not weaken the anti-spoofing rule, because the rule is the anchor
rather than the absence of a prefix.** What is matched after the label is still a
`fullmatch` (or an anchored regex), and a forgery's own prefix sits *inside* the
label and survives the strip: `System chat: <Bob> Alice joined the game` fails on
`<Bob> `, `/say` fails on `[Bob] `, command feedback on `[Bob: `. The residual is
unchanged too — an op with `/tellraw` can emit a system chat whose entire body is a
forged line, which was equally true of the unlabelled format.

This was found by the Activity page reporting a three-player, two-hour evening as a
single 5-second session: the only run in the sample logs old enough to be 26.2 was a
5-second visit, and the 26.3 run that held the actual evening parsed to nothing. Note
how quiet the failure was — a labelled log is not malformed, it simply contains no
events, which is indistinguishable from an idle server. `/api/players` happened to
survive it only because it reads the `list` answer with `re.search`.

What it gets wrong, and these are worth knowing rather than hiding:

- `kill -9` on the JVM by hand is indistinguishable from an OOM kill — it restarts.
- A host whose own unit does `ExecStop=tmux send-keys stop` before reboot is *telling
  us the stop was asked for*, and the rule believes it: the server will not come back.
  Use `always` there.
- A mod that issues `/stop` on a nightly schedule reads as deliberate. The game cannot
  tell us the difference between an admin asking and a scheduler asking.

And three ways `latest.log` can fail to be there — note that redirecting a start
script's output is **not** one of them, which this file used to claim. stdout is the
`SysOut` *console* appender; `logs/latest.log` is a separate `RollingRandomAccessFile`
appender on the same root logger, writing the file directly, so `./start.sh > out.log`
silences the console only:

- **java's CWD is not where we are looking.** `fileName="logs/latest.log"` resolves
  against the JVM's working directory, so a `start.sh` that `cd`s before exec'ing java
  puts it somewhere else. A *confirmed* dir is safe by construction — `_observe_session()`
  reads it from `/proc/<java>/cwd`, the very CWD log4j resolved against — which is one
  more reason `dir_confirmed` is worth carrying around.
- **A custom log4j config** (`-Dlog4j.configurationFile=`, common on modpacks and on
  hosts still carrying Log4Shell-era mitigations) can drop or rename the File appender.
- **We cannot read it**: a server running as another user under a `0700` game dir — the
  su/sudo case the panel otherwise supports — gives `PermissionError`, hence
  `unreadable`, hence stays down. The intersection of two supported features, and the
  residual most likely to be hit in practice.

### Why not a pidfile

The obvious cheaper idea, and it keeps coming back, so: add `--pidFile minecraft.pid`
to the launch line plus `; rm minecraft.pid`, and restart when the file survives. It
is a real vanilla *and* Paper option, and the game never deletes it (`writePidFile` is
one `Files.writeString`; there is no `deleteOnExit` anywhere near it), so the idea is
sound as far as it goes. It was rejected for what it *measures*:

`;` is unconditional, so the shell removes the file whenever it outlives java — a
clean stop and a crash alike. "Pidfile present" therefore means **the shell died with
the JVM**, i.e. the machine went away. That is a proxy for "was this stopped on
purpose" which fails in both directions: it stays down after an OOM-kill on a live
host, and comes back after a deliberate `tmux kill-session`. The case that settles it
is a crash at 03:00 followed by a reboot at 08:00 — the shell removed the file at
03:00, so the server never returns and the panel reports nothing wrong.

`&& rm` instead of `; rm` repairs the signal deaths (exit 128+n) but not the ones that
matter most: vanilla exits **0** after a Java-level crash, so an exception or a heap
OOM still reads as a clean stop, and it newly breaks "typed `stop`, then SIGKILLed a
hung JVM".

The deeper objection is coverage. A pidfile only exists if *the panel built the launch
line*, which excludes script mode, servers started by hand at the pane, and every
su/sudo server — the cases `_observe_session()`, `_remember_run()` and `_java_under()`
exist to serve — and it excludes them silently, a missing file being indistinguishable
from a stop. It also re-imports a persisted flag with no owner: a file left by a reboot
survives a hand-start and a hand-stop, and restarts a server the admin deliberately
shut down. Reading the game's own log is the same move as reading `/proc/<pid>/cmdline`
— take the evidence off the thing that was actually running.

(If some future need really is "did the shell outlive java", `--pidFile` is the wrong
tool for it anyway: `cd d && touch RUN && ./start.sh; rm -f RUN` gives the identical
signal, works in script mode, and can live in the panel's own directory keyed by
session. The PID number itself buys nothing.)

Scope is deliberately **panel startup only**. Nothing supervises a running server; a
crash at 3am stays down until someone starts it or the panel restarts. `_boot_time()`
reads `btime` from `/proc/stat`, falling back to `sysctl -n kern.boottime`, and is
cached — it cannot change while we run, and on macOS it costs a subprocess.

## Backing up the world when a server stops

The Server tab's second checkbox, **default off**, stored as `backup_on_stop`
beside `start_policy` and for the same reason: it is a standing policy about a
session rather than a fact about a game, and it has to be readable in the one
situation where it applies — a server that has just stopped — without that read
depending on anything the stopped server could have told us.

There is deliberately **one trigger**: the running → stopped edge in
`_observe_session()`. Not one in `/api/server/stop` and another for stops we
merely noticed — the Stop button only asks the console to stop, and what it
produces a moment later *is* that same edge, so hanging the backup off the edge
covers the button, a `stop` typed at the pane, a crash and a `kill` with one
piece of code. It also covers each of them at the only moment the world is
complete and no longer being written; backing up from the endpoint would tar a
world the server was still saving into.

The edge is seen when status is polled, which every open page does every 5 s (the
Overview's per-session fetches count too). With no page open nothing is observed
at all, so a server that stops overnight is backed up when someone next opens the
panel — late, but the world has been sitting still since, so it is the same
archive. What is lost is the timestamp's meaning, and `_STOP_BACKUPS` records the
real time. A panel restart clears `_LAST_RUNNING`, so a stop that happens across
one is never noticed and never backed up: no edge, no false archive.

`_stop_backup_pass()` requires a **confirmed** directory (`_confirmed_dir()`) —
this writes a gigabyte, and the rule about not writing into a directory we only
guessed applies most of all here. In practice the server running a moment ago is
exactly what confirms it; the case that survives is the su/sudo server whose `cwd`
we never got to read, and there the state says so rather than archiving whatever
world happens to be under the pane's shell.

The tar runs on its own thread — it is reached from `/api/server/status`, which
every page polls, and a world takes as long as it takes. `_STOP_BACKUPS` holds
`running` / `done` / `failed` / `skipped` per session, in memory only (it
describes this panel's run; read back after a restart it would claim a backup for
a stop nobody here saw), and rides along on the status poll the page is already
making. Every outcome prints, for the same reason the start policy's do.

Its note on the card carries that **state and nothing else** — what a backup is doing
or did, plus a `problem` if the setting cannot work. No explanation of what the
checkbox means: the label says it, and saying it twice buried the one line that has
to be read, "Backing up the world now…", which is why Start is held. The label says
"as an autosave" deliberately, because that is the consequence worth knowing rather
than a description of the mechanism — see the naming note below.

It is a `.policy-item` row like the start-policy options above it, full width and
green when set, even though it is a checkbox rather than a radio. Different control,
same kind of question — a standing policy about this session — so it reads as one
group instead of two unrelated widgets that happen to sit together.

While it runs, **Start and Worlds→Load are refused with 409**. Starting into the
directory would have the server writing what tar is reading, producing a corrupt
archive of a perfectly good world — the one failure this feature exists to
prevent — and Load deletes that world outright. The Start button disables itself
to match and the status card says why. A server started by hand at the pane can
still race it; nothing here can stop that.

Archives are named `world-<ts>-autosave.tgz`, the same as the autosave taken
before a Load, because they are the same thing: a backup the panel took without
being asked. That also means the Worlds page's **Delete all autosaves** sweeps
them, which matters — this fires on *every* stop, and a nightly-restarted server
accumulates one full world archive a day with no retention policy of its own.

### One thing at a time with a world

Save reads the world for as long as tar takes, Load deletes it and unpacks another,
and Start hands it to a JVM that begins writing at once. Any two together is a
broken archive or a broken world, and `_is_running()` does not prevent it: that
says what the *server* is doing, and these are the panel's own requests racing each
other on separate threads — two tabs, or two admins.

So all three take `_world_claim(session, …)` for as long as they run: Save around
the tar, Load around the whole autosave → delete → unpack, Start from its running
check to the keystroke. A second claim is **refused with 409, not queued** — the
page says what is in the way ("The world is being loaded — try again when that
finishes") and the admin retries, exactly as with a stop-backup. Save and Load put
their `_is_running()` check *inside* the claim, because Start holds the same claim
around its own check and keystroke; with the checks outside, each could pass before
the other acted.

The on-stop backup keeps its own state (`_STOP_BACKUPS`) rather than a claim: it
outlives the request that noticed the stop, and Save is deliberately allowed
alongside it, two readers being harmless. Start and Load check both.

Claims are in memory and per process, like `_STOP_BACKUPS`. What they do not cover
is what the panel cannot see: a server started by hand at the pane, and the moment
between Start's keystroke and the JVM appearing on the tty.

### `_archive_world()` and why it links rather than replaces

The one place a world archive is written: the Worlds page's Save, the autosave
before a Load, and the on-stop backup all come through it, so all three produce
the same shape of file in the same place.

**A symlinked world is followed, one level.** A world kept on another disk is a
symlink in the game dir, and tar handed a symlink archives the symlink — 248 bytes,
exit status 0, and a "backup" that restores nothing, which Save, the pre-Load
autosave and the on-stop backup all did. `_world_member()` hands tar `world/.`
instead when `world` is a link: the path resolves through it, while links *inside*
the world are still stored as links. Not `-h`, which follows every link there is,
including ones that lead out of the world or in a circle; and not a trailing slash,
which GNU tar strips before looking. Members are then named `world/./…` and unpack
to `world/` like any other archive. An ordinary directory keeps the plain `world`.

**Load unlinks a symlinked world rather than emptying it.** tar refuses to unpack
through a symlink, `rmtree` refuses to remove one, and what the link points at is
not the panel's to delete. So Load removes the *link*, leaves its target exactly as
it was, and the restored world is an ordinary directory in the game dir — the
response carries `unlinked` and the page says so. An admin who wants the world back
on the other disk moves it and re-links it; the panel will not guess at that.

Tar writes into a scratch file first, never under the archive's own name. A tar
that dies half way — the panel killed mid-backup, a full disk — must not leave a
truncated archive in the list looking loadable, because Load deletes the current
world *before* extracting.

**The scratch file has no name** (`_open_scratch()`): it is opened with `O_TMPFILE`
in the worlds dir and tar writes to it as stdout. Until it is linked into place it
exists only as an open descriptor, so the kernel frees it when the last one closes
— there is nothing to clean up after a panel that dies mid-backup, because there
was never anything there to find. That includes the awkward case `KillMode=process`
creates, where tar *outlives* the panel: it finishes writing a file nobody can
name, exits, and the space comes back. The previous hidden `.tmp` did not survive
that; each interrupted backup left a world-sized file that nothing listed, counted
or removed.

A named `.world-<pid>-<tid>.tgz.tmp` remains as the fallback where `O_TMPFILE` is
not on offer (macOS, NFS, some FUSE mounts). `_sweep_scratch()` runs before every
archive and removes any whose pid is no longer a live process — which also clears
out what earlier versions of the panel left behind.

The final name is then claimed with `os.link()`, not `os.replace()` — for the
nameless file by way of `/proc/self/fd/<n>`, which `link()` follows; that is the
documented way to give an `O_TMPFILE` a name. These names
are second-resolution and they do collide: an automatic backup can land in the
same second as a hand-pressed Save (it happened on the first test run of this
feature, and `replace()` silently destroyed the older archive). `link()` fails
instead, and the next free second is taken — bounded at 60 — so nothing is ever
overwritten. A filesystem without hard links falls back to `replace()` behind an
`os.path.exists()` check, which is the best a check alone can promise.

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

The `list` answer is then matched with `fullmatch` against the message body
(`_LIST_ANSWER_RE`, after the `"]: "` split and `_log_broadcast()`), not searched
for. The pane shows chat as well as the console's own output, so a `search` let a
player answer on the server's behalf by typing the sentence — the same forgery the
log readers are anchored against, arriving by a different route.

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
the pane's tty **that was handed a command** counts as the server.

`_wrapper_runs_a_command()` is what keeps that from being too crude: an admin
sitting in `su - mc` or `sudo -i` is a shell, not a server, and counting one
would peg the panel at Running with no way to start anything. `su` needs `-c`;
`sudo`/`doas` need a trailing command and no `-i`/`-s`.

*Whether* it is running is therefore taken on the wrapper's word. **What** it is
running is not: `_java_under()` walks `/proc` for a java process descended from
the wrapper and reads jar and heap off that command line. The wrapper's own
arguments describe the server only when the admin spelled the java line out at
the prompt — `sudo -u mc ./start.sh` says nothing at all — and they are kept
only as the fallback for when the walk comes back empty.

The walk is cheap and bounded: `/proc/<pid>/comm` filters to java processes
first, then `_proc_descends_from()` climbs the ppid chain (capped at 32 hops,
so a bad `/proc` read cannot loop). It runs only in the wrapper case, and only
on the stopped → running edge for the store's purposes. `_proc_ppid()` splits
`stat` **after the last `)`** — comm is parenthesised and may itself contain
both spaces and parentheses, which is the usual trap with that file.

`/proc/<pid>/cmdline` is world-readable, so this reaches another user's JVM from
an unprivileged panel; `hidepid=2` and macOS have no answer for it, which is why
None is an ordinary result rather than an error. Finding the JVM also yields a
real `pid`, so the heap card now works for an su/sudo server whenever the panel
can read that user's `hsperfdata` file — and says *which* of the two it couldn't
do when it can't.

What it does **not** yield is a directory: `cwd` is the one part of another
user's process that stays unreadable, so `wrapped: True` travels in the info
dict and `_observe_session()` passes `allow_pane=False` on the strength of it.
See the directory-learning section — falling back to the pane there would record
the privilege wrapper's CWD as the game dir.

Seeing a real `java` on the tty always wins over the wrapper, so nothing about a
normally-started server changed. What did change for every case is that argv now
comes from `/proc` when it can: `ps -o args=` hands back one space-joined string,
in which a jar path containing a space is indistinguishable from two arguments.

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
UI has to be able to tell them apart. The Server page and the Overview cards both
draw `ok: false` as **Unreachable**, with the error, and the Server page holds its
start form — `updateStartEnabled()` checks `serverError` too, since it runs on every
keystroke in the form and would otherwise switch Start back on. For a while only the
Players page looked at the flag, and the Server page showed "Stopped" with a live
Start button for a pane that was not there.

The same trap used to *write*. The pages that list jars, mods and world saves
create the directories they list, and did so in whatever `tmux_pane_path()` returned
— so opening the Mods page with a stopped server's shell sitting in `~` left
`~/mods` and `~/mods-saves` behind. `_ensure_game_subdirs()` now creates them only
when the pane's directory is the session's **confirmed** game dir. Before a server's
first run nothing is created by looking, and nothing needs to be: each action that
writes into one of those directories makes it for itself.

The roster still resolves its own game dir per request, so this trap is unchanged
here — but the panel store now records what it believes for each session, and the
`/api/server/start-policy` reports it as `dir` / `dir_confirmed` (the Server page no
longer prints it — see the start-policy section). `vibepanel-state.json` is the
quickest way to see whether the panel and the admin disagree about where a server
lives, and a `dir` carrying `dir_confirmed: true` was read off a server that was
actually running there.

Names are validated against `^[A-Za-z0-9_]{1,16}$` before ever reaching a console
command; ban reasons go through `pane_text()` — see the section below.

### Log scraping for add-suggestions

`_scan_latest_log()` pulls `UUID of player <name> is <uuid>` lines out of the last
512 KB of **`logs/latest.log` only**. Rotated logs are ignored *here*, `.gz` and plain
alike: stitching the tails of several files together would mean searching a history
with holes in it, which is both slower and confusing to reason about. The Activity
page is the one feature that does read the rotated archives — whole, not tails, so
no holes — see its section below.

**The line is matched with `fullmatch` against the message body**, after the
`"]: "` split and only on timestamped lines — the same anti-spoofing rule as
`_last_run_ending()`, and it matters more here than anywhere: what this scan finds
is offered to the admin with a "+ As OP" button, and is where a *stopped* server's
whitelist takes a UUID from. It used to be an unanchored `search`, so a player
typing `UUID of player Alice is <uuid>` in chat planted a suggestion of their
choosing, and `_resolve_uuid()` — which takes the newest sighting — preferred the
forgery to the genuine line. Chat arrives prefixed (`<Bob> …`), so it can never be
the whole body. The `System chat: ` label is deliberately *not* stripped: the
authenticator's line is the server's own logger output and never wears it.

The read itself is `_read_log_tail()`, shared with `_last_run_ending()` — the two
readers of this file want the same window and the same seek. It **raises** rather
than swallowing `OSError`, because the start policy has to tell "no log here" from
"could not read the log" and this scraper does not.

Log lines carry only `[HH:MM:SS]`, so dates are reconstructed by counting each
backwards clock jump as a midnight rollover, then dating each hit back from the
file's mtime, which is the date of its last line. Note the premise that used to be
stated here — "a restart rotates the file away, so a backwards jump can only be
midnight" — is wrong in a way that happens not to matter: stock `log4j2.xml` carries
`OnStartupTriggeringPolicy` **and** a `TimeBasedTriggeringPolicy` on a `yyyy-MM-dd`
pattern, so the file also rotates at midnight mid-run and there is usually no
rollover left to count. A config without the time-based trigger still produces one,
which is why the counting stays.

## The Activity page

`/api/activity` answers "who was playing, when, and for how long" from the game's
own logs — `logs/latest.log` plus the rotated `logs/YYYY-MM-DD-N.log.gz` beside it,
which makes it the **one** reader of the rotated archives (the tail-only rule above
`LOG_SCAN_MAX_BYTES` is scoped to the suggestion and last-run readers). The page is
called **Activity**, never "Sessions": that word is taken — see the vocabulary note
at the top of this file — and a "play session" in the code is always a *cluster*.

### What is parsed, and what deliberately is not

`_parse_activity_log()` matches `<name> joined the game` / `<name> left the game`
with `fullmatch` against the message body after the `"]: "` split and after
`_log_broadcast()` strips a `System chat: ` label — both are broadcast lines, so
26.3 labels them and 26.2 does not; see the label section above, and note that
getting this wrong is what made the page report a two-hour evening as five seconds.
The `fullmatch` is the same anti-spoofing rule as `_last_run_ending()`, and for the
same reason: `latest.log` carries chat, chat always arrives prefixed, and an
unanchored test would let any player fabricate history by typing "Alice joined the
game". Events are only taken from timestamped lines, so a stack-trace continuation
cannot contribute one.

`lost connection` is deliberately unmatched: every fully-joined player gets
`left the game` on every disconnect path (kick, timeout, quit), and `lost
connection` without it only happens for a player who never finished joining — who
never opened an interval. Crashes log neither line; they are handled by the two
lifecycle markers this parser shares with `_last_run_ending()`: `Stopping server`
closes every open interval at its own time, and `Starting minecraft server version`
closes them at the last timestamped line seen *before* it — the best witness to
when a crashed run actually died. A start marker that opens its file resolves
against the previous file's last line, which is why files feed in ascending order
carrying that epoch forward.

### Dating, and why gz files trust their filename

Lines carry only `[HH:MM:SS]`. `latest.log` back-dates from its mtime — the mtime
*is* the time of its last line — exactly as `_scan_latest_log()` does. A rotated
`.gz` must **never** do that: its mtime is the *rotation* time, and
`OnStartupTriggeringPolicy` fires at the next server start, so a file can be
gzipped days after its last line was written. It dates forward from the date in
its filename instead. The residual: a server idle since day D but restarted on D+3
may gzip day-D lines into a file named D+3, and nothing in the content can correct
that — worst case a session groups under the rotation date, never with wrong times
or durations.

Events from all files are merged and epoch-sorted **before** interval-building,
because the stock config rotates at midnight mid-run: one evening's joins sit in a
`.gz` with their leaves in `latest.log`.

### The cache: parse each gz exactly once

A `.gz` is immutable once log4j has written it, so `_ACTIVITY_GZ_CACHE` memoizes
each parse against `(size, mtime)` — the panel's first mtime-keyed cache — and
`latest.log` is re-parsed only when its own `(size, mtime)` moves. Both caches are
memory-only, the `_STOP_BACKUPS` rule: a cold re-parse costs well under a second,
so persisting it would buy nothing and add a file that can go stale. No lock,
following `HEAP_PEAKS`: every write is one dict assignment of an immutable value,
and the worst race is a duplicate parse. The horizon is `ACTIVITY_DAYS` (14) by
filename date, capped at `ACTIVITY_MAX_GZ_FILES`, and each request prunes the cache
to the files it just listed so entries age out of memory with their files.

A `.gz` that cannot be decompressed is **skipped, and the skip is cached** like a
parse: it contributes no events, prints one line, and the rest of the history is
shown. A file cut short while log4j was compressing it (a crash, a full disk) is
the realistic case. The catch has to name `EOFError` and `zlib.error` — what a
truncated or corrupt stream raises — because neither is an `OSError`; they used to
sail past `_activity_report()`'s handler and 500 the Activity page and the
Overview strip for the fourteen days the file took to age out.

`latest.log` gets its own window (`ACTIVITY_LATEST_MAX_BYTES`, 8 MB), not
`_read_log_tail()`'s 512 KB — a busy day exceeds that, and a join scrolled out of
the window silently loses the whole stint. The one interval-builder consequence: a
leave whose join fell outside the window (or horizon) is dropped, half an interval
being worse than none.

### Three transforms keep the summary readable

This is the answer to "overlapping groups and one-minute drop-ins must not make
the view a mess", and each has a constant:

| | what | why |
|---|---|---|
| STITCH (`ACTIVITY_STITCH_SECS`, 10 min) | a player's intervals separated by less than the gap merge into one **stint**, counting `reconnects` | a dropped connection and a rejoin is one visit, not two |
| CLUSTER (`ACTIVITY_CLUSTER_GAP_SECS`, 15 min) | start-sorted stints sweep into **play-sessions**; a stint starting within the gap of the cluster's reach joins it | on a 1-D timeline the sweep *is* connected components; the bridge keeps one evening whole across a short mid-evening server restart |
| DEMOTE (`ACTIVITY_BRIEF_SECS`, 5 min) | a player whose stints in a cluster are *all* shorter (and none ongoing — someone who just arrived is not a blip) becomes a footnote, not a timeline lane | blips must not mint lanes; a player who earned a lane keeps all their stints on it, brief ones included, because the lane is already paid for |

DEMOTE resolves **after** CLUSTER, per cluster: the footnote is per-session wording
anyway, and a demoted visit must still stretch the session it visited. A cluster
whose players are all brief is kept, flagged `all_brief`, rendered as a compact
line — a lone two-minute hop still shows rather than vanishing.

### The payload carries epochs

The client draws a Gantt — one lane per player, bars positioned by percent inside a
relative track, the heap-bar idiom — and a Gantt needs numbers, so `/api/activity`
returns epochs. This is the one sanctioned exception to "format on the server",
paid for with the client's first time formatters (`fmtDuration`, `fmtClock`); the
payload's `now` is the panel's clock, so ongoing bars and day grouping never depend
on the browser's clock agreeing with ours. Diagnostics ride along (`game_dir`,
`gz_found`, `gz_parsed_now`, `latest_log`) for the roster endpoint's reason: an
empty history is far more often a wrong game dir than an idle server, and the
empty state renders where it looked.

No polling: page-enter load plus a manual Refresh, the Worlds idiom — re-fetching
per entry is what the server-side cache makes cheap, and having no guard flag and
no module-level cache shrinks the `resetSessionUi()` obligation to one container
clear.

### The Overview's activity strip

The Overview carries a fixed-size summary of every server's Activity: one row per
server across `ACTIVITY_DAYS` day-aligned columns (browser-local, like the Activity
page's day headings — the last column is today, and each is labelled with its
weekday over its day of the month), each play-session highlighted and
nothing else. A row is a link to that server's Activity page
(`clickSessionTab(s, 'activity')`), which is what lets the rows stay this plain.
**The size never changes**: a server with no history, one still loading and one whose
fetch failed all get the same empty row, the reason in its tooltip. The day count
reaches the client as `data-days` on `#overview-activity`, rendered from
`ACTIVITY_DAYS`, so it has one source.

It reuses `/api/activity` per session rather than a new endpoint, and is fetched by
`loadOverview()` only — page entry, Refresh, Reset Peaks — **never by the status
timer**: a running server's `latest.log` moves constantly, so every timed fetch would
miss the cache and re-parse it, the same reason the Activity page doesn't poll. It
renders into its own container, not `#overview-grid`, so the 15 s card redraws leave
it alone.

## Updating mods

The Mods page is two folders and the admin decides what is in them; the panel has
never downloaded a mod, and so has never been responsible for which ones run. The
update feature keeps as much of that as an update feature can: **it never chooses a
mod, it offers a newer build of a file the admin already put in `mods/`.**

Identity is the file's own SHA-512 — no mod list, no config file, no filename
pattern, nothing specific to any mod. Modrinth resolves a hash to a project and,
given a loader (`MOD_LOADER`, `fabric`) and a game version, to that project's
newest build. A jar fetched from somewhere else still matches when the bytes are
the same: Geyser's own download site serves the identical file Modrinth does.

`_mod_update_report()` makes **two** requests, `/version_files` and
`/version_files/update`, because the second alone returns nothing at all for both
"Modrinth has never seen this file" and "its project has no build for this
version", and those are different things to tell an admin — the second one is the
answer to "are my mods ready for the next version yet?". That question is also why
the check is allowed while the server runs; only the update itself requires it
stopped, like every other mod change.

| status | meaning |
|---|---|
| `update` | a different build is the newest for that version |
| `current` | the newest build's hash is the file we hold |
| `held` | builds exist for that version, but only on a less stable channel than this file |
| `no-build` | known file, nothing published for that version |
| `unknown` | not on Modrinth; left alone |

**A file is only offered a build at least as stable as itself**
(`_allowed_channels()`, over `MOD_CHANNELS` = release, beta, alpha). The channel of
the file we hold comes from the same `/version_files` answer that says Modrinth
knows it, so the track is read off the file, not configured: a release stays on
releases, a beta may take a beta or a release. That is what lets one rule serve
both fabric-api, which should never drift onto a snapshot build, and Geyser, which
publishes *every* build as `beta` and would be offered nothing under a blanket
"releases only". An unrecognised channel reads as `release`, the strictest answer.

The unfiltered lookup still goes first, and only a file whose newest build is too
unstable is asked about again with `version_types` set — an older, stable-enough
build may sit behind the newer one. When there is none the status is `held`, which
is worth its own words: "only a beta for 26.4 so far" tells the admin the project
is working on it, where "no build" would not. To move a mod onto a less stable
track the admin installs that build by hand once; the rule follows the file.

`POST /api/mods/update` takes a filename and a version and **redoes the lookup
itself** — the jar picker's rule: the client selects a file that is on disk and
never contributes a URL or a destination name. `_update_mod()` then:

- refuses a URL that is not `https://cdn.modrinth.com/`, a size over
  `MOD_DOWNLOAD_MAX`, and a filename `_validate_mod_filename()` would not accept —
  no separators and no control characters. This is the one filename the panel takes
  from somewhere other than its own disk, so it is held to the rule for names that
  get typed even though nothing types it today;
- streams to a hidden `.tmp` in `mods/` (not `.jar`, so neither the page nor Fabric
  sees it), and discards it unless the SHA-512 matches what Modrinth published;
- **only then** moves the old jar to `mods-saves/` via `_move_mod_file()` — the same
  coalesce/conflict rule as Deactivate — and `os.replace`s the new one into place.
  Download first, move second: a failed download changes nothing.

The new jar keeps **Modrinth's filename**, never the old one's. Versioned names are
the only version display the Mods page has, and they are what keeps retired copies
in `mods-saves/` from colliding — a file kept as `Geyser-Fabric.jar` forever would
hit the "different content, same name" conflict on its second update.

Nothing is deleted, so rollback is Activate/Deactivate. The cost is that old
versions accumulate in Inactive, and clearing them out is the admin's job.

The version field is prefilled by `_guess_game_version()` from `last_jar`'s
`mc.<ver>-loader` (the shape `get-me-fabric.sh` downloads), carried on
`/api/mods/list` so the prefill costs no request. It is a prefill only: the upgrade
flow is "download the new Fabric jar, type the new version here, Check", and a
server that has not yet run the new jar still remembers the old one.

Deliberately not done: installing dependencies a new build declares, checking the
Inactive column (those are, by definition, out), remembering results anywhere, and
any check the admin did not click for. `modUpdates` on the client holds the last
check and is cleared in `resetSessionUi()` — its rows carry live Update buttons,
the same hazard as the conflict notice.

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

## Heap utilisation on the Overview cards (prototype)

`/api/server/heap` reads the JVM's own performance counters out of
`/tmp/hsperfdata_<user>/<pid>` and returns `used` / `committed` / `reserved` / `peak`
in bytes, plus `collections`. The Overview card draws them as one bar: the track is
`reserved` (-Xmx), the fill is `used`, and a notch marks `peak`. `_pane_java_info()`
also returns `pid`, and it is always the java process itself — a wrapper's pid is no
use to anything wanting to read a JVM's counters, so an su/sudo server contributes a
pid only when `_java_under()` located the real one. That case then turns on file
permissions: the counter file is mode 0600 and the panel never runs as root, so a
server running as another user gets "counter file is not readable" — the heap bar
is simply not available there — instead of the older, blunter "java process not
visible".

Every HotSpot JVM memory-maps that file and keeps it current as a matter of course;
it is what `jstat` reads. The format is a documented binary — 32-byte prologue, then
variable-length named entries — at version 2.0 since Java 5, and `_read_perf_counters()`
parses it in about forty lines. **No JDK is required and the server is not touched at
all**, which is the whole point.

### Why not jcmd

The first version of this shelled out to `jcmd <pid> GC.heap_info`, and it was
replaced because it was expensive — but not for the reason it looked. Measured over
50 samples against an idle JVM:

| | wall | CPU (client) | CPU inside the target JVM |
|---|---|---|---|
| `jcmd GC.heap_info` | 3.07 s | 4.69 s | 0.01 s |
| reading hsperfdata | 0.02 s | 0.02 s | 0.00 s |

`GC.heap_info` costs the Minecraft server about 0.2 ms; it is not scrutinising
anything costly. The ~94 ms per sample was **`jcmd` itself booting a second JVM** to
speak the attach protocol — `jcmd -h`, which contacts nothing, still costs 30 ms.
`jstat` reads exactly the counters we now read but pays the same startup tax, so it
would be strictly worse than reading the file ourselves.

**There is deliberately no fallback to jcmd.** A server whose counters we cannot read
shows no bar and the reason in a tooltip. A fallback would quietly change what the
number means (see below) *and* reintroduce the CPU cost on exactly the machines that
were already unhappy — better that an admin sees "heap unavailable" and decides.

### `used` is the live set, not occupancy — and that is the point

HotSpot refreshes these counters at GC boundaries, so `used` is what survived the
last collection rather than what is occupied this instant. The difference is not
subtle. A churning Parallel heap, sampled repeatedly:

```
hsperfdata used=161.5M     jcmd used=499.0M
hsperfdata used=161.5M     jcmd used=379.6M     ← same live set,
hsperfdata used=161.5M     jcmd used=501.5M       jcmd sampling eden's sawtooth
```

The live set is the better of the two figures here, and the only one worth a peak
marker: a healthy server fills eden to near its limit before collecting *by design*,
so a peak taken from instantaneous occupancy saturates within minutes and stops
saying anything. It also means the bar moves in steps at collections rather than
flickering. The UI says "live", not "used", so the distinction survives contact with
whoever reads it next.

Before the first collection every counter is still zero, which would render as a
server using no heap at all — so `collections == 0` is refused with "waiting for the
first GC" instead. It resolves within seconds on a real server.

### Deriving the maximum

No counter states the whole heap's maximum, so `_heap_from_counters()` derives it,
and the two collector families disagree about what a generation's max means. With
`-Xmx1g`, verified on JDK 25:

| collector | `generation.0.maxCapacity` | `generation.1.maxCapacity` | whole heap |
|---|---|---|---|
| G1 | 1024M | 1024M | 1024M — one shared region pool, each generation reports all of it |
| ZGC | 1024M | 1024M | 1024M — likewise |
| Parallel | 341M | 683M | 1024M — a fixed young/old split, so these sum |
| Serial | 341M | 683M | 1024M — likewise |

Hence: identical maxima mean a shared pool, differing ones get summed. The one way
that misreads is Parallel or Serial with `-Xmn` at exactly half of `-Xmx`, so it is
backstopped by an invariant — a heap cannot be committed beyond its reservation, and
if it appears to be, the sum was right after all. Do not reach for
`sun.gc.policy.name` to tell the collectors apart: it is `GarbageFirst` for G1 but
absent entirely for ZGC.

Other things worth knowing about the file: it is mode 0600 and owned by the JVM's
user, so the directory is globbed rather than assumed (`hsperfdata_*`) — it is named
for the JVM's user, not ours. HotSpot hardcodes `/tmp` on Linux
and ignores `TMPDIR`; `$TMPDIR` is checked too only because macOS puts it there.
`-XX:+PerfDisableSharedMem` (common in containers) or `-XX:-UsePerfData` removes the
file altogether, which is the "no counter file" message.

### Finding the file is not trusting it

`/tmp` is everybody's. Any local user can create `/tmp/hsperfdata_x/<pid>` — as a
file of invented figures, a symlink to `/dev/zero`, or a fifo — and `glob` returns
candidates in no particular order, so theirs could be the one found. The read used
to be unbounded and to follow links: the `/dev/zero` case ran the panel out of
memory, on a host where the thing most likely to be killed for that is the game.

So `_read_hsperf()` only reads a candidate that is what a JVM would have made — the
same checks HotSpot applies to its own directory before using it:

| | must be |
|---|---|
| the directory | a real directory, not a link; owned by the JVM's user; not writable by group or other |
| the file | a regular file, not a link; owned by that same user; at most `HSPERF_MAX_BYTES` (4 MB — the real thing is 32 KB) |

"The JVM's user" is whoever owns `/proc/<pid>` (`_proc_uid()`). The panel's own uid
is accepted as well, for a host with no `/proc` and for a JVM whose `/proc` entry is
root-owned because the process is not dumpable; neither is a user a stranger can
create files as. The file is opened *through* the directory's descriptor
(`dir_fd=`), with `O_NOFOLLOW`, and judged by `fstat` on what was actually opened,
so nothing can be swapped between the check and the read; `O_NONBLOCK` is what makes
opening a fifo return rather than wait.

Anything that fails a check is skipped as though it were not there, with one
exception: a `PermissionError` on the file inside a directory that *passed* is the
genuine file of a server running as another user, and is reported as such.

### Host peaks, the refresh rate, and Reset Peaks

The host bars carry the same notch, fed by `peak` fields that `/api/system/stats` now
returns inside each block, in that block's own unit — load average for `cpu`, bytes
used for `ram` and `disk`. They are marked but not labelled, so the row stays as
narrow as it was; the number is in the notch's tooltip. Because the peak is folded in
by `_record_peak()` as a side effect of *serving* the stats, the mods and worlds pages
contribute samples too — they call the same endpoint for their disk line.

The Overview's auto-refresh reads `[every] [15] s` beside its Refresh button, where
the word "every" is itself the on/off button — clicking it says `off` and stops the
timer. Both halves persist in `localStorage`: `vibepanel.overviewRefreshSecs` (default
15 s) and `vibepanel.overviewRefreshOn`. `refreshSecs()` clamps to 1…3600 on read
*and* writes the clamped value back, so a stale or hand-edited key can't outlive the
check. Clamping, not rejecting: the value only drives a repeating fetch, and the
failure mode worth preventing is a sub-second interval hammering the panel. It commits
on `change`, not `input` — restarting the timer per keystroke would drop to 1 s the
moment someone types the "1" of "120". `refreshOn()` treats anything that isn't
literally `'off'` as on, so a missing or garbled key leaves refreshing working rather
than stranding someone on a stale page they think is live.

`overviewRestartTimer()` is the only place a timer is created, and it clears before it
schedules, so it serves both directions of the toggle and every rate change without
leaking an interval. Off leaves `overviewPollTimer` null — the same state as being on
another page, which is why the toggle handler tests `activePage` rather than the timer
to decide whether to act. Off stops the *repeating* fetch only: arriving on the page
still loads once, and the manual Refresh button is untouched. Both still sample peaks,
since that is a side effect of the endpoints themselves.

`POST /api/peaks/reset` forgets everything at once: `HEAP_PEAKS` and `SYSTEM_PEAKS`
both. It is global rather than per-session because the button lives on the Overview,
the one page showing all of them together. Nothing is pushed to the client in reply —
peaks are re-seeded from the next sample, so the button just calls `loadOverview()`.

## Directory layout (relative to game dir)

```
<game-dir>/
  server-jars/         # .jar files for starting the server  (JARS_DIR)
  mods/                # active Fabric mods                   (MODS_DIR)
  mods-saves/          # inactive/stashed mods                (MODS_SAVES_DIR)
  world-saves/         # .tgz world backups                   (WORLDS_DIR)
  logs/latest.log      # name→UUID suggestions, how the last run ended, play activity (read-only)
  logs/YYYY-MM-DD-N.log.gz   # older play activity for the Activity page (read-only)
  config/Geyser-Fabric/config.yml   # bedrock.port, if Geyser is installed (read-only)
  whitelist.json       # read + written by the Players page
  ops.json             #   "
  banned-players.json  #   "
  get-me-fabric.sh     # auto-installed from repo root if missing
  start.sh             # optional: the game's own start script, if it has one
  .vibepanel.json      # last-used start form per session (written on start/stop)
```

And in the **panel's** own working directory, not the game dir:

```
<panel-dir>/
  vibepanel-state.json  # expected sessions, their game dirs, start policies
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

**`title` attributes would count too**, if any per-session UI used one: a tooltip
naming the previous server's jar and game directory is exactly as wrong as a stale
visible line, and worse to notice, because nothing on screen looks out of date. The
policy cards briefly carried one and it had to be cleared here like everything else.

Per-session data that should survive a switch (rather than be cleared) is keyed by
session name — see `sayHistory`, so a broadcast sent to one server reappears under
that server's tab and never under another's.

## Anything typed into the pane must be inert as a shell command

`tmux_send()` types text at whatever is reading the pane. That is *usually* the
Minecraft console, but the server may have stopped a moment earlier — and we
accept that race rather than trying to close it — so a shell may be reading
instead. `say hi; rm -rf ~` typed at a shell runs `rm`. This is not theoretical;
it was verified with canary files before being fixed.

The two endpoints that type on an admin's say-so with nothing else in the way —
`/api/say` and `/api/players` — ask `_pane_java_info()` whether the server is
running immediately before the keystroke and answer 409 if it is not. That does not
close the race, and is not meant to: it is what makes it *a race*, a window of
milliseconds, rather than something a request sent to a stopped server gets every
time. The page disabling its Send button was never a check; the endpoint is.

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

That is now the rule for **every** pattern that validates a whole string, not only
the ones headed for the pane: they are written without `^…$` and called with
`fullmatch()`. World-save and mod filenames, player names, UUIDs and the log
markers all used `match()` with a `$` and accepted a trailing newline — harmless
only because each happened to sit behind a `.strip()` or a `splitlines()`.
`match()` survives in one place, `_LOG_LINE_TIME_RE`, where a prefix is what is
wanted.

That set is wider than the strict minimum — with the separators gone, a lone `(`
only yields a bash syntax error — but the safety of the narrower list depends on
our line always beginning with `say `/`ban `, which is a fragile thing to rely on.
Prefer the blunt version.

The cost is that apostrophes, `!` and brackets are dropped from chat, so
`/api/say` returns the text it actually sent as `sent`, and the UI records *that*
in its history and says "some characters removed" rather than silently
misreporting what was broadcast.

`/api/server/start` is the exception that is genuinely meant for a shell: it uses
`shlex.quote()` on the game dir and on the jar path or `./<script>`. That also makes
paths containing spaces work, which they previously did not.

The custom start script is the one place a **name from the client** reaches that
line, and quoting is deliberately not the whole of its defence — a name is only
accepted if `_resolve_start_script()` finds it as an executable plain file resolving
inside the game dir, and control characters are rejected outright, because a newline
would be typed as Enter no matter how the rest is quoted. See the start-forms section
above for the full list.

## Who may talk to the panel

The panel has no login: whoever can reach the port is the admin. "Who can reach
it" is therefore the whole of its access control — which is why `--host` defaults to
loopback and the supported way to reach it from elsewhere is a Tailscale address,
never `0.0.0.0` — and a browser is a way of
reaching it that the network cannot see — any page open in a browser that can
reach the panel can ask that browser to send it requests. Binding to a Tailscale
address does nothing about that, because the request comes from the admin's own
machine.

One `before_request` hook (`_guard_request()`, deciding in `_request_refusal()`)
applies three rules to **every** route — page, static file and API alike — so an
endpoint added later is covered without anyone remembering to:

| rule | what it stops | why it works |
|---|---|---|
| `Host` must name this machine | DNS rebinding: a page that points its own name at our address is *same-origin* to the browser, so nothing else in the request distinguishes it | the `Host` header still carries the attacker's name |
| `Origin`, when present, must be the origin the request was sent to | a cross-site POST | every browser sends `Origin` with one |
| anything that is not a GET must carry `X-VibePanel` | the same, in a browser old or odd enough to omit `Origin` | a page on another origin cannot add a header of its own without a CORS preflight, and the panel never grants one |

The first row is a different attack from the other two, and the difference is
worth keeping straight. A forged request is blind: another site makes the browser
*send* something and never sees the answer. A rebound one is same-origin, so it
passes the `Origin` and header rules untouched and can read the console and download
worlds as well. Only the `Host` check stops it, and it applies however the panel is
bound — `localhost:8080`, the default, is the classic rebinding target.

`_host_allowed()` accepts an IP literal (there is no name there to rebind),
`localhost`, the machine's own hostname, the names Tailscale gives it, and whatever
`--allowed-host` adds — a leading dot matches subdomains, and `*` turns the check
off. It fails closed, and the 403 names the flag to pass.

**The Tailscale names are asked for, not assumed.** `_ask_tailscale()` runs
`tailscale status --json` and takes `Self.DNSName` — the MagicDNS name, long
(`host.tailnet.ts.net`) and short. That is the one name an admin is likely to type
that the OS cannot report: the short form usually matches the hostname, the full one
never does. Allowing everything under `.ts.net` would be less code and is probably
safe, but it rests on how somebody else's DNS is run; this rests on our own node
saying its own name. No Tailscale, logged out, or a CLI that hangs all read as "no
names" — the timeout is 3 s — and never as an error.

It runs once at startup and again when a `Host` is about to be refused, at most
once every `_TAILSCALE_RETRY_SECS`. The retry is for boot, where the panel can be up
before tailscaled is, and for a machine renamed while we run; the interval is what
stops it being something a stranger's requests can make us do on demand. An empty
answer never *removes* a name already learned. This is not on the "External network
access" list below because nothing leaves the machine: the CLI talks to the local
daemon.

`Sec-Fetch-Site` says what `Origin` says more precisely, and is checked when it is
there — but **browsers only send it to https and localhost**, and the usual
deployment is plain http on a tailnet address. It is a bonus, never the mechanism.
Where it is present, the one cross-site request allowed is following a link to the
page itself: a top-level `GET` of a non-`/api/` path. Never a frame, never a
subresource.

Two things follow from this for anyone adding an endpoint:

- **GET must not change anything.** Nothing above can stop a cross-site GET from
  being *sent* over plain http — an `<img src>` carries nothing that tells it from
  our own request. That is why `/api/players` is a POST: it types `list` at the
  pane. What a GET may still do is be wasteful (`/api/mods/updates` asks Modrinth),
  and the response headers keep its answer from reaching the page that asked.
- **Read the body with `_json_body()`, never `get_json(force=True)`.** `force` reads
  any body as JSON, and `text/plain` is exactly what a cross-site form *can* send.
  The hook refuses a non-JSON body with 415 before the route runs; `_json_body()`
  is the second lock, and also turns a body of `[]` into "no fields" rather than an
  `AttributeError`.

On the client, `apiFetch()` is the only caller of `fetch()` and adds the header to
everything that is not a GET. A bare `fetch()` POST is refused, which is the point.

`_harden_response()` puts the same headers on every response: a
`Content-Security-Policy` that allows only our own script (the page has no inline
script and loads nothing from elsewhere, so it can simply say so — `'unsafe-inline'`
is for *styles* only, which the bars and timelines are positioned with), `nosniff`,
`frame-ancestors 'none'` / `X-Frame-Options`, and `Cross-Origin-Resource-Policy`.
The CSP is defence in depth behind `esc()`: it is what turns a missed escape in
`app.js` from script execution into a rendering glitch.

A reverse proxy in front of the panel must pass `Host` through unchanged; one that
rewrites it makes every `Origin` look foreign. `X-Forwarded-Host` is deliberately
ignored — a rebinding page can send any header it likes.

## External network access

The panel reaches the internet in exactly **four** places, and the list is meant to
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

4. The Mods page's update check and update, against `api.modrinth.com` and
   `cdn.modrinth.com` — **only on a click**, never on page load or a timer. See
   "Updating mods".

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

**`KillMode=process` is not optional.** A tmux session that `_ensure_session()` created
is a child of the panel and lands in the unit's cgroup — daemonizing does not escape
one — so systemd's default `KillMode=control-group` SIGTERMs it on every
`systemctl restart vibepanel`, taking the running game down with the panel. That was
survivable while autostart was a bool, since the restart simply started the server
again. Under `unless-stopped` it is not: the JVM's shutdown hook writes a clean
shutdown to the log on the way out, the next pass reads that back as a deliberate
signal kill on a live host, and the server stays down. A routine panel restart would
stop the game permanently.

It only bites sessions the *panel* created — one an admin started from their own login
shell lives in a different cgroup — which is exactly the boot-time path.
