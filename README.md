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

## Extra actions

- **Diff**: for anything `behind` or `diverged`, shows `git diff` against
  the fetched upstream before you commit to an Update.
- **Update all**: appears once more than one item is updatable; updates them
  one at a time.
- **Remove**: `omarchy plugin remove <id> --yes` for plugins, or just drops
  an app from the tracked list. Needs a second click within 4s to confirm --
  for a plugin without local edits this deletes its directory outright ("its
  git repo remains upstream" is Omarchy's own justification), so don't
  confirm one you haven't actually finished with.
- **Self-update**: the Dashboard's own row behaves like any other plugin's
  for Update and Diff. Its enable/disable switch and Remove button are
  hidden -- pulling either out from under its own running widget is asking
  for the exact kind of hot-reload-during-lock crash this plugin exists to
  avoid; use `omarchy plugin disable/remove` from a terminal for those two.

## Tracked apps

Use "+ Add app" in the panel: name, id, repo dir, and it validates the repo
dir is a real git checkout before saving. Remove one the same way as a
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
