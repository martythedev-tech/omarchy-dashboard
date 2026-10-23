# Plugin Dashboard

A bar chip for every third-party and custom Omarchy plugin -- including
itself -- plus the apps you choose to track, with a badge for how many have a
real update waiting, an enable/disable switch per plugin, a diff preview, and
an Update button once a check against the actual upstream (git, the package
repos, the AUR) confirms there's something to pull. A **Discover** tab browses
and installs plugins from the official catalog.

Apps come in two kinds (see [Apps](#apps)): an **installed package** you want
reminding about, like Brave (`brave-bin`), and a **source repo** you build
yourself, like Flea or Howdy.

Omarchy's own built-ins never show up here: the plugin list comes from
`omarchy plugin list --json` filtered to `firstParty: false`, which is
exactly the "not Omarchy's own" boundary Omarchy itself already draws.

## The icon

The bar icon is a puzzle piece, the usual sign for plugins. Its colour comes from your
theme's palette (`colors.toml`), and it moves with what it is telling you:

- **Green, seated in its slot**: everything is current.
- **Yellow, lifted out of its slot** (its empty slot outlined beneath it), with the count
  of waiting updates beside it.
- **Red**, with one shake when it turns red: something needs a person -- local changes
  that clash with an update, a half-finished git operation, or an action that failed in
  the last day that you haven't seen yet (opening the panel counts as seeing it).
- **Rocking on its base** while a check or an action runs.
- When the last update lands it **drops back into its slot** with a bounce, or gives a
  small pop if it was already seated.

## How it works

Nothing here re-implements an update mechanism. Every action shells out to
the command that already owns it:

- **Plugins**: `omarchy plugin enable/disable <id>` for the switch,
  `omarchy plugin update <id> --yes` for the button -- Omarchy's own
  fetch + diff + fast-forward-only merge + validate + rollback --
  `omarchy plugin remove <id> --yes` for Remove. A plugin whose deployed
  directory isn't a git checkout (installed by hand rather than
  `omarchy plugin add`) shows "No update source" instead of a button,
  because there's genuinely nothing to check.
- **Plugins with local work**: `omarchy plugin update` only fast-forwards, so it
  refuses a checkout with its own edits or commits. The Dashboard does that
  update itself: it sets uncommitted edits aside (`git stash`), rebases local
  commits onto upstream (one upstream already has drops out), puts the edits
  back, and validates. Any step that fails puts the checkout back exactly as it
  was. Each check dry-runs this first (`git merge-tree`, which touches nothing),
  so such a plugin shows a normal Update button with an EDITED / +N LOCAL tag,
  and only work that clashes with the update shows "dirty" or "diverged",
  naming the files.
- **Apps**: an installed package updates the way the system does (`omarchy update`, or
  `yay -S` for an AUR package), in a terminal; a source repo runs its own `updateCmd`
  (default `rebuild.sh --install`). The whole story is under [Apps](#apps).

`dashboard.py check` does the checking (git fetch + compare per plugin and
source app, `checkupdates` and the AUR for package apps, `pacman -Q` for
installed versions) and writes
`~/.local/state/omarchy/plugins/martythedev-tech.dashboard/status.json`,
which the panel reads. It runs on a timer (`dashboard-check.timer`, every
few hours) and once synchronously whenever the panel opens, so opening it
never shows data older than that. Any item that just became updatable since
the last check (not one that's been sitting there) fires a desktop
notification via `notify-send`.

## Apps

The **APPS** section tracks things that are not plugins. Add one with **+ Add app** at the
bottom of it, then pick what kind it is.

| | Installed package | Source repo |
|---|---|---|
| For | anything installed with pacman or yay, e.g. Brave (`brave-bin`) | a git checkout you build and install yourself, e.g. Flea, Howdy |
| Add it with | **Installed package**: type the package name, **Fill in** | **Source repo**: **Choose…** the folder (or type it), **Fill in** |
| Checked against | the package repos (`checkupdates`), or the AUR | its git upstream (`git fetch`) |
| Update button | **Update system…** (repo package) or **Update…** (AUR) | **Update** |
| What Update runs | `omarchy update`, or `yay -S <pkg>`, in a terminal | its update command, in the background |
| Row says | `· package` | `· app` |

### When does the Update button appear?

Only when an update is actually waiting. A current app shows no pill and no button -- there
is nothing to run. When a new version lands, the next check (every few hours, and whenever
the panel opens) turns the row to UPDATE (BEHIND for a source repo), adds it to the bar badge, sends a notification
with an **Update** button, and the row gets its own button. To update the system at any
other time, run `omarchy update` yourself (Omarchy's own update icon in the bar runs it too,
but appears only when a new Omarchy release is out).

### Installed packages

Type the package name and press **Fill in**: it names the app and says where the package
comes from and how it will update. The name people type is often not the package's
(`brave` is `brave-bin`); a name another package provides is tracked as that package, and
one that is not installed gets suggestions.

- *From a package repo* (omarchy, extra, ...): checked with `checkupdates`, which refreshes
  a private copy of the sync databases (no root; the system's are untouched). Its update is
  **Update system…**: `omarchy update` in a terminal -- the whole system, because Arch does
  not support upgrading one repo package on its own.
- *From the AUR*: checked against the AUR's version; **Update…** runs `yay -S <pkg>` in a
  terminal.
- A `-git` (or other VCS) package shows NO SOURCE: the AUR's version only moves when its
  PKGBUILD does, not when the source does.

These updates open a terminal because they need sudo. The terminal records the result
under Recent and rechecks when it is done. **Update all** and the notification's **Update**
open one terminal for every package app and run everything else as usual. A package app
has no Changes view (there is no checkout to diff) and no Roll back.

### Source repos

Pick **Source repo**, then **Choose…** (the desktop's folder chooser) or type a path and
press **Fill in**. That reads the repo and fills the rest -- name and id from the folder,
the package name from `.SRCINFO`, the upstream branch and remote from git, and
`./rebuild.sh --install` if the repo has an executable `rebuild.sh` -- and says what it
could not work out.

- The check asks "has the tracked branch's upstream moved past what we've merged" -- not
  "is the checked-out branch different from origin," which would always be true for a
  checkout like Flea's `aarch64-local`, deliberately ahead of the AUR branch it tracks.
- The update command must build *and install*. A `rebuild.sh` brings upstream in itself.
  For anything else (say `make install`), **Pull upstream first** is ticked: the Dashboard
  fast-forwards the checked-out branch to upstream before running the command. It never
  merges: local commits upstream lacks make it stop and say so.
- After the command, the app is checked again. If the checkout still lacks upstream's
  commits, or the installed package is older than what the repo builds, the update is
  reported as failed with that reason -- an exit code 0 alone is not trusted.
- **Changes** shows the incoming commits and diff, as for a plugin.

### Removing and editing

**Stop tracking** (in the expanded row, two clicks) only forgets an app here; it never
touches the repo or the package. Apps live in
`~/.local/state/omarchy/plugins/martythedev-tech.dashboard/apps.json`, which can be
hand-edited. It starts empty -- except on the author's machine, where it is seeded with Flea
and Howdy because their checkouts are there:

```json
[
  {"id": "brave-bin", "name": "Brave", "source": "package", "pkgName": "brave-bin"},
  {"id": "flea", "name": "Flea", "repoDir": "/home/you/Projects/flea", "pkgName": "flea",
   "branch": "master", "remote": "origin", "updateCmd": ["./rebuild.sh", "--install"]},
  {"id": "tool", "name": "Tool", "repoDir": "/home/you/Projects/tool", "pkgName": "tool",
   "branch": "main", "remote": "origin", "updateCmd": ["make", "install"], "pullFirst": true}
]
```

## What the row tells you

Rows sort by what needs you: a stuck git operation or local changes clashing with an
update first, then anything one click would update, then sources that could not
be reached, then the rest (held items with the rest) -- alphabetical within each.

Each row starts with a tile: the plugin's letter on a color taken from your theme's palette
(a leading "Omarchy" is skipped, so Omarchy Doctor is **D**), faded when the plugin is off,
with a dot on its corner when the row wants something -- yellow for an update, red for a
problem. Beside it: the name, its state pill, the version, and one line of description (the
plugin's `manifest.json`, or the package's own description for an app).

A row itself carries only its state pill, its on/off switch (plugins) and at most one
button -- **Update** (**Update system…** / **Update…** for a package app) when there is
one, **Unhold** when held. Click the row's name to expand it: its full status, the branch
it is on (or, for a package app, where it comes from), when upstream last committed, and
every other action that applies -- **Changes**, **Hold**, a link to its source (the
GitHub repo, or the AUR page), **Open folder**, **Terminal**, then **Roll back** and
**Remove…** (**Stop tracking** for an app) last. New plugins are found in the
**Discover** tab (below).

**Same-version commits are quiet.** For anything behind, the check reads the
version the incoming commits would install (the plugin's `manifest.json`, or an
app's `.SRCINFO`) without touching the checkout. A row says `→ v1.2.0` when that
changes the version, or `same version` (with a muted pill) when it doesn't --
a README or screenshot commit. Same-version updates stay in the panel with their
Update button and in Update all, but stay out of the bar badge and never send a
notification. If the incoming version can't be read, it alerts as before. The
catch: a plugin that changes code without bumping its version is quiet too.

**Update all** ends with a one-line summary ("2 updated, 1 failed (Flea).") and
leaves the first failure's output open.

The state pill, next to the name -- none at all when nothing is waiting:

- **BEHIND N** (yellow): N upstream commits would come in; **Update** brings them.
- **DIRTY** / **DIVERGED** (red): local edits or commits clash with the update; the expanded row
  names the files. Update yourself.
- **GIT**: a rebase, merge, cherry-pick or bisect is half done in the checkout. Finish or
  abort it.
- **OFFLINE**: the source could not be reached this check.
- **HELD**: you held this update (see Hold below). **OFF**: a plugin you turned off.
- **WORKING**: an action on it is running.
- **NOT INSTALLED** (source apps): the repo is current but the installed package is older
  than the version its `.SRCINFO` builds -- an update whose build or install step never
  ran. Update runs the app's `updateCmd`, which builds and installs it.
- **UPDATE** (package apps): a newer version of the package is out; the row says which.
- **NO SOURCE**: nothing to check against -- a plugin that is not a git checkout, a `-git`
  package, or a package that is in neither the repos nor the AUR. The expanded row says
  which.

Beside it, in a muted outline:

- **+N LOCAL**: the checkout carries N commits upstream does not have -- a fork you
  maintain, or a local fix you are holding. Shown on a current row too, so it is not
  mistaken for an untouched install.
- **EDITED**: the checkout has uncommitted edits; an update carries them over (see Plugins
  with local work above).
- **✓ VERIFIED** (green) / **UNVERIFIED**: what the marketplace says about the code you
  run -- see [Marketplace verification](#marketplace-verification). On a row with an update
  waiting it is about the update instead: **✓ UPDATE VERIFIED** / **UPDATE UNVERIFIED**.
  No badge for a plugin the marketplace does not list.

When the name line is full, the name keeps its room: the badges show in that order (state,
marketplace, LOCAL / EDITED) while they fit, and a marketplace badge that does not fit
moves to the line under the description ("→ v1.5.5 · update unverified").

### Marketplace verification

The official catalog runs an automated security baseline on each listed plugin and
records the exact commit it passed. The Dashboard compares that commit with yours:

- **Verified** only when the commit you run is that exact commit. Uncommitted edits are
  not covered (the expanded row says so; the row says EDITED).
- **Unverified** for anything else: a newer upstream commit the check has not reached yet,
  your own local commits, or a plugin whose check never passed. The expanded row's
  **Checked** line says which, and how many commits you are past the checked one.
- When an update is waiting, the badge judges the commit the update would bring
  (`FETCH_HEAD`), since that is the decision in front of you.

Most listed plugins move faster than the check, so UNVERIFIED is common and is not a
warning on its own: it means nobody but you has looked at this exact code. The expanded row
links to the plugin's marketplace page (**Marketplace ↗**). The check reuses Discover's
cached catalog (refetched when older than 6 hours); if it cannot be read, rows show no
badge rather than a guess.

**Update all: verified only.** The switch at the bottom right of the panel (**Update all:
everything** / **Update all: verified only**; click it to flip) narrows Update all to plugin
updates that bring the verified commit. The button then reads **Update verified (N)**. Left
out: an UPDATE UNVERIFIED plugin, an unlisted one (your own plugins included), and any
plugin while the catalog cannot be read. Apps are not in the marketplace and always go.
Each skipped row keeps its own **Update** button, and the summary names what was skipped.
The notification's **Update** follows the same switch. The choice is kept in
`settings.json` in the state folder.

Checks run side by side, so a full check takes about as long as the slowest single
fetch rather than the sum of them.

## Extra actions

- **Changes**: for anything `behind` or `diverged`, lists the commits the
  update brings (and, for a plugin, the local ones upstream lacks), with a Diff
  tab for the full `git diff` against the fetched upstream.
- **Hold**: from the expanded row or the Changes view, keeps an update you don't want yet
  out of the badge, Update all and notifications; the row shows HELD and an Unhold
  button. Holds live in `holds.json` in the state directory.
- **Roll back** (plugins): for a week after the Dashboard updates a plugin, and
  as long as its checkout hasn't moved since, puts it back where the update
  found it -- `git reset --hard` to the old commit (uncommitted edits are set aside
  and put back on top; if they do not fit the old version, the update stays),
  validate (the update is restored if the old version no longer validates), stop
  and remove any service the update had started that the old version doesn't
  ship, the plugin's own `run.sh --ensure`, and a plugin rescan. It then holds
  the plugin, so the same update isn't offered straight back. Apps have no Roll
  back: a source app is rebuilt from source, and a package app is updated by the system.
- **Recent**: every update, rollback, install and removal is recorded in
  `history.json` (last 50); the newest few are listed at the bottom of the panel.
- **Update all**: appears once more than one item is updatable; updates them
  one at a time (package apps together, in one terminal -- see [Apps](#apps)). It can be
  limited to marketplace-verified plugin updates (see [verified only](#marketplace-verification)).
- **Remove…** (a plugin): shows what it will do before anything happens --
  delete the folder (a git checkout; its source stays upstream), unlink it (a
  symlink), or move it to a backup (anything else); stop and remove the
  services that run its code (matched on the plugin's path in the unit, not
  the unit's name); and the data folders it left in `~/.local/state`,
  `~/.cache` and `~/.local/share`, with sizes. Data is kept unless you tick
  **Trash its data**, and then it goes to the desktop trash, never deleted
  outright. A folder another installed plugin also points at is never
  offered. Then `omarchy plugin remove <id> --yes`; if that refuses, the
  services are started again. For an app, **Stop tracking** (two clicks)
  only forgets it here (see [Apps](#apps)).
- **Leftovers**: what removed plugins left behind -- omarchy's
  `.<id>.bak.*` backup folders, services still pointing at a plugin that is
  gone, and data a removal kept. **Clean up** sends a folder to the trash and
  stops and removes a unit.
- **New background services**: an update can ship a systemd user unit the plugin's
  own `install.py` would have installed (Pulse 1.3.2 added `gpu-pulse.service`), and
  `omarchy plugin update` never runs `install.py`. After a successful update the
  Dashboard compares the units the plugin ships before and after, and installs and
  starts the ones that are new -- then reloads that plugin's widget so it notices. It is
  deliberately narrow: only a unit that was not shipped before (a service you removed on
  purpose stays removed), only if the plugin already has another unit installed, only a
  unit that runs the plugin's own code, and never over an existing file. A shipped unit
  that *changed* is reported, not replaced. Anything it could not start makes the update
  report a failure instead of "up to date".
- **Self-update**: the Dashboard's own row behaves like any other plugin's
  for Update and Diff. Its enable/disable switch and Remove button are
  hidden -- pulling either out from under its own running widget is asking
  for the exact kind of hot-reload-during-lock crash this plugin exists to
  avoid; use `omarchy plugin disable/remove` from a terminal for those two.

## After an update: did it actually load?

Once a plugin update (or rollback) has landed and the shell has reloaded it, the
Dashboard reads the shell's own log for anything new about that plugin. A
message that it failed to load (a QML error, a missing type) turns the update
into a failure -- with the shell's words in the output, and Roll back offered,
since the update did land. A new plain warning is shown but doesn't fail it.
Messages the plugin was already logging before the update are not counted.

## Updates run outside the panel

Updating, rolling back, installing or removing a plugin changes the plugins folder, and the shell
answers any change there by rebuilding every plugin widget -- this one too,
mid-update. So the panel doesn't run these itself: it hands them to a
transient user unit (`dashboard-job-*`) and follows `job.json` in the state
folder. A panel rebuilt halfway through still shows the job going and how it
ended, and the history entry, the load check and Roll back all still happen.
Only one job runs at a time. Package apps are the exception: their update needs sudo, so it
runs in a terminal instead (see [Apps](#apps)).

When an update changes a plugin's code (not just docs, tests or images), the
services it ships that are running are restarted, so they run the new code.

## Notifications

The "update available" notification has **Update** and **Open** buttons. Update
updates the items it named that are still updatable and not held (and verified, under
**Update all: verified only**), then says how that went, naming anything skipped; Open opens the panel. It runs as a job like any other, so an open panel shows it
going -- except package apps, which get a terminal.
The notification is waited on by a small
transient user unit (`dashboard-notify-*`), which goes away once it is clicked,
dismissed, or an hour has passed.

## Discover

The **Discover** tab (beside **Installed** under the header) lists the official plugin
catalog -- what plugins.omarchy.org and omarchyplugins.com list -- searchable, popular
(GitHub stars) or newest first. Omarchy's own built-ins are left out, as everywhere else
here. A plugin already installed (by id, or a fork of the same repo under another id) says
INSTALLED, with **Show in list**.

**Install…** appears only for entries the catalog itself says `omarchy plugin add` installs
alone; suites, monorepos and anything with its own installer say MANUAL SETUP and link to
their repo instead (untick **Installable** to see them). Install asks first, in the row, with
the warning `omarchy plugin add` gives: plugins run unsandboxed inside the shell, and what
lands is the repo's current code, which the catalog may or may not have checked (VERIFIED /
UPDATE UNVERIFIED / UNVERIFIED). Then, as a job like an update:

- `omarchy plugin add <repo>.git --yes` -- clone, validate, refuse an id already taken. The
  repo comes from the cached catalog, not from the panel, and the catalog's own
  `installCommand` is never run.
- `omarchy plugin enable <id>`, so it lands where its manifest asks to sit in the bar. A
  plugin that replaces the whole bar is installed but left off.
- If it ships systemd units, it says so: installing a plugin does not set those up.
- The same load check as an update: the shell's log decides whether it actually loaded.

The catalog is about 15 MB; `dashboard.py catalog` keeps a trimmed copy (~3 MB,
`catalog.json` in the state folder) and refetches it when it is older than 6 hours or on
**Refresh**. The regular check uses the same copy for [Marketplace
verification](#marketplace-verification). A failed fetch keeps the old list and says why.
Installs show under Recent.

Opening a row shows the catalog's screenshot of the plugin (its 720 px card image, fetched
from plugins.omarchy.org only when the row is opened).

## Keyboard and scripts

The panel answers the shell's IPC:

```bash
omarchy-shell martythedev-tech.dashboard toggle   # open, close, show, hide too
omarchy-shell martythedev-tech.dashboard discover # open on the Discover tab
omarchy-shell martythedev-tech.dashboard check    # force a check now
omarchy-shell martythedev-tech.dashboard count    # updates waiting, e.g. for a script
omarchy-shell martythedev-tech.dashboard update <plugin-or-app-id>   # as if Update was clicked (a package app opens its terminal)
```

For a key, add to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + ALT + U", "Plugin Dashboard", "omarchy-shell martythedev-tech.dashboard toggle")
```

Use the command, not `{ panel = "martythedev-tech.dashboard" }`: that form calls the
shell's generic `shell toggle`, which did not open this bar widget's popup when tried.

## Install

```bash
git clone https://github.com/martythedev-tech/omarchy-dashboard.git
cd omarchy-dashboard
python3 install.py
```

Installs under `~/.config/omarchy/plugins/martythedev-tech.dashboard`,
appends to the far-right bar, installs and enables
`dashboard-check.timer` (a check every 5 hours, and after boot), and runs one check
immediately so the panel has real data the first time you open it. Existing files and bar
layout are backed up under `~/.local/state/omarchy/backups/dashboard-TIMESTAMP/`.

`omarchy plugin add https://github.com/martythedev-tech/omarchy-dashboard.git --enable`
works too, but installs no timer: checks then happen only when the panel opens, and there
are no update notifications in between.

## Diagnosis

```bash
python3 dashboard.py check                     # one-shot, prints the status JSON
python3 dashboard.py catalog --force           # refetch the plugin catalog
python3 dashboard.py inspect-package brave     # what Add app would make of a package
systemctl --user status dashboard-check.timer
journalctl --user -u dashboard-check.service
python3 -m unittest discover -s tests -v
node tests/js/test_model.cjs
omarchy plugin validate .
```
