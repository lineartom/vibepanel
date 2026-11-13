# Future work

What is known to be open, and why it was left. `CLAUDE.md` describes how the panel
works today; this file is the list of things that should change, so that a
deliberate deferral is not later mistaken for an oversight (or re-reported as a
new bug).

Items are named by function rather than line number, since lines move.

## Rethink: how servers are declared (`--session` and friends)

The largest item, and the reason several bugs below were deferred rather than
patched: they are symptoms of one model that grew by accretion. Today a server is
identified by a tmux session *name*; the set of servers comes from `--session`,
or the stored set, or `TMUX_TARGET`, or adopting the only session that exists; and
where a server lives is learned by watching. Each rule is reasonable on its own.
Together they have sharp edges, mostly at first setup and at reboot.

To be redesigned as a whole — how a server is declared, named and found, and what
happens the first time and after a reboot — rather than fixed one at a time.

Known problems the redesign should make impossible:

- **`TMUX_TARGET` is ignored after the first run.** `__main__` resolves the set as
  `--session`, else the stored set, else `TMUX_TARGET`. Once anything is stored the
  env var never wins, though the docs present it as equivalent to `--session`.
  `start.sh` documents `TMUX_TARGET=myserver ./start.sh`, which silently stops
  meaning anything on the second run.
- **A wrong `--session` that relies on adoption wipes the store at reboot.**
  `_resolve_tmux_target()` adopts the sole tmux session when the named one is
  missing. After a reboot there is no tmux server, so adoption cannot happen, the
  wrong name is kept, and `_set_expected_sessions()` drops the adopted session's
  entry — its directory, start policy and backup setting. The unit file's comment
  that leaving `--session` in place "is harmless" is not true in this case.
- **tmux matches `-t name` by prefix.** If `mc` is missing while `mc2` exists,
  `has-session -t mc` succeeds, so `_ensure_session()` never recreates `mc` and its
  tab reads and controls `mc2` — Stop included. Only bites when one session name is
  a prefix of another. The narrow fix is `-t =name` (exact match) everywhere a
  target is passed; it was held back to be done with the rest. *Unverified: taken
  from tmux's documented target matching, not reproduced.*
- **`SERVER_DIR` is one global** used as the fallback directory for every session
  the panel has never seen, which is the assumption the per-session store exists to
  replace.

Questions the redesign has to answer:

- Should a server be identified by something other than its tmux session name?
- What is the single source of truth for the set of servers, and what is the
  explicit way to add or forget one?
- Should adoption exist at all, or be a one-time setup step rather than a rule
  applied on every start?
- What should env vars mean once a store exists?

## Rethink: installation (`install.sh`)

Left alone on purpose; the whole install process is to be reconsidered.

- **Every upgrade regenerates the unit file** from the template with a fixed
  `ARGS`, discarding the edits its own closing message tells the admin to make
  (port, extra flags). Only `User` and `WorkingDirectory` are read back.
- **Exits silently when `tailscale` is missing.** Under `set -e` the failed
  `tailscale ip -4` ends the script before the message explaining why.
- **`envsubst` is assumed** to be installed and is not checked for.
- **`User=root` now cannot work**: the panel refuses to run as root (exit 78). The
  installer should refuse that choice up front rather than install a unit that
  will not start.
- It is the only place the Tailscale address is chosen; the panel's own default is
  now `127.0.0.1`.

## Smaller open items

### Security

- **GETs that reach the internet.** `/api/mods/updates` (Modrinth) and
  `/api/server/latest-minecraft` (Fabric meta) are GETs. Over plain http a page on
  another site can cause a GET to be *sent* — it cannot read the answer — so these
  can be triggered from outside. Making them POSTs closes that.
- **`/api/server/latest-minecraft` is fetched on every Server page visit**, only to
  fill a placeholder. The public-IP lookup is deliberately once-at-startup for the
  same reason this should be cached.
- **`_last_run_ending()` accepts untimestamped lines.** The suggestion and Activity
  readers only take events from lines that start with a timestamp; this one does
  not, so a continuation line reading `…]: Stopping the server` counts as a stop
  request. Needs a newline injected into the log, for which no route is known.

### Correctness

- **A mod download interrupted by the panel dying leaves a hidden `.tmp` in
  `mods/`.** Same class as the world-backup scratch file, which now uses a nameless
  file; `_download_mod()` does not.
- **World claims do not cover what the panel cannot see**: a server started by hand
  at the pane, and the moment between Start's keystroke and the JVM appearing on
  the tty. Accepted as the limit of "basic locking".
- **A stop-backup interrupted by a panel restart is simply lost** — no archive and
  no record, since `_STOP_BACKUPS` is in memory.
- **A state file holding valid JSON that is not an object crashes.**
  `.vibepanel.json` containing `[]` makes `_last_jar()` and friends raise;
  `vibepanel-state.json` containing `[]` stops the panel starting. Hand-editing
  only.
- **Returning to the Server page after a start or stop fetches the two policy notes
  twice**, once for entering the page and once for the transition. Harmless.

### Limits worth deciding on

- **`world` is hardcoded.** `level-name` in `server.properties` is ignored, and
  servers that keep dimensions in sibling directories (`world_nether`, …) would
  have only `world` backed up. Fine for vanilla and Fabric.
- **Restart-loop start scripts.** "Stopped" means no `java` on the pane's tty. A
  script that relaunches the server sits in that state between runs, so an on-stop
  backup or a Worlds → Load can overlap the relaunch.
- **Disk figures are for `/`**, not the filesystem the game dir or world saves are
  on.
- **No retention for on-stop autosaves.** One full world archive per stop, until
  someone presses Delete Autosaves.
- **Archives made from a symlinked `world` before that was fixed contain only the
  link.** Nothing flags them; the Worlds list could, by size or by listing the
  archive.
- **The `list` answer is only recognised behind a `]: ` prefix.** True of vanilla,
  Fabric, Paper and Forge consoles; a custom console pattern without it would show
  nobody online.

### Docs

- `README.md` still describes the start policy as a single checkbox ("Starting a
  server automatically"); it has been three options for some time.
- `_fetch_public_ip()`'s docstring says the panel reaches the internet in "only two
  places". There are four.

## Needs checking somewhere this could not be tested

The fixes on `review-fixes` were tested in a container with tmux, Flask and a real
JVM, but with no browser and no Tailscale.

- **The Content-Security-Policy has never been loaded in a real browser.** It was
  written from reading the page (no inline script, nothing loaded from elsewhere).
  If something on a page stops working, the browser console will name the blocked
  resource.
- **The client changes were driven under a fake DOM in Node**, not clicked through:
  the `X-VibePanel` header, the Unreachable state, and start/stop detection on the
  Server page.
- **`tailscale status --json` is assumed to carry `Self.DNSName`.** Tested against
  a stand-in CLI. On a real host the panel should print `Tailscale name: …` at
  startup.

## Decided — not planned

Recorded so they are not raised again as findings.

- **No login and no TLS.** The panel relies on the network it is bound to;
  Tailscale is the supported way to reach it from another device.
- **A panel that is not root but is more privileged than the game dir's owner** has
  a smaller version of the exposure that running as root had. Treated as the
  admin's deliberate arrangement and not checked for.
- **A reverse proxy must pass `Host` through unchanged.** `X-Forwarded-Host` is
  ignored on purpose.
- **Scripts must send `X-VibePanel: 1`** on anything that is not a GET.
