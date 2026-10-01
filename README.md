# Plugin Dashboard

A bar chip for every third-party and custom Omarchy plugin -- including
itself -- plus tracked local apps like Flea and Howdy, with a badge for how
many have a real update waiting, an enable/disable switch per plugin, a diff
preview, and an Update button once a check against the actual upstream (git,
AUR) confirms there's something to pull.

Omarchy's own built-ins never show up here: the plugin list comes from
`omarchy plugin list --json` filtered to `firstParty: false`, which is
exactly the "not Omarchy's own" boundary Omarchy itself already draws.

## How it works

Nothing here re-implements an update mechanism. Every action shells out to
the command that already owns it:

- **Plugins**: `omarchy plugin enable/disable <id>` for the switch,
  `omarchy plugin update <id> --yes` for the button -- Omarchy's own
  fetch + diff + fast-forward-only merge + validate + rollback --
  `omarchy plugin remove <id> --yes` for Remove. A plugin whose deployed
  directory isn't a git checkout (installed by hand rather than
  `omarchy plugin add`) shows "No update source" instead of a button,
  because there's genuinely nothing to check. A clean checkout with local
  commits that never made it upstream shows "diverged" rather than
  "behind" -- offering the button there would just hand you the fast-forward
  merge failure instead of preventing it.
- **Apps** (Flea, Howdy, and anything added via "+ Add app"): each one's
  own `updateCmd` (default `rebuild.sh --install`) in its project
  directory. The check mirrors what `rebuild.sh` itself asks -- "has the
  tracked branch (AUR) moved past what we've merged" -- not "is the
  checked-out branch different from origin," which would always be true
  for something like Flea's `aarch64-local`, deliberately ahead of the AUR
  branch it tracks.

`dashboard.py check` does the checking (git fetch + compare per item,
`pacman -Q` for app versions) and writes
`~/.local/state/omarchy/plugins/martythedev-tech.dashboard/status.json`,
which the panel reads. It runs on a timer (`dashboard-check.timer`, every
few hours) and once synchronously whenever the panel opens, so opening it
never shows data older than that. Any item that just became updatable since
the last check (not one that's been sitting there) fires a desktop
notification via `notify-send`.

## What the row tells you

Rows sort by what needs you: a stuck git operation or local changes blocking an
update first, then anything one click would update, then sources that could not
be reached, then the rest (held items with the rest) -- alphabetical within each.

Click a row's name to expand it: its full status, the branch it is on, when
upstream last committed, a link to its source (the GitHub repo, or the AUR
package page for an app), and **Open folder** / **Terminal** buttons for its
checkout.

**Update all** ends with a one-line summary ("2 updated, 1 failed (Flea).") and
leaves the first failure's output open.

- **+N LOCAL**: the checkout carries N commits upstream does not have -- a fork you
  maintain, or a local fix you are holding. Shown next to CURRENT or BEHIND so it is not
  mistaken for an untouched install.
- **NOT INSTALLED** (apps): the repo is current but the installed package is older than
  the version its `.SRCINFO` builds -- an update whose build or install step never ran.
  Update runs the app's `updateCmd`, which builds and installs it.

Checks run side by side, so a full check takes about as long as the slowest single
fetch rather than the sum of them.

## Extra actions

- **Changes**: for anything `behind` or `diverged`, lists the commits the
  update brings (and, for a plugin, the local ones upstream lacks), with a Diff
  tab for the full `git diff` against the fetched upstream.
- **Hold**: from the Changes view, keeps an update you don't want yet out of
  the badge, Update all and notifications; the row shows HELD and an Unhold
  button. Holds live in `holds.json` in the state directory.
- **Roll back** (plugins): for a week after the Dashboard updates a plugin, and
  as long as its checkout hasn't moved since, puts it back where the update
  found it -- `git reset --hard` to the old commit (refused over local changes),
  validate (the update is restored if the old version no longer validates), stop
  and remove any service the update had started that the old version doesn't
  ship, the plugin's own `run.sh --ensure`, and a plugin rescan. It then holds
  the plugin, so the same update isn't offered straight back. Apps are rebuilt
  from source, so they have no Roll back.
- **Recent**: every update and rollback is recorded in `history.json` (last
  50); the newest few are listed at the bottom of the panel.
- **Update all**: appears once more than one item is updatable; updates them
  one at a time.
- **Remove**: `omarchy plugin remove <id> --yes` for plugins, or just drops
  an app from the tracked list. Needs a second click within 4s to confirm --
  for a plugin without local edits this deletes its directory outright ("its
  git repo remains upstream" is Omarchy's own justification), so don't
  confirm one you haven't actually finished with.
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

## Notifications

The "update available" notification has **Update** and **Open** buttons. Update
updates the items it named that are still updatable and not held, then says how
that went; Open opens the panel. The notification is waited on by a small
transient user unit (`dashboard-notify-*`), which goes away once it is clicked,
dismissed, or an hour has passed.

## Keyboard and scripts

The panel answers the shell's IPC:

```bash
omarchy-shell martythedev-tech.dashboard toggle   # open, close, show, hide too
omarchy-shell martythedev-tech.dashboard check    # force a check now
omarchy-shell martythedev-tech.dashboard count    # updates waiting, e.g. for a script
```

For a key, add to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + ALT + U", "Plugin Dashboard", "omarchy-shell martythedev-tech.dashboard toggle")
```

Use the command, not `{ panel = "martythedev-tech.dashboard" }`: that form calls the
shell's generic `shell toggle`, which did not open this bar widget's popup when tried.

## Tracked apps

Use "+ Add app" in the panel. Start with the repo: **Choose…** opens the
desktop's folder chooser, or type a path and press **Fill in**. Either reads the
repo and fills the rest -- name and id from the folder, the package name from
`.SRCINFO`, the upstream branch and remote from git, and `./rebuild.sh --install`
if the repo has an executable `rebuild.sh` -- and says what it could not work
out. Saving checks the repo dir is a real git checkout and that there is an
update command. Remove one the same way as a
plugin, via its Remove button -- that only forgets it here, it never touches
the repo or package.

Under the hood, apps live in a small file,
`~/.local/state/omarchy/plugins/martythedev-tech.dashboard/apps.json`,
seeded on first run with Flea and Howdy, and can still be hand-edited:

```json
[
  {"id": "flea", "name": "Flea", "repoDir": "/home/you/Projects/flea", "pkgName": "flea",
   "branch": "master", "remote": "origin", "updateCmd": ["./rebuild.sh", "--install"]}
]
```

Add another app the same way: a repo with a `master` (or whatever
`branch` names) tracking its real upstream, and an `updateCmd` that
builds and installs from the checked-out working tree.

## Install

```bash
git clone https://github.com/martythedev-tech/omarchy-dashboard.git
cd omarchy-dashboard
python3 install.py
```

Installs under `~/.config/omarchy/plugins/martythedev-tech.dashboard`,
appends to the far-right bar, installs and enables
`dashboard-check.timer`, and runs one check immediately so the panel has
real data the first time you open it. Existing files and bar layout are
backed up under `~/.local/state/omarchy/backups/dashboard-TIMESTAMP/`.

## Diagnosis

```bash
python3 dashboard.py check                     # one-shot, prints the status JSON
systemctl --user status dashboard-check.timer
journalctl --user -u dashboard-check.service
python3 -m unittest discover -s tests -v
node tests/js/test_model.cjs
omarchy plugin validate .
```
