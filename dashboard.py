#!/usr/bin/env python3
"""Dashboard: every third-party/own Omarchy plugin plus tracked local apps,
checked against their real upstream. No bespoke update logic of its own --
every action shells out to the command that already owns it (`omarchy plugin
enable/disable/update/remove` for plugins, each source app's own rebuild.sh,
`omarchy update` or `yay -S` for package apps) and this script's only job is deciding when an Update button should
show and reporting what that command did.

One exception: a plugin whose own run.sh implements `--ensure` (2026-09-12,
Omastorm) pins a native binary separately from its git content, fetched and
swapped in by that same command. `omarchy plugin update` finishing (and the
shell hot-reload it triggers) does not reliably re-run it -- confirmed live:
a real update landed the new git content while the old engine binary and its
daemon sat untouched for minutes with nothing in its own bootstrap log, no
error anywhere, and the Dashboard still reported "up to date" throughout,
because its update_state check only ever looks at git HEAD. cmd_update runs
that same command itself, synchronously, right after a successful git
update, so what gets reported here reflects whether the plugin actually
finished coming up -- still the plugin's own command, just called directly
instead of trusted to happen on its own.

The second exception (2026-09-21, Pulse 1.3.0 -> 1.3.2): an update can ship a NEW
systemd user unit (Pulse added a GPU recorder, `gpu-pulse.service`). The units are
installed by the plugin's own install.py, which `omarchy plugin update` never runs, so
the new recorder did not exist, the widget read it as offline, and Pulse ranks an
offline recorder as the worst thing on the machine: the bar showed "GPU · OFFLINE" in
place of its live readings while the dashboard said "up to date". cmd_update now
compares the units the plugin ships before and after the update and installs the ones
that are new (see install_new_units for the guards).

The third (2026-10-05): updates, rollbacks and removals run as a job in their own transient
user unit, not as a child of the panel (see JOB_KINDS for why), and an update that changed
a plugin's code restarts the services it ships (restart_updated_units).

The fourth (2026-10-05, 1.9.0): Remove also stops and removes the plugin's services and,
if asked, trashes its data folders (cmd_remove); `omarchy plugin remove` touches only the
plugin's own folder. Leftovers lists what removals left behind.

The fifth (2026-10-23, 1.11.0): Discover browses the official plugin catalog and installs
from it through `omarchy plugin add` (cmd_catalog, cmd_install).

The sixth (2026-10-23, 1.12.0): package apps (pacman/AUR, e.g. brave-bin) update in a
terminal, where sudo can ask (cmd_terminal_update); a source app can be pulled before its
update command runs (pull_upstream), and is checked again afterwards.
"""
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SELF_ID = 'martythedev-tech.dashboard'
BINDINGS_PATH = Path.home() / '.config/hypr/bindings.lua'
PLUGINS_DIR = Path.home() / '.config/omarchy/plugins'
STATE = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'omarchy/plugins' / SELF_ID
STATUS_PATH = STATE / 'status.json'
APPS_PATH = STATE / 'apps.json'
USER_UNIT_DIR = Path.home() / '.config/systemd/user'
CHECK_CACHE_SECONDS = 300
# Where a plugin's units are looked for: its top level and one directory down (Pulse keeps
# them in collectors/), never in the places a plugin keeps other people's files.
UNIT_SUFFIXES = ('.service', '.timer')
UNIT_SKIP_DIRS = {'.git', 'tests', 'docs', 'node_modules', '__pycache__'}
# How long to let a freshly started recorder write its first reading before the widget is
# reloaded to find it (the widget's file watch is set up at load, and a file that did not
# exist then is never noticed).
UNIT_SETTLE_SECONDS = 8
# Every item's check is a network fetch with its own 20 s timeout. One at a time, a dozen
# items took ~8 s on a good connection and would take minutes offline; side by side the
# whole check costs about as long as the slowest single fetch.
CHECK_WORKERS = 8
# The states an Update click can act on: 'behind' pulls new commits, 'not-installed'
# builds and installs what the repo already has (an app's updateCmd does both).
UPDATABLE_STATES = ('behind', 'not-installed')
# history.json keeps this many entries; the panel shows the newest few.
HISTORY_LIMIT = 50
HISTORY_SHOWN = 8
# A plugin can be rolled back from its last update for this long. Past it, the Roll back
# button would sit on every row the Dashboard ever updated.
ROLLBACK_WINDOW_SECONDS = 7 * 24 * 3600
# Commits listed per side in the "what's new" view.
LOG_LIMIT = 50
# The shell writes its log under the runtime dir, one directory per shell instance.
SHELL_LOG_ROOT = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}') / 'quickshell/by-id'
# How long after the last reload trigger the shell is given to (re)load a plugin before
# its log is read. Loading is asynchronous; a broken plugin logs its failure within a
# second or two.
LOAD_SETTLE_SECONDS = 4
_LOG_LEVEL = re.compile(r'^\S+ \S+\s+(WARN|ERROR|CRIT|FATAL)\b')
_LOG_STAMP = re.compile(r'^\S+ \S+\s+')
# What the shell says when a plugin did not load at all, as opposed to a warning from one
# that did (Omastorm logs an IPC-handler warning on every load and works fine).
_LOAD_FAILURE = re.compile(r'failed|\b\w*Error\b|is not a type|is not installed|unavailable', re.I)
# A notification's buttons wait for a click; past this its waiter gives up.
NOTIFY_WAIT_SECONDS = 3600
# Changed files that do not make a plugin's running services stale.
_NOT_CODE = re.compile(r'(^|/)(docs|tests)/|\.(md|txt|png|jpe?g|gif|svg|webp)$|(^|/)LICENSE$', re.I)
# The actions that change a plugin's directory. The shell answers any such change by
# unloading and reloading every plugin widget, this one included, and a process the panel
# started dies with it (QProcess kills its child on destruction): 2026-10-05, three updates
# from the panel landed but none reached history. So these run as a job in their own
# transient user unit, and the panel follows job.json instead of a process's output.
JOB_KINDS = ('update', 'rollback', 'remove', 'cleanup', 'install')
# A job that is 'starting' this long without its worker having taken it never will.
JOB_START_GRACE_SECONDS = 60

# Seeded into APPS_PATH the first time it's missing -- only those whose checkout exists, so
# anyone else installing this starts with no apps rather than two rows for folders they do
# not have. From then on the file on disk is what's read; edit it there (or via the
# "+ Add app" form) to add or change tracked apps.
DEFAULT_APPS = [
    {'id': 'flea', 'name': 'Flea', 'repoDir': str(Path.home() / 'Projects/flea'), 'pkgName': 'flea',
     'branch': 'master', 'remote': 'origin', 'updateCmd': ['./rebuild.sh', '--install']},
    {'id': 'howdy', 'name': 'Howdy', 'repoDir': str(Path.home() / 'Projects/howdy'), 'pkgName': 'howdy',
     'branch': 'master', 'remote': 'origin', 'updateCmd': ['./rebuild.sh', '--install']},
]


def run(args, cwd=None, timeout=20, stream_log=None):
    """(returncode, stdout, stderr); never raises -- a missing binary or a
    timeout is just another kind of failure the caller already has to handle.

    stream_log: if set to a path, merge stdout/stderr and append each line to
    that file as it arrives so the panel can tail a long update (Flea's cargo
    build) instead of sitting on '... 47s' until the process exits.
    """
    if stream_log:
        return run_live(args, cwd=cwd, timeout=timeout, log_path=stream_log)
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, check=False)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def run_live(args, cwd=None, timeout=900, log_path=None):
    log_path = Path(log_path or (STATE / 'action.log'))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=cwd, bufsize=1,
        )
    except OSError as e:
        return 1, '', str(e)
    chunks = []
    deadline = time.time() + timeout
    try:
        with log_path.open('w') as log:
            while True:
                if time.time() > deadline:
                    p.kill()
                    p.wait()
                    return 1, ''.join(chunks), f'timed out after {timeout}s'
                line = p.stdout.readline() if p.stdout else ''
                if line == '' and p.poll() is not None:
                    break
                if line:
                    chunks.append(line)
                    log.write(line)
                    log.flush()
        return p.wait(), ''.join(chunks), ''
    except Exception as e:
        p.kill()
        return 1, ''.join(chunks), str(e)


def status_is_fresh(status, now=None, max_age=CHECK_CACHE_SECONDS):
    if not status or not status.get('ts'):
        return False
    return ((now if now is not None else time.time()) - status['ts']) < max_age


def load_status():
    try:
        data = json.loads(STATUS_PATH.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return None


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path, data, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text((json.dumps(data, separators=(',', ':')) if compact else json.dumps(data, indent=2)) + '\n')
    tmp.replace(path)


# Read through STATE at call time (not fixed at import), so a test that points STATE
# somewhere else takes these with it.
def history_path():
    return STATE / 'history.json'


def holds_path():
    return STATE / 'holds.json'


def load_history():
    return _read_json(history_path(), [])


def append_history(entry):
    _write_json(history_path(), (load_history() + [entry])[-HISTORY_LIMIT:])


def load_holds():
    """{id: {'ts', 'reason'}} for everything held: left out of the badge, Update all and
    notifications until unheld, because its update is not wanted yet (a local fix waiting
    on upstream, or an update that was just rolled back)."""
    return _read_json(holds_path(), {})


def save_holds(holds):
    _write_json(holds_path(), holds)


def settings_path():
    return STATE / 'settings.json'


def update_all_scope():
    """'verified' when Update all (and a notification's Update) should take only plugin
    updates that bring the commit the marketplace verified; 'all' otherwise. The panel's
    footer writes it."""
    settings = _read_json(settings_path(), {})
    return 'verified' if isinstance(settings, dict) and settings.get('updateAllScope') == 'verified' else 'all'


def in_update_all(item, scope):
    """Whether Update all takes this item under that scope. Apps are not in the marketplace
    and always go; a plugin goes under 'verified' only if its update is the verified commit
    -- one that is unlisted, or whose catalog could not be read, does not."""
    if scope != 'verified' or item.get('kind') == 'app':
        return True
    return (item.get('market') or {}).get('incoming') == 'verified'


def repo_head(repo_dir):
    rc, out, _ = run(['git', '-C', str(repo_dir), 'rev-parse', 'HEAD'])
    return out.strip() if rc == 0 else ''


def git_log(repo_dir, rev_range):
    """[{hash, subject, author, ts}] for rev_range, newest first, at most LOG_LIMIT."""
    rc, out, _ = run(['git', '-C', str(repo_dir), 'log', f'-n{LOG_LIMIT}',
                      '--format=%h%x1f%s%x1f%an%x1f%ct', rev_range], timeout=20)
    commits = []
    if rc != 0:
        return commits
    for line in out.splitlines():
        parts = line.split('\x1f')
        if len(parts) == 4:
            commits.append({'hash': parts[0], 'subject': parts[1], 'author': parts[2],
                            'ts': int(parts[3]) if parts[3].isdigit() else 0})
    return commits


def load_apps():
    if APPS_PATH.exists():
        try:
            data = json.loads(APPS_PATH.read_text())
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            pass
    return default_apps()


def default_apps():
    return [a for a in DEFAULT_APPS if (Path(a['repoDir']) / '.git').is_dir()]


def save_apps(apps):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = APPS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(apps, indent=2) + '\n')
    tmp.replace(APPS_PATH)


def ensure_apps_file():
    if APPS_PATH.exists():
        return
    save_apps(default_apps())


def discover_plugins():
    """Every plugin `omarchy plugin list` reports as not first-party -- the
    exact "not Omarchy's own" boundary Omarchy itself already draws. This now
    includes the dashboard's own entry, so it can check and update itself
    too; the CLI layer (cmd_disable/cmd_remove) separately refuses to touch
    SELF_ID for the two actions that would pull the shell out from under its
    own running widget."""
    rc, out, _ = run(['omarchy', 'plugin', 'list', '--json'])
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    # The list is the shell's registry, which rescans in the background: just after a
    # removal it still names a plugin whose folder is already gone (2026-10-05, CPU Pulse
    # lingered as a row until the next check). Every plugin listed here lives in
    # PLUGINS_DIR, so the folder on disk decides.
    return [p for p in data if not p.get('firstParty') and isinstance(p.get('id'), str)
            and (PLUGINS_DIR / p['id']).exists()]


def manifest_version(plugin_id):
    path = PLUGINS_DIR / plugin_id / 'manifest.json'
    try:
        return json.loads(path.read_text()).get('version', '')
    except (OSError, ValueError):
        return ''


def manifest_description(plugin_id):
    path = PLUGINS_DIR / plugin_id / 'manifest.json'
    try:
        return str(json.loads(path.read_text()).get('description') or '')[:300]
    except (OSError, ValueError):
        return ''


def pkg_description(pkg_name):
    """The installed package's one-line description, or '' (the row's second line)."""
    rc, out, _ = run(['pacman', '-Qi', pkg_name])
    for line in out.splitlines() if rc == 0 else []:
        key, _, value = line.partition(':')
        if key.strip() == 'Description':
            return value.strip()[:300]
    return ''


def has_ensure_bootstrap(plugin_dir):
    """Whether this plugin owns a run.sh implementing `--ensure` -- the convention a
    plugin uses to fetch/swap a native binary it pins separately from its git content
    (Omastorm's engine daemon is the first). Generic on purpose: covers any future
    plugin that adopts the same pattern, not just the one that surfaced it."""
    run_sh = Path(plugin_dir) / 'run.sh'
    try:
        return run_sh.is_file() and '--ensure' in run_sh.read_text()
    except OSError:
        return False


def run_ensure_bootstrap(plugin_dir):
    """Runs the plugin's own bootstrap synchronously and waits for it, rather than
    trusting the shell's post-update hot-reload to have triggered it on its own --
    confirmed it does not always. Safe to call even when nothing needed fetching:
    run.sh --ensure is the plugin's own idempotent readiness check, so a build that's
    already current just confirms that and returns quickly."""
    return run(['bash', 'run.sh', '--ensure'], cwd=str(plugin_dir), timeout=60)


def shipped_units(plugin_dir):
    """{unit name: (path, sha256)} for every systemd unit the plugin ships, so what an
    update adds or changes can be told from what was already there."""
    plugin_dir = Path(plugin_dir)
    found = {}
    try:
        candidates = list(plugin_dir.iterdir())
        for child in list(candidates):
            if child.is_dir() and child.name not in UNIT_SKIP_DIRS and not child.name.startswith('.'):
                candidates.extend(child.iterdir())
        for path in candidates:
            if path.is_file() and path.suffix in UNIT_SUFFIXES and path.name not in found:
                found[path.name] = (path, hashlib.sha256(path.read_bytes()).hexdigest())
    except OSError:
        pass
    return found


def install_new_units(plugin_dir, before):
    """Installs the systemd user units an update ADDED, which `omarchy plugin update`
    does not do (the plugin's install.py does, and nothing runs that). Returns
    (messages, ok). Deliberately narrow, because starting a background service is not a
    small thing to do unasked:

      * only a unit that was not shipped before the update. A unit the user removed on
        purpose stays removed, because it was not new;
      * only if the plugin already has another unit installed, i.e. it was set up with
        services in the first place. A plugin whose services were never installed is
        left alone;
      * only a unit that mentions the plugin's own directory, i.e. it runs that plugin's
        code, not something else;
      * never overwrites a unit file that already exists.

    A shipped unit that CHANGED is reported, not replaced: the installed copy may carry
    the user's own edits.
    """
    plugin_dir = Path(plugin_dir)
    after = shipped_units(plugin_dir)
    new = sorted(name for name in after if name not in before)
    changed = sorted(name for name in after if name in before and after[name][1] != before[name][1]
                     and (USER_UNIT_DIR / name).exists())
    messages = []
    for name in changed:
        messages.append(f'{name} changed in this update; the installed copy was left as it is '
                        f'(run the plugin\'s install.py to refresh it).')
    if not new:
        return messages, True
    if not any((USER_UNIT_DIR / name).exists() for name in after if name not in new):
        return messages, True
    ok = True
    started = []
    for name in new:
        target = USER_UNIT_DIR / name
        if target.exists():
            continue
        try:
            text = after[name][0].read_text()
        except (OSError, UnicodeDecodeError) as e:
            messages.append(f'Could not read the new unit {name}: {e}')
            ok = False
            continue
        if plugin_dir.name not in text:
            messages.append(f'{name} is new in this update but does not run this plugin\'s own code; '
                            f'not installed.')
            continue
        try:
            USER_UNIT_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            target.chmod(0o644)
        except OSError as e:
            messages.append(f'Could not install {name}: {e}')
            ok = False
            continue
        rc, out, err = run(['systemctl', '--user', 'daemon-reload'], timeout=30)
        if rc == 0:
            rc, out, err = run(['systemctl', '--user', 'enable', '--now', name], timeout=60)
        if rc != 0:
            messages.append(f'Installed {name} but could not start it: {(err or out).strip()[-300:]}')
            ok = False
        else:
            messages.append(f'Installed and started {name}, which is new in this update.')
            started.append(name)
    if started:
        reload_plugin_widget(plugin_dir)
    return messages, ok


def reload_plugin_widget(plugin_dir):
    """Makes the shell hot-reload just this plugin (it watches the plugin directory), after
    a pause for the recorder just started to write its first reading. Omarchy has no
    command for this; touching the manifest is what its own file watch reacts to."""
    time.sleep(UNIT_SETTLE_SECONDS)
    try:
        os.utime(Path(plugin_dir) / 'manifest.json')
    except OSError:
        pass


def restart_updated_units(plugin_dir, old_head, new_head, units_before):
    """Restarts the running services this plugin already had installed when the code
    between old_head and new_head changed (2026-10-05, Pulse 1.3.2 -> 1.3.5: the update
    changed collectors/cpu_pulse.py, and cpu-pulse.service went on running the old code
    for hours). Docs, tests and images do not count as code. Only services: a timer's
    service starts fresh each time anyway. Only units that run this plugin's own code,
    the same guard install_new_units uses. Returns (messages, ok)."""
    plugin_dir = Path(plugin_dir)
    if not old_head or not new_head or old_head == new_head:
        return [], True
    rc, out, _ = run(['git', '-C', str(plugin_dir), 'diff', '--name-only', old_head, new_head])
    if rc != 0 or not any(f.strip() and not _NOT_CODE.search(f) for f in out.splitlines()):
        return [], True
    messages, ok, restarted = [], True, []
    for name in sorted(units_before):
        target = USER_UNIT_DIR / name
        if not name.endswith('.service') or not target.exists():
            continue
        try:
            if plugin_dir.name not in target.read_text():
                continue
        except (OSError, UnicodeDecodeError):
            continue
        _, state, _ = run(['systemctl', '--user', 'is-active', name], timeout=10)
        if state.strip() != 'active':
            continue
        rc, out, err = run(['systemctl', '--user', 'restart', name], timeout=60)
        if rc == 0:
            restarted.append(name)
        else:
            ok = False
            messages.append(f'{name} did not restart: {(err or out).strip()[-300:]}')
    if restarted:
        messages.insert(0, 'Restarted ' + ', '.join(restarted) + ' to run the new code.')
    return messages, ok


def shell_log():
    """The running shell's log: the newest one, since every shell restart starts a new
    instance directory and the old ones stay behind."""
    try:
        logs = [d / 'log.log' for d in SHELL_LOG_ROOT.iterdir() if (d / 'log.log').is_file()]
    except OSError:
        return None
    return max(logs, key=lambda f: f.stat().st_mtime) if logs else None


def shell_log_mark():
    """(log path, size) now, so what is logged after an update can be told from before."""
    log = shell_log()
    try:
        return (log, log.stat().st_size) if log else (None, 0)
    except OSError:
        return (None, 0)


def _plugin_lines(plugin_id, plugin_dir, text):
    """WARN-or-worse lines about this plugin, timestamps stripped so the same message
    from an earlier load compares equal."""
    paths = {f'/plugins/{plugin_id}/'}
    try:
        paths.add(str(Path(plugin_dir).resolve()) + '/')  # the Dashboard is a symlink
    except OSError:
        pass
    named = re.compile(r'(?i)(?:plugin(?: widget)?|failed for) ' + re.escape(plugin_id) + r'(?![\w.-])')
    found = []
    for line in text.splitlines():
        if _LOG_LEVEL.match(line) and (any(p in line for p in paths) or named.search(line)):
            found.append(_LOG_STAMP.sub('', line, count=1).strip())
    return found


def new_plugin_problems(plugin_id, plugin_dir, mark):
    """(new lines, failed) for this plugin since `mark`, after giving the shell
    LOAD_SETTLE_SECONDS to reload it. Only messages it had not already logged before the
    mark count -- a warning it gives on every load is not news. `failed` is whether any of
    them says the plugin did not load.

    This is the check none of the earlier fixes made: Omastorm's engine and Pulse's GPU
    recorder each left the widget broken after an update that reported success, and so
    would a QML error in the new version, which nothing here looked for."""
    time.sleep(LOAD_SETTLE_SECONDS)
    log_before, offset = mark
    log_now = shell_log()
    if not log_now:
        return [], False
    try:
        text_now = log_now.read_text(errors='replace')
        text_before = log_before.read_text(errors='replace') if log_before else ''
    except OSError:
        return [], False
    if log_now == log_before:
        earlier, later = text_now[:offset], text_now[offset:]
    else:  # the shell restarted in between: all of the new log is new
        earlier, later = text_before, text_now
    seen = set(_plugin_lines(plugin_id, plugin_dir, earlier))
    lines = []
    for line in _plugin_lines(plugin_id, plugin_dir, later):
        if line not in seen and line not in lines:
            lines.append(line)
    return lines, any(_LOAD_FAILURE.search(line) for line in lines)


def check_plugin_loaded(plugin_id, plugin_dir, mark):
    """(ok, message) for the end of an update or rollback: '' when the shell said nothing
    new about the plugin."""
    lines, failed = new_plugin_problems(plugin_id, plugin_dir, mark)
    if not lines:
        return True, ''
    shown = '\n'.join(lines[:8]) + (f'\n… and {len(lines) - 8} more' if len(lines) > 8 else '')
    if failed:
        return False, 'The shell could not load it:\n' + shown
    return True, 'The shell logged new warnings after reloading it:\n' + shown


def pkg_version(pkg_name):
    rc, out, _ = run(['pacman', '-Q', pkg_name])
    if rc != 0:
        return ''
    parts = out.strip().split()
    return parts[1] if len(parts) > 1 else ''


def installed_name(pkg_name):
    """The installed package's own name, or '' -- `pacman -Q brave` answers for brave-bin,
    which provides it, and tracking 'brave' would check the wrong package."""
    rc, out, _ = run(['pacman', '-Q', pkg_name])
    parts = out.split() if rc == 0 else []
    return parts[0] if parts else ''


def repo_operation_in_progress(repo_dir):
    """Name of a git operation left mid-flight (rebase/merge/cherry-pick/
    bisect), or '' if the tree is otherwise idle. Checked before comparing
    against upstream at all: an ahead/behind count computed while one of
    these is running names a state that's about to change again once it's
    resolved, and offering Update mid-rebase would hand `updateCmd` a
    checkout it can't even `git checkout` out of -- confirmed live,
    2026-09-16 (Flea's aarch64-local was found mid-rebase, left behind by an
    earlier `rebuild.sh --install` run through this same Dashboard, and
    neither plugin_update_state nor app_update_state had ever checked for
    it)."""
    git_dir = Path(repo_dir) / '.git'
    if (git_dir / 'rebase-merge').is_dir() or (git_dir / 'rebase-apply').is_dir():
        try:
            onto = (git_dir / 'rebase-merge' / 'onto').read_text().strip()[:7]
        except OSError:
            onto = ''
        return 'rebase onto ' + onto if onto else 'rebase'
    if (git_dir / 'MERGE_HEAD').is_file():
        return 'merge'
    if (git_dir / 'CHERRY_PICK_HEAD').is_file():
        return 'cherry-pick'
    if (git_dir / 'BISECT_LOG').is_file():
        return 'bisect'
    return ''


def plugin_update_state(plugin_dir):
    """Mirrors omarchy-plugin-update's own check exactly (fetch origin's
    HEAD, compare against ours) so "update available" here always agrees
    with what `omarchy plugin update` would actually do -- including its
    fast-forward-only merge, which is why a clean tree with local commits
    that aren't upstream is reported as 'diverged' rather than 'behind': the
    Update button only ever fires on 'behind', and offering it here would
    just hand the user a merge failure `omarchy plugin update` can't recover
    from on its own.
    """
    plugin_dir = Path(plugin_dir)
    if not (plugin_dir / '.git').is_dir():
        return 'no-repo', 0, ''
    op = repo_operation_in_progress(plugin_dir)
    if op:
        return 'in-progress', 0, op
    rc, _, err = run(['git', '-C', str(plugin_dir), 'fetch', '--quiet', 'origin', 'HEAD'], timeout=20)
    if rc != 0:
        return 'unreachable', 0, err.strip()[-500:]
    rc1, head, _ = run(['git', '-C', str(plugin_dir), 'rev-parse', 'HEAD'])
    rc2, fetch_head, _ = run(['git', '-C', str(plugin_dir), 'rev-parse', 'FETCH_HEAD'])
    if rc1 != 0 or rc2 != 0:
        return 'unreachable', 0, 'could not resolve HEAD/FETCH_HEAD'
    if head.strip() == fetch_head.strip():
        return 'up-to-date', 0, ''
    rc3, count, _ = run(['git', '-C', str(plugin_dir), 'rev-list', '--count', 'HEAD..FETCH_HEAD'])
    behind = int(count.strip()) if rc3 == 0 and count.strip().isdigit() else 0
    if behind == 0:
        return 'up-to-date', 0, ''
    rc4, dirty, _ = run(['git', '-C', str(plugin_dir), 'status', '--porcelain'])
    rc5, ahead_count, _ = run(['git', '-C', str(plugin_dir), 'rev-list', '--count', 'FETCH_HEAD..HEAD'])
    ahead = int(ahead_count.strip()) if rc5 == 0 and ahead_count.strip().isdigit() else 0
    if not dirty.strip() and ahead == 0:
        return 'behind', behind, ''
    # Local edits or commits no longer block the Update button on their own: carry_update()
    # sets them aside, updates, and puts them back on top. Only a change that collides with
    # the incoming one still needs a person -- 2026-10-20, six plugins sat DIRTY over a
    # one-line Qt 6.12 colour fix, three of which upstream had since made itself.
    carries, conflicts = local_work_carries(plugin_dir)
    _, untracked_clash = untracked_in_update(plugin_dir)
    if carries and not untracked_clash:
        return 'behind', behind, ''
    if untracked_clash:
        conflicts = ', '.join(([conflicts] if conflicts else []) + untracked_clash[:3])
    clash = f' -- clashes with the update in {conflicts}' if conflicts else ''
    if dirty.strip():
        lines = dirty.strip().split('\n')
        return 'dirty', behind, f'{len(lines)} file(s) changed locally{clash}'
    return 'diverged', behind, f'{ahead} local commit(s) not upstream{clash}'


def app_update_state(repo_dir, branch, remote):
    """Mirrors rebuild.sh's own check exactly: is `remote/branch` a commit
    our local `branch` hasn't incorporated yet -- not "is HEAD different from
    origin/HEAD", which would always be true for a checkout like Flea's
    aarch64-local that's meant to diverge from the AUR-tracking branch."""
    repo_dir = Path(repo_dir)
    if not (repo_dir / '.git').is_dir():
        return 'no-repo', 0, ''
    op = repo_operation_in_progress(repo_dir)
    if op:
        return 'in-progress', 0, op
    rc, _, err = run(['git', '-C', str(repo_dir), 'fetch', '--quiet', remote], timeout=20)
    if rc != 0:
        return 'unreachable', 0, err.strip()[-500:]
    remote_ref = f'{remote}/{branch}'
    rc1, _, _ = run(['git', '-C', str(repo_dir), 'rev-parse', '--verify', branch])
    rc2, _, _ = run(['git', '-C', str(repo_dir), 'rev-parse', '--verify', remote_ref])
    if rc1 != 0 or rc2 != 0:
        return 'unreachable', 0, f'could not resolve {branch}/{remote_ref}'
    rc3, _, _ = run(['git', '-C', str(repo_dir), 'merge-base', '--is-ancestor', remote_ref, branch])
    counted = branch
    if rc3 == 0:
        # `branch` has it, but the app is built from what is CHECKED OUT, which for Flea is
        # aarch64-local, a different branch that has to merge `branch` in. An update that
        # moved master and was then aborted leaves master current and the build stale.
        rc_head, _, _ = run(['git', '-C', str(repo_dir), 'merge-base', '--is-ancestor', remote_ref, 'HEAD'])
        if rc_head == 0:
            return 'up-to-date', 0, ''
        counted = 'HEAD'
    rc4, count, _ = run(['git', '-C', str(repo_dir), 'rev-list', '--count', f'{counted}..{remote_ref}'])
    behind = int(count.strip()) if rc4 == 0 and count.strip().isdigit() else 0
    if behind == 0:
        return 'up-to-date', 0, ''
    rc5, dirty, _ = run(['git', '-C', str(repo_dir), 'status', '--porcelain'])
    if dirty.strip():
        lines = dirty.strip().split('\n')
        return 'dirty', behind, f'{len(lines)} file(s) changed locally'
    return 'behind', behind, ''


def local_commit_count(repo_dir, upstream_ref):
    """Commits on HEAD that upstream_ref does not have. 'up-to-date' and 'behind' say
    nothing about these on their own, so a fork carrying its own work (omamail's
    ews-support, 92 commits) or a held local fix (Power Pulse's, 1) read as plain
    CURRENT, the same as an untouched checkout."""
    rc, out, _ = run(['git', '-C', str(repo_dir), 'rev-list', '--count', f'{upstream_ref}..HEAD'])
    return int(out.strip()) if rc == 0 and out.strip().isdigit() else 0


def local_edit_count(repo_dir):
    """Uncommitted changes to tracked files -- the ones an update has to set aside and put
    back. Untracked files are left where they are and never counted."""
    rc, out, _ = run(['git', '-C', str(repo_dir), 'status', '--porcelain', '--untracked-files=no'])
    return len(out.strip().splitlines()) if rc == 0 and out.strip() else 0


def untracked_in_update(plugin_dir, upstream_ref='FETCH_HEAD'):
    """Untracked files the update adds at the same path, as (same, differ): those already
    byte-identical to the update's copy, and those that are not. A merge refuses to
    overwrite either kind, and neither stash nor merge-tree sees them. The usual source of
    the first kind is a new file written into the installed copy, pushed from there, and
    now coming back as part of the update -- 2026-10-22, Touchpad Glance's
    touchpad-typing-guard failed its update that way."""
    d = str(plugin_dir)
    rc, out, _ = run(['git', '-C', d, 'ls-files', '--others', '--exclude-standard', '-z'])
    if rc != 0:
        return [], []
    same, differ = [], []
    for path in filter(None, out.split('\0')):
        rc, theirs, _ = run(['git', '-C', d, 'rev-parse', '--verify', '--quiet', f'{upstream_ref}:{path}'])
        if rc != 0:
            continue
        rc, ours, _ = run(['git', '-C', d, 'hash-object', '--', path])
        (same if rc == 0 and ours.strip() == theirs.strip() else differ).append(path)
    return same, differ


def has_local_work(plugin_dir):
    """Whether an update has anything of this checkout's own to carry over."""
    return bool(local_edit_count(plugin_dir) or local_commit_count(plugin_dir, 'FETCH_HEAD')
                or any(untracked_in_update(plugin_dir)))


def local_work_carries(plugin_dir, upstream_ref='FETCH_HEAD'):
    """(True, '') when this checkout's own work -- local commits and uncommitted edits
    together -- merges cleanly onto upstream_ref; (False, 'a.qml, b.qml') naming the
    conflicting files when it does not. A dry run in git's object store (`stash create`
    writes a commit without touching the tree or the stash list, `merge-tree` merges without
    a checkout), so a check can ask on every pass. An edit upstream has since made itself
    merges as clean, which is what lets such a plugin update instead of sitting DIRTY."""
    d = str(plugin_dir)
    rc, base, _ = run(['git', '-C', d, 'merge-base', 'HEAD', upstream_ref])
    if rc != 0:
        return False, ''
    _, stash, _ = run(['git', '-C', d, 'stash', 'create'])
    tip = stash.strip() or 'HEAD'
    rc, out, _ = run(['git', '-C', d, 'merge-tree', '--write-tree', '--name-only',
                      f'--merge-base={base.strip()}', tip, upstream_ref])
    if rc == 0:
        return True, ''
    # Output: the tree id, the conflicted paths, a blank line, then informational messages.
    files = []
    for line in out.splitlines()[1:]:
        if not line.strip():
            break
        files.append(line.strip())
    return False, ', '.join(files[:3]) + (f' and {len(files) - 3} more' if len(files) > 3 else '')


def carry_update(plugin_dir):
    """`omarchy plugin update` for a checkout with its own work on it, which that command's
    fast-forward-only merge refuses: set uncommitted edits aside, rebase local commits onto
    upstream (a commit upstream already has drops out on its own), put the edits back, and
    validate the result. Any step that fails puts the checkout back exactly as it was --
    same HEAD, same edits -- so a refused update costs nothing. (rc, out, err) like run()."""
    d = str(plugin_dir)
    before = repo_head(plugin_dir)
    rc, _, err = run(['git', '-C', d, 'fetch', '--quiet', 'origin', 'HEAD'], timeout=60)
    if rc != 0:
        return 1, '', 'fetch failed: ' + err.strip()
    edits = local_edit_count(plugin_dir)
    ahead = local_commit_count(plugin_dir, 'FETCH_HEAD')
    stashed = False
    if edits:
        rc, _, err = run(['git', '-C', d, 'stash', 'push', '--quiet', '-m', 'dashboard: local edits before update'])
        if rc != 0:
            return 1, '', 'Could not set the local edits aside: ' + err.strip()
        stashed = True

    # Untracked files the update adds would stop the merge. One identical to the update's
    # copy is moved into the git dir for the merge (which writes the same bytes back) and
    # dropped once the update has passed; restore() moves it back. One that differs is a
    # clash like any other.
    _, gitdir, _ = run(['git', '-C', d, 'rev-parse', '--absolute-git-dir'])
    parking = Path(gitdir.strip()) / 'dashboard-parked'
    parked = []

    def unpark():
        for path in parked:
            dest = Path(d) / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(parking / path, dest)
        shutil.rmtree(parking, ignore_errors=True)

    def restore(why):
        run(['git', '-C', d, 'rebase', '--abort'])
        run(['git', '-C', d, 'reset', '--hard', '--quiet', before])
        unpark()
        if stashed:
            prc, _, perr = run(['git', '-C', d, 'stash', 'pop', '--quiet'])
            if prc != 0:
                return 1, '', (why + '\nThe local edits could not be put back; they are kept in '
                               f'`git -C {d} stash list`: ' + perr.strip())
        return 1, '', why + '\nNothing changed: the plugin is still on its old version with your local changes.'

    same, differ = untracked_in_update(plugin_dir)
    if differ:
        return restore('New files here clash with files the update adds: ' + ', '.join(differ[:5]))
    for path in same:
        (parking / path).parent.mkdir(parents=True, exist_ok=True)
        os.replace(Path(d) / path, parking / path)
        parked.append(path)

    if ahead:
        rc, out, err = run(['git', '-C', d, 'rebase', '--quiet', 'FETCH_HEAD'], timeout=60)
        if rc != 0:
            return restore('Your local commits clash with the update:\n' + (err or out).strip()[-1500:])
    else:
        rc, out, err = run(['git', '-C', d, 'merge', '--ff-only', '--quiet', 'FETCH_HEAD'])
        if rc != 0:
            return restore('The update could not be applied: ' + (err or out).strip())
    if stashed:
        # apply, not pop: the stash entry stays the exact copy restore() puts back on the old
        # HEAD until the update has passed validation, however far it got.
        rc, out, err = run(['git', '-C', d, 'stash', 'apply', '--quiet'])
        if rc != 0:
            return restore('Your local edits clash with the update:\n' + (err or out).strip()[-1500:])
    rc, out, err = run(['omarchy', 'plugin', 'validate', d], timeout=60)
    if rc != 0:
        return restore('The update with your local changes on top failed validation:\n'
                       + (err or out).strip()[-1500:])
    if stashed:
        run(['git', '-C', d, 'stash', 'drop', '--quiet'])
    shutil.rmtree(parking, ignore_errors=True)
    run(['omarchy-shell', 'shell', 'rescanPlugins'], timeout=20)
    kept = []
    still_ahead = local_commit_count(plugin_dir, 'FETCH_HEAD')
    if still_ahead:
        kept.append(f'{still_ahead} local commit(s)')
    still_edited = local_edit_count(plugin_dir)
    if still_edited:
        kept.append(f'{still_edited} locally edited file(s)')
    note = ('Kept ' + ' and '.join(kept) + ' on top.') if kept else \
        'Your local changes are now part of the update itself, so none are left over.'
    return 0, f'Updated {Path(d).name}. {note}', ''


# States in which upstream was never compared, so there is no ahead count to give.
NO_COMPARISON_STATES = ('no-repo', 'unreachable', 'in-progress')


def srcinfo_version(repo_dir):
    """'[epoch:]pkgver-pkgrel' as the repo's .SRCINFO declares it, or '' if it has none.
    Read from .SRCINFO rather than by sourcing PKGBUILD, which is a shell script."""
    try:
        text = (Path(repo_dir) / '.SRCINFO').read_text()
    except (OSError, UnicodeDecodeError):
        return ''
    return srcinfo_text_version(text)


def srcinfo_text_version(text):
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(' = ')
        if sep and key in ('pkgver', 'pkgrel', 'epoch') and key not in fields:
            fields[key] = value.strip()
    if not fields.get('pkgver') or not fields.get('pkgrel'):
        return ''
    epoch = fields.get('epoch')
    return (epoch + ':' if epoch and epoch != '0' else '') + fields['pkgver'] + '-' + fields['pkgrel']


def installed_older_than(installed, built):
    """pacman's own version ordering (vercmp), not string comparison: 0.3.10 > 0.3.9."""
    rc, out, _ = run(['vercmp', installed, built], timeout=5)
    try:
        return rc == 0 and int(out.strip()) < 0
    except ValueError:
        return False


def web_url(remote_url):
    """A browser link for a git remote, or '' if it is not one a browser can open. Never
    carries credentials: an https remote can hold a token (https://user:token@host/...),
    and this ends up in status.json and on screen."""
    url = (remote_url or '').strip()
    m = re.match(r'^[\w.-]+@([\w.-]+):(.+)$', url)                     # git@host:owner/repo
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r'^(?:https?|ssh|git)://(?:[^@/]+@)?([\w.-]+)(?::\d+)?/(.+)$', url)
        if not m:
            return ''
        host, path = m.group(1), m.group(2)
    path = re.sub(r'\.git/?$', '', path.strip('/'))
    if host == 'aur.archlinux.org':                                   # the package page, not the git
        return f'https://aur.archlinux.org/packages/{path}'
    return f'https://{host}/{path}'


def repo_info(repo_dir, remote, upstream_ref):
    """What the expanded row shows: where the checkout is, which branch it is on, a link to
    its source, and when upstream last committed. upstream_ref is None when upstream was
    never reached this check."""
    repo_dir = str(repo_dir)
    info = {'path': repo_dir, 'branch': '', 'webUrl': '', 'upstreamTs': 0}
    if not (Path(repo_dir) / '.git').exists():
        return info
    rc, out, _ = run(['git', '-C', repo_dir, 'rev-parse', '--abbrev-ref', 'HEAD'])
    if rc == 0:
        info['branch'] = out.strip()
    rc, out, _ = run(['git', '-C', repo_dir, 'remote', 'get-url', remote])
    if rc == 0:
        info['webUrl'] = web_url(out)
    if upstream_ref:
        rc, out, _ = run(['git', '-C', repo_dir, 'log', '-1', '--format=%ct', upstream_ref])
        if rc == 0 and out.strip().isdigit():
            info['upstreamTs'] = int(out.strip())
    return info


def upstream_version(repo_dir, ref, kind):
    """The version the incoming commits would install -- the manifest's for a plugin, the
    .SRCINFO's for an app -- read from `ref` without touching the checkout, or '' if it
    cannot be read."""
    path = 'manifest.json' if kind == 'plugin' else '.SRCINFO'
    rc, out, _ = run(['git', '-C', str(repo_dir), 'show', f'{ref}:{path}'])
    if rc != 0:
        return ''
    if kind == 'app':
        return srcinfo_text_version(out)
    try:
        version = json.loads(out).get('version', '')
        return version if isinstance(version, str) else ''
    except ValueError:
        return ''


def version_change(installed, incoming):
    """True/False when both versions are known, None when either is not. A commit that
    leaves the version alone (a README, a screenshot) is still an update, but not one worth
    a notification -- 2026-10-01: Touchpad Glance's preview.png commit announced itself as
    an update to a plugin already on the version it "updated" to."""
    if not installed or not incoming:
        return None
    return installed != incoming


def check_plugin(p):
    pid = p['id']
    plugin_dir = PLUGINS_DIR / pid
    state, behind, reason = plugin_update_state(plugin_dir)
    compared = state not in NO_COMPARISON_STATES
    ahead = local_commit_count(plugin_dir, 'FETCH_HEAD') if compared else 0
    version = manifest_version(pid)
    incoming = upstream_version(plugin_dir, 'FETCH_HEAD', 'plugin') if state == 'behind' else ''
    return {
        'id': pid, 'kind': 'plugin', 'name': p.get('name', pid),
        'description': manifest_description(pid),
        'kinds': [k for k in (p.get('kinds') or []) if isinstance(k, str)],
        'version': version, 'upstreamVersion': incoming,
        'versionChange': version_change(version, incoming), 'enabled': bool(p.get('enabled')),
        'canDisable': bool(p.get('canDisable', True)),
        'updateState': state, 'behind': behind, 'reason': reason, 'ahead': ahead,
        'edits': local_edit_count(plugin_dir) if compared else 0,
        'info': repo_info(plugin_dir, 'origin', 'FETCH_HEAD' if compared else None),
    }


def check_app(a):
    branch = a.get('branch', 'master')
    remote = a.get('remote', 'origin')
    state, behind, reason = app_update_state(a['repoDir'], branch, remote)
    version = pkg_version(a.get('pkgName', a['id']))
    # The git check only says the SOURCE is current. After a merge whose build or install
    # never ran (sudo prompt, failed makepkg), the installed package is still the old one
    # and this row used to say "up to date" regardless (2026-09-21, Flea 0.3.0-1 vs 0.3.1-2).
    if state == 'up-to-date' and version:
        built = srcinfo_version(a['repoDir'])
        if built and installed_older_than(version, built):
            state, reason = 'not-installed', f'repo has {built}, installed is {version}'
    compared = state not in NO_COMPARISON_STATES
    ahead = local_commit_count(a['repoDir'], f'{remote}/{branch}') if compared else 0
    incoming = upstream_version(a['repoDir'], f'{remote}/{branch}', 'app') if state == 'behind' else ''
    return {
        'id': a['id'], 'kind': 'app', 'name': a.get('name', a['id']),
        'description': a.get('description') or (pkg_description(a.get('pkgName', a['id'])) if version else ''),
        'version': version, 'upstreamVersion': incoming,
        'versionChange': version_change(version, incoming),
        'updateState': state, 'behind': behind, 'reason': reason, 'ahead': ahead,
        'info': repo_info(a['repoDir'], remote, f'{remote}/{branch}' if compared else None),
    }


# Package apps: something installed with pacman or from the AUR (Brave's brave-bin), tracked
# without a source checkout. There is no Dashboard-side build: the update is the system's.
AUR_RPC = 'https://aur.archlinux.org/rpc/v5/info'
# AUR packages built from a VCS head: the AUR's version is whenever the PKGBUILD was last
# touched, not when the source moved, so it cannot say when one is behind.
VCS_SUFFIXES = ('-git', '-svn', '-hg', '-bzr', '-darcs', '-fossil', '-cvs')


def is_package_app(app):
    return app.get('source') == 'package'


def sync_repo(pkg_name):
    """The sync repo that carries this package ('omarchy', 'extra', ...), or '' when none
    does: then it came from the AUR or a local build."""
    rc, out, _ = run(['pacman', '-Si', pkg_name])
    if rc != 0:
        return ''
    for line in out.splitlines():
        key, sep, value = line.partition(':')
        if sep and key.strip() == 'Repository':
            return value.strip()
    return ''


def sync_updates():
    """({package: new version}, '') for every repo package with an update, from
    checkupdates (which refreshes a private copy of the sync databases, never the system's,
    and needs no root), or (None, why) when it could not tell."""
    rc, out, err = run(['checkupdates', '--nocolor'], timeout=180)
    if rc == 2:  # nothing to update
        return {}, ''
    if rc != 0:
        return None, (err or out).strip()[-300:] or 'checkupdates failed'
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] == '->':
            found[parts[0]] = parts[3]
    return found, ''


def aur_versions(names):
    """({name: AUR version}, '') for the names the AUR has, or (None, why)."""
    if not names:
        return {}, ''
    query = '&'.join('arg[]=' + urllib.request.quote(n, safe='') for n in names)
    try:
        req = urllib.request.Request(f'{AUR_RPC}?{query}', headers={'User-Agent': 'omarchy-plugin-dashboard'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read(4 * 1024 * 1024))
        return {r['Name']: r['Version'] for r in data.get('results', [])
                if isinstance(r, dict) and r.get('Name') and r.get('Version')}, ''
    except (OSError, ValueError, AttributeError, TypeError) as e:
        return None, f'could not reach the AUR: {e}'


def package_context(apps):
    """What checking every package app needs, looked up once per check rather than per
    app: where each comes from, and the pending versions (checkupdates for repo packages,
    the AUR for the rest). Only asks a source that some tracked package actually uses."""
    pkgs = [a.get('pkgName') or a['id'] for a in apps if is_package_app(a)]
    repos = {p: sync_repo(p) for p in pkgs}
    ctx = {'repos': repos, 'sync': {}, 'syncError': '', 'aur': {}, 'aurError': ''}
    if any(repos.values()):
        ctx['sync'], ctx['syncError'] = sync_updates()
    foreign = [p for p, r in repos.items() if not r and not p.endswith(VCS_SUFFIXES)]
    if foreign:
        ctx['aur'], ctx['aurError'] = aur_versions(foreign)
    return ctx


def check_package_app(a, ctx):
    pkg = a.get('pkgName') or a['id']
    version = pkg_version(pkg)
    repo = (ctx.get('repos') or {}).get(pkg, '')
    info = {'path': '', 'branch': '', 'webUrl': '', 'upstreamTs': 0, 'repo': repo or 'AUR'}
    state, reason, incoming = 'up-to-date', '', ''
    if not version:
        state, reason = 'no-repo', f'{pkg} is not installed'
    elif repo:
        updates = ctx.get('sync')
        if updates is None:
            state, reason = 'unreachable', ctx.get('syncError') or 'could not check for updates'
        elif pkg in updates:
            state, incoming = 'behind', updates[pkg]
    else:
        info['webUrl'] = f'https://aur.archlinux.org/packages/{pkg}'
        aur = ctx.get('aur')
        if pkg.endswith(VCS_SUFFIXES):
            state, reason = 'no-repo', 'built from the latest source; the AUR cannot say when that moves'
        elif aur is None:
            state, reason = 'unreachable', ctx.get('aurError') or 'could not reach the AUR'
        elif pkg not in aur:
            state, reason = 'no-repo', 'not in the repos or the AUR'
        elif installed_older_than(version, aur[pkg]):
            state, incoming = 'behind', aur[pkg]
    return {
        'id': a['id'], 'kind': 'app', 'source': 'package', 'name': a.get('name', a['id']),
        'description': a.get('description') or (pkg_description(pkg) if version else ''),
        'version': version, 'upstreamVersion': incoming,
        'versionChange': version_change(version, incoming),
        'updateState': state, 'behind': 1 if state == 'behind' else 0, 'reason': reason, 'ahead': 0,
        'updateVia': 'system' if repo else 'aur', 'info': info,
    }


def terminal_ids(items, ids):
    """The ids among these that update in a terminal (package apps) rather than as a job."""
    package = {i['id'] for i in items if i.get('source') == 'package'}
    return [i for i in ids if i in package]


def open_terminal_update(ids):
    """A terminal running terminal-update for these package apps: their updates need sudo,
    which only a terminal can ask for."""
    if not ids:
        return True
    # Detached with no pipes: run() would wait on the terminal holding its output open.
    try:
        subprocess.Popen(['uwsm-app', '--', 'xdg-terminal-exec', '--title=Plugin Dashboard update',
                          sys.executable, str(Path(__file__).resolve()), 'terminal-update', *ids],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


def cmd_terminal_update(args):
    """Runs in a terminal: updates package apps the way the system does. A repo package is
    updated by `omarchy update` -- the whole system, since Arch does not support upgrading
    one repo package on its own -- and an AUR one by `yay -S`. Then records each in Recent
    and rechecks."""
    ensure_apps_file()
    apps = {a['id']: a for a in load_apps() if is_package_app(a)}
    wanted = [apps[i] for i in args.ids if i in apps]
    if not wanted:
        print('Nothing to update here.')
    before = {a['id']: pkg_version(a.get('pkgName') or a['id']) for a in wanted}
    system = [a for a in wanted if sync_repo(a.get('pkgName') or a['id'])]
    aur = [a for a in wanted if a not in system]
    failed = set()
    if system:
        print('Updating the system (' + ', '.join(a['name'] for a in system) + ' come from the package repos)…\n')
        if subprocess.run(['omarchy', 'update']).returncode != 0:
            failed.update(a['id'] for a in system)
    if aur:
        print('\nUpdating from the AUR: ' + ', '.join(a['name'] for a in aur) + '\n')
        if subprocess.run(['yay', '-S', '--needed', *[a.get('pkgName') or a['id'] for a in aur]]).returncode != 0:
            failed.update(a['id'] for a in aur)
    print()
    for a in wanted:
        after = pkg_version(a.get('pkgName') or a['id'])
        ok = a['id'] not in failed
        print(f"{a['name']}: {before[a['id']] or '?'} → {after or '?'}" + ('' if ok else ' (failed)'))
        append_history({'ts': time.time(), 'id': a['id'], 'name': a['name'], 'kind': 'app', 'action': 'update',
                        'ok': ok, 'before': '', 'after': '', 'fromVersion': before[a['id']], 'toVersion': after,
                        'unitsInstalled': [], 'source': 'package',
                        'message': '' if ok else 'the update command failed; see its terminal'})
    check_all()
    if sys.stdin.isatty():
        try:
            input('\nPress Enter to close.')
        except EOFError:
            pass


def alerts(item):
    """Whether an item belongs in the bar badge and the notification: it can be updated,
    is not held, and is not known to leave its version unchanged. An update whose
    incoming version could not be read still alerts."""
    return (item.get('updateState') in UPDATABLE_STATES and not item.get('held')
            and item.get('versionChange') is not False)


def _previously_behind_ids():
    try:
        data = json.loads(STATUS_PATH.read_text())
        return {i['id'] for i in data.get('items', []) if i.get('updateState') in UPDATABLE_STATES}
    except (OSError, ValueError):
        return set()


def notify_new_updates(items, old_behind_ids):
    """A toast only for items that just *became* one-click-updatable since
    the last check -- not a repeat every cycle for something that's been
    sitting there, and not for 'dirty'/'diverged', which need a person
    regardless of how many checks go by."""
    newly = [i for i in items if alerts(i) and i['id'] not in old_behind_ids]
    if not newly:
        return
    names = ', '.join(i['name'] for i in newly[:5])
    if len(newly) > 5:
        names += f' and {len(newly) - 5} more'
    title = 'Update available' if len(newly) == 1 else f'{len(newly)} updates available'
    # The notification's buttons need a process waiting for the click. Run it as its own
    # transient user unit: started from dashboard-check.service, a plain child would be
    # killed with the service's cgroup the moment the check finished.
    rc, _, _ = run(['systemd-run', '--user', '--collect', '--quiet',
                    f'--unit=dashboard-notify-{int(time.time())}',
                    sys.executable, str(Path(__file__).resolve()), 'await-notification',
                    title, names, *[i['id'] for i in newly]], timeout=10)
    if rc != 0:
        run(['notify-send', '--app-name=Plugin Dashboard', '--icon=system-software-update',
             title, names], timeout=5)


def cmd_await_notification(args):
    """Shows the update notification with Update and Open buttons and waits for a click.
    Update updates the items the notification named that are still updatable and not held
    by then (the same thing Update all does for them, one at a time), then says how it went.
    Open opens the panel."""
    try:
        p = subprocess.run(['notify-send', '--app-name=Plugin Dashboard', '--icon=system-software-update',
                            '--action=update=Update', '--action=open=Open',
                            args.title, args.body], capture_output=True, text=True,
                           timeout=NOTIFY_WAIT_SECONDS, check=False)
        choice = p.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return
    if choice == 'open':
        run(['omarchy-shell', SELF_ID, 'open'], timeout=10)
        return
    if choice != 'update':
        return
    status = load_status() or {}
    scope = update_all_scope()
    waiting = [i for i in status.get('items', [])
               if i['id'] in args.ids and i.get('updateState') in UPDATABLE_STATES and not i.get('held')]
    wanted = {i['id'] for i in waiting if in_update_all(i, scope)}
    skipped = [i.get('name', i['id']) for i in waiting if i['id'] not in wanted]
    names = {i['id']: i.get('name', i['id']) for i in status.get('items', [])}
    done, failed = [], []
    ids = [i for i in args.ids if i in wanted]
    skipped_note = ('Skipped, update not verified: ' + ', '.join(skipped)) if skipped else ''
    in_terminal = terminal_ids(status.get('items', []), ids)
    if in_terminal:
        open_terminal_update(in_terminal)
        ids = [i for i in ids if i not in in_terminal]
        if not ids:
            if skipped_note:
                run(['notify-send', '--app-name=Plugin Dashboard', '--icon=dialog-information',
                     'Not all updated', skipped_note], timeout=5)
            return
    # As a job, so an open panel shows it going like one it started itself. This waiter
    # is already its own unit, so it runs the job in-process.
    job = new_job('update', ids) if ids else None
    if ids and job is None:
        run(['notify-send', '--app-name=Plugin Dashboard', '--icon=dialog-information', 'Not updated',
             'Another update is still running; try again once it is done.'], timeout=5)
        return
    for r in (execute_job(job)['results'] if job else []):
        (done if r['ok'] else failed).append(names.get(r['id'], r['id']))
    if not done and not failed:
        body = skipped_note or 'Nothing left to update.'
    else:
        body = ', '.join(filter(None, ['Updated ' + ', '.join(done) if done else '',
                                       'failed: ' + ', '.join(failed) if failed else '']))
        body += ('. ' + skipped_note) if skipped_note else ''
    run(['notify-send', '--app-name=Plugin Dashboard',
         '--icon=' + ('dialog-error' if failed else 'system-software-update'),
         'Update failed' if failed else 'Updated' if done else 'Not updated', body + (' — open the Dashboard for details.' if failed else '')],
        timeout=5)


def check_all():
    old_behind_ids = _previously_behind_ids()
    apps = load_apps()
    pkg_ctx = package_context(apps)
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        catalog_job = pool.submit(refresh_catalog)
        jobs = [pool.submit(check_plugin, p) for p in discover_plugins()] + \
               [pool.submit(check_package_app, a, pkg_ctx) if is_package_app(a) else pool.submit(check_app, a)
                for a in apps]
        items = [job.result() for job in jobs]
        catalog, catalog_error = catalog_job.result()
    index = catalog_index(catalog)
    # 'market' is None for a plugin the marketplace does not list, and left out altogether
    # when the catalog could not be read -- then nobody knows.
    if catalog:
        for item in items:
            if item.get('kind') == 'plugin':
                item['market'] = market_status(item, index)
    holds = load_holds()
    history = load_history()
    shortcuts = read_shortcuts()
    now = time.time()
    display_names = {i['id']: i.get('name') for i in items}
    for item in items:
        item['held'] = item['id'] in holds
        if item.get('kind') == 'plugin':
            item['shortcut'] = shortcuts.get(item['id'], '')
        item['holdReason'] = holds.get(item['id'], {}).get('reason', '')
        candidate = rollback_candidate(item['id'], history, now) if item.get('kind') == 'plugin' else None
        item['rollback'] = ({'to': candidate['before'][:7], 'toVersion': candidate.get('fromVersion', ''),
                             'fromVersion': candidate.get('toVersion', '')} if candidate else None)
    status = {
        'ts': now,
        'items': items,
        'updatable': sum(1 for i in items if alerts(i)),
        'leftovers': leftovers(),
        # False when the catalog could not be read at all: then "not listed" means "unknown".
        'catalogOk': bool(catalog), 'catalogError': catalog_error,
        # Plugin entries were recorded under their id (cmd_update knows no display name), so
        # Recent read "com.omastorm.radar updated"; show the name the rows use instead.
        'history': [dict({k: e.get(k) for k in ('id', 'name', 'kind', 'source', 'action', 'ok', 'fromVersion',
                                                 'toVersion', 'ts', 'message')},
                         name=display_names.get(e.get('id')) or e.get('name') or e.get('id'))
                    for e in reversed(history[-HISTORY_SHOWN:])],
    }
    notify_new_updates(items, old_behind_ids)
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(status, indent=2))
    tmp.replace(STATUS_PATH)
    return status


def cmd_check(args):
    ensure_apps_file()
    reap_stale_job()
    if not getattr(args, 'force', False):
        cached = load_status()
        if status_is_fresh(cached):
            print(json.dumps(cached))
            return
    print(json.dumps(check_all()))


def cmd_enable(args):
    rc, out, err = run(['omarchy', 'plugin', 'enable', args.id])
    print(json.dumps({'ok': rc == 0, 'message': (out or err).strip()}))
    check_all()


def cmd_disable(args):
    if args.id == SELF_ID:
        print(json.dumps({'ok': False, 'message': 'Refusing to disable the Dashboard from within itself -- '
                                                    'use `omarchy plugin disable` from a terminal instead.'}))
        return
    rc, out, err = run(['omarchy', 'plugin', 'disable', args.id])
    print(json.dumps({'ok': rc == 0, 'message': (out or err).strip()}))
    check_all()


def item_version(item_id, app=None):
    return pkg_version(app.get('pkgName', item_id)) if app else manifest_version(item_id)


def cmd_update(args):
    ensure_apps_file()
    apps = {a['id']: a for a in load_apps()}
    app = apps.get(args.id)
    if app and is_package_app(app):
        print(json.dumps({'ok': False, 'message': f"{app.get('name', args.id)} is a package: it updates in a terminal, "
                                                   'where sudo can ask (its Update button opens one).'}))
        return
    repo_dir = app['repoDir'] if app else PLUGINS_DIR / args.id
    before = repo_head(repo_dir)
    from_version = item_version(args.id, app)
    units_installed_before = set()
    if app:
        a = app
        rc, out, err = (pull_upstream(a['repoDir'], a.get('branch', 'master'), a.get('remote', 'origin'))
                        if a.get('pullFirst') else (0, '', ''))
        if rc == 0:
            pulled = out
            rc, out, err = run(a['updateCmd'], cwd=a['repoDir'], timeout=900, stream_log=STATE / 'action.log')
            out = (pulled + '\n' if pulled.strip() else '') + out
        if rc == 0:
            # The command saying it worked is not the same as the app being current: one
            # that builds without pulling (make install on a checkout nobody merged) rebuilt
            # the old version and exited 0.
            after = check_app(a)
            if after['updateState'] in ('behind', 'not-installed', 'dirty', 'in-progress'):
                rc = 1
                err = (err + '\n' if err.strip() else '') + 'The update command finished, but ' + {
                    'behind': "the checkout still lacks upstream's commits -- the command has to bring them in "
                              '(or tick "Pull upstream first" for this app).',
                    'not-installed': 'the installed package is still older than the repo builds: '
                                     + after.get('reason', ''),
                    'dirty': 'the checkout has local changes that keep upstream out: ' + after.get('reason', ''),
                    'in-progress': 'it left a git operation unfinished: ' + after.get('reason', ''),
                }[after['updateState']]
    else:
        plugin_dir = PLUGINS_DIR / args.id
        units_before = shipped_units(plugin_dir)
        units_installed_before = {name for name in units_before if (USER_UNIT_DIR / name).exists()}
        log_mark = shell_log_mark()
        if has_local_work(plugin_dir):
            rc, out, err = carry_update(plugin_dir)
        else:
            rc, out, err = run(['omarchy', 'plugin', 'update', args.id, '--yes'], timeout=180,
                               stream_log=STATE / 'action.log')
        if rc == 0 and has_ensure_bootstrap(plugin_dir):
            erc, eout, eerr = run_ensure_bootstrap(plugin_dir)
            if erc != 0:
                rc = erc
                err = (err + '\n' if err.strip() else '') + \
                    'Update landed but the plugin did not come up cleanly:\n' + (eerr or eout)
            elif eout.strip():
                out = (out + '\n' if out.strip() else '') + eout
        if rc == 0:
            unit_messages, units_ok = install_new_units(plugin_dir, units_before)
            if unit_messages:
                text = '\n'.join(unit_messages)
                if units_ok:
                    out = (out + '\n' if out.strip() else '') + text
                else:
                    rc = 1
                    err = (err + '\n' if err.strip() else '') + \
                        'Update landed but a new background service did not come up:\n' + text
        if rc == 0:
            restart_messages, restarted_ok = restart_updated_units(plugin_dir, before, repo_head(plugin_dir),
                                                                   units_before)
            if restart_messages:
                text = '\n'.join(restart_messages)
                if restarted_ok:
                    out = (out + '\n' if out.strip() else '') + text
                else:
                    rc = 1
                    err = (err + '\n' if err.strip() else '') + \
                        'Update landed but a background service did not restart:\n' + text
        # Last, after every reload the steps above may have triggered.
        if rc == 0 and before != repo_head(plugin_dir):
            load_ok, load_text = check_plugin_loaded(args.id, plugin_dir, log_mark)
            if not load_ok:
                rc = 1
                err = (err + '\n' if err.strip() else '') + 'Update landed, but ' + load_text[0].lower() + load_text[1:]
            elif load_text:
                out = (out + '\n' if out.strip() else '') + load_text
    # Long build logs (Flea's cargo test output, say) aren't useful in full to
    # the UI -- the tail carries the actual result or error.
    # A failure carries both streams: stdout has what the command said it was doing, stderr
    # what went wrong, and `out or err` dropped the second whenever the first had any text.
    if rc == 0:
        message = (out or err).strip()[-4000:]
    else:
        message = '\n'.join(t for t in (out.strip(), err.strip()) if t)[-4000:]
    # What a rollback needs: where HEAD was, and which units this update started (so a
    # rollback to a version that does not ship them can stop them again).
    units_added = [] if app else sorted(
        name for name in shipped_units(repo_dir)
        if name not in units_installed_before and (USER_UNIT_DIR / name).exists())
    after = repo_head(repo_dir)
    # A plugin "update" that found nothing to pull changed nothing, and listing it under
    # Recent ("rebuilt, no version change") only buries the real ones. An app's no-op still
    # rebuilt and reinstalled, so it stays; so does any failure.
    if not app and rc == 0 and before and before == after:
        print(json.dumps({'ok': True, 'message': message}))
        check_all()
        return
    append_history({
        'ts': time.time(), 'id': args.id, 'name': app.get('name', args.id) if app else args.id,
        'kind': 'app' if app else 'plugin', 'action': 'update', 'ok': rc == 0,
        'before': before, 'after': after,
        'fromVersion': from_version, 'toVersion': item_version(args.id, app),
        'unitsInstalled': units_added,
        'message': '' if rc == 0 else (message.strip().splitlines() or [''])[-1][:200],
    })
    print(json.dumps({'ok': rc == 0, 'message': message}))
    check_all()


def rollback_candidate(plugin_id, history, now=None):
    """The update this plugin can be rolled back from, or None: its most recent change
    (an update that moved HEAD, or a rollback) must be an update, inside
    ROLLBACK_WINDOW_SECONDS, with HEAD still where that update left it -- anything else
    means the checkout moved on since and resetting would throw that away. An update that
    moved HEAD but reported failure counts: it landed and then broke (a service that would
    not start, a widget the shell could not load), which is when a rollback is wanted most;
    one omarchy refused outright left HEAD where it was."""
    changes = [e for e in history if e.get('id') == plugin_id and e.get('kind') == 'plugin'
               and (e.get('action') == 'rollback' or (e.get('before') and e.get('before') != e.get('after')))]
    if not changes:
        return None
    last = changes[-1]
    if last.get('action') != 'update':
        return None
    if (now if now is not None else time.time()) - last.get('ts', 0) > ROLLBACK_WINDOW_SECONDS:
        return None
    if repo_head(PLUGINS_DIR / plugin_id) != last.get('after'):
        return None
    return last


def cmd_rollback(args):
    """Puts a plugin back where its last Dashboard update found it: the same
    `reset --hard` omarchy-plugin-update itself does when an update fails validation,
    then the same steps an update takes (validate, the plugin's own --ensure, a plugin
    rescan), run in reverse for any service the update had started. Then holds the plugin,
    or the next check would offer the same update straight back."""
    def done(ok, message):
        print(json.dumps({'ok': ok, 'message': message}))
        if ok:
            check_all()

    ensure_apps_file()
    if args.id in {a['id'] for a in load_apps()}:
        return done(False, 'Roll back is for plugins. An app is rebuilt from source; '
                           'check out the commit you want in its repo and run its update command.')
    plugin_dir = PLUGINS_DIR / args.id
    entry = rollback_candidate(args.id, load_history())
    if not entry:
        return done(False, 'Nothing to roll back: no Dashboard update of this plugin in the last '
                           f'{ROLLBACK_WINDOW_SECONDS // 86400} days, or it has changed since.')
    op = repo_operation_in_progress(plugin_dir)
    if op:
        return done(False, f'A git {op} is in progress in {plugin_dir}; resolve it first.')
    # Local edits an update carried over ride along back too: aside, reset, back on top.
    stashed = False
    if local_edit_count(plugin_dir):
        rc, _, err = run(['git', '-C', str(plugin_dir), 'stash', 'push', '--quiet', '-m',
                          'dashboard: local edits before rollback'])
        if rc != 0:
            return done(False, 'Could not set the local edits aside: ' + err.strip())
        stashed = True
    log_mark = shell_log_mark()
    rc, out, err = run(['git', '-C', str(plugin_dir), 'reset', '--hard', entry['before']])
    if rc != 0:
        if stashed:
            run(['git', '-C', str(plugin_dir), 'stash', 'pop', '--quiet'])
        return done(False, 'git reset failed: ' + (err or out).strip())
    if stashed:
        rc, out, err = run(['git', '-C', str(plugin_dir), 'stash', 'pop', '--quiet'])
        if rc != 0:
            run(['git', '-C', str(plugin_dir), 'reset', '--hard', '--quiet', entry['after']])
            run(['git', '-C', str(plugin_dir), 'stash', 'pop', '--quiet'])
            return done(False, 'Your local edits do not fit the earlier version, so the plugin was left on '
                               'the update:\n' + (err or out).strip()[-1500:])
    rc, out, err = run(['omarchy', 'plugin', 'validate', str(plugin_dir)], timeout=60)
    if rc != 0:
        if stashed:
            run(['git', '-C', str(plugin_dir), 'stash', 'push', '--quiet'])
        run(['git', '-C', str(plugin_dir), 'reset', '--hard', entry['after']])
        if stashed:
            run(['git', '-C', str(plugin_dir), 'stash', 'pop', '--quiet'])
        return done(False, 'The earlier version no longer passes validation, so the plugin was left on the '
                           'update:\n' + (err or out).strip()[-1500:])
    ok = True
    messages = [f"Rolled back to {entry['before'][:7]}"
                + (f" (v{entry['fromVersion']})" if entry.get('fromVersion') else '') + '.']
    shipped_now = shipped_units(plugin_dir)
    removed = False
    for name in entry.get('unitsInstalled') or []:
        if name in shipped_now:
            continue
        run(['systemctl', '--user', 'disable', '--now', name], timeout=60)
        try:
            (USER_UNIT_DIR / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            messages.append(f'Could not remove {name}: {e}')
            ok = False
            continue
        removed = True
        messages.append(f'Stopped and removed {name}, which the update had added.')
    if removed:
        run(['systemctl', '--user', 'daemon-reload'], timeout=30)
    restart_messages, restarted_ok = restart_updated_units(plugin_dir, entry['after'], entry['before'],
                                                           shipped_now)
    messages.extend(restart_messages)
    ok = ok and restarted_ok
    if has_ensure_bootstrap(plugin_dir):
        erc, eout, eerr = run_ensure_bootstrap(plugin_dir)
        if erc != 0:
            ok = False
            messages.append('The plugin did not come up cleanly:\n' + (eerr or eout).strip()[-1500:])
    run(['omarchy-shell', 'shell', 'rescanPlugins'], timeout=20)
    load_ok, load_text = check_plugin_loaded(args.id, plugin_dir, log_mark)
    if load_text:
        messages.append(load_text)
    ok = ok and load_ok
    holds = load_holds()
    holds[args.id] = {'ts': time.time(), 'reason': 'rolled back from v' + entry['toVersion']
                      if entry.get('toVersion') else 'rolled back'}
    save_holds(holds)
    messages.append('Held, so the update is not offered again until you unhold it.')
    append_history({
        'ts': time.time(), 'id': args.id, 'name': entry.get('name', args.id), 'kind': 'plugin',
        'action': 'rollback', 'ok': ok, 'before': entry['after'], 'after': repo_head(plugin_dir),
        'fromVersion': entry.get('toVersion', ''), 'toVersion': manifest_version(args.id),
        'unitsInstalled': [], 'message': '' if ok else messages[-1][:200],
    })
    print(json.dumps({'ok': ok, 'message': '\n'.join(messages)}))
    check_all()


def known_item(item_id):
    return item_id in {a['id'] for a in load_apps()} or (PLUGINS_DIR / item_id).is_dir()


def cmd_hold(args):
    ensure_apps_file()
    if not known_item(args.id):
        print(json.dumps({'ok': False, 'message': f'No plugin or tracked app called {args.id}.'}))
        return
    holds = load_holds()
    holds[args.id] = {'ts': time.time(), 'reason': getattr(args, 'reason', None) or 'held'}
    save_holds(holds)
    print(json.dumps({'ok': True, 'message': f'Holding {args.id}: its updates stay out of the badge, '
                                             f'Update all and notifications until you unhold it.'}))
    check_all()


def cmd_unhold(args):
    holds = load_holds()
    if holds.pop(args.id, None) is None:
        print(json.dumps({'ok': False, 'message': f'{args.id} is not held.'}))
        return
    save_holds(holds)
    print(json.dumps({'ok': True, 'message': f'{args.id} is no longer held.'}))
    check_all()


# Where a plugin keeps data outside its own folder. Not ~/.config: what is there is
# the user's own configuration more often than the plugin's leftovers.
DATA_ROOTS = (Path.home() / '.local/state', Path.home() / '.cache', Path.home() / '.local/share')
# The shell's own per-plugin state folder (this Dashboard keeps its state there too).
PLUGIN_STATE_ROOT = Path.home() / '.local/state/omarchy/plugins'
# Never offered for deletion, whoever seems to own them.
SHARED_DATA_NAMES = {'omarchy', 'quickshell', 'systemd', 'applications', 'icons', 'fonts', 'Trash',
                     'fontconfig', 'mesa_shader_cache', 'qtshadercache', 'QtProject', 'plugins'}
# Id segments that name nobody's data ("com" in com.omastorm.radar).
_GENERIC_SEGMENTS = {'com', 'org', 'net', 'io', 'dev', 'app', 'tech', 'ali'}
_DATA_REF = re.compile(r'(?:\.local/state|\.cache|\.local/share)/([A-Za-z0-9][A-Za-z0-9._-]*)')
_CODE_SUFFIXES = {'.qml', '.js', '.py', '.sh', '.rs', '.json', '.toml', '.service'}


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', str(text or '').lower()).strip('-')


def plugin_unit_pattern(plugin_id):
    return re.compile(r'plugins/' + re.escape(plugin_id) + r'(?=[/\s"\']|$)', re.M)


def installed_plugin_units(plugin_id):
    """The user units that run this plugin's code: installed by its own install.py or by
    this Dashboard, and left running by `omarchy plugin remove`, which knows nothing of
    them. Matched on the plugin's path in the unit, not the unit's name: cpu-pulse.service
    belongs to Pulse now, not to the CPU Pulse plugin it was once named for."""
    pattern = plugin_unit_pattern(plugin_id)
    found = []
    try:
        candidates = sorted(USER_UNIT_DIR.iterdir())
    except OSError:
        return []
    for unit in candidates:
        if unit.suffix not in UNIT_SUFFIXES or not unit.is_file():
            continue
        try:
            if pattern.search(unit.read_text()):
                found.append(unit.name)
        except (OSError, UnicodeDecodeError):
            continue
    return found


def plugin_data_names(plugin_id, plugin_dir=None):
    """Names this plugin might keep data under in DATA_ROOTS: its id, the meaningful parts
    of it, its display name, the units it ships, and whatever its code names under
    .local/state, .cache or .local/share."""
    plugin_dir = Path(plugin_dir or PLUGINS_DIR / plugin_id)
    names = {plugin_id}
    names.update(seg for seg in plugin_id.split('.') if len(seg) > 2 and seg not in _GENERIC_SEGMENTS)
    try:
        manifest = json.loads((plugin_dir / 'manifest.json').read_text())
        if _slug(manifest.get('name')):
            names.add(_slug(manifest.get('name')))
    except (OSError, ValueError, AttributeError):
        pass
    names.update(Path(name).stem for name in shipped_units(plugin_dir))
    seen = 0
    for path in sorted(plugin_dir.rglob('*')) if plugin_dir.is_dir() else []:
        if seen >= 400:
            break
        if path.suffix not in _CODE_SUFFIXES or any(part in UNIT_SKIP_DIRS for part in path.parts) \
                or not path.is_file():
            continue
        seen += 1
        try:
            if path.stat().st_size > 512 * 1024:
                continue
            names.update(_DATA_REF.findall(path.read_text(errors='ignore')))
        except OSError:
            continue
    return {n for n in names if n and n not in SHARED_DATA_NAMES}


def dir_size(path, limit=200000):
    total, count = 0, 0
    stack = [Path(path)]
    while stack and count < limit:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for e in entries:
            count += 1
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                else:
                    total += e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def plugin_data_dirs(plugin_id):
    """The data folders that are this plugin's alone: they exist, and no other installed
    plugin points at the same name (every plugin reads ~/.local/state/omarchy/current/theme;
    Pulse writes the old CPU Pulse's ~/.local/state/cpu-pulse)."""
    mine = plugin_data_names(plugin_id)
    others = set()
    try:
        siblings = [d for d in PLUGINS_DIR.iterdir() if not d.name.startswith('.') and d.name != plugin_id]
    except OSError:
        siblings = []
    for other in siblings:
        others |= plugin_data_names(other.name, other)
    found = []
    for root in DATA_ROOTS:
        for name in sorted(mine - others):
            path = root / name
            if path.is_dir() and not path.is_symlink():
                found.append({'path': str(path), 'size': dir_size(path)})
    state_dir = PLUGIN_STATE_ROOT / plugin_id
    if state_dir.is_dir() and plugin_id != SELF_ID:
        found.append({'path': str(state_dir), 'size': dir_size(state_dir)})
    return found


def plugin_backups(plugin_id=None):
    """The folders `omarchy plugin remove` keeps for a plugin that was not a git checkout
    (.<id>.bak.<UTC stamp>[-n]) -- never cleaned up by anything."""
    try:
        entries = sorted(PLUGINS_DIR.iterdir())
    except OSError:
        return []
    out = []
    for d in entries:
        m = re.match(r'^\.(.+)\.bak\.(\d{14})(?:-\d+)?$', d.name)
        if m and d.is_dir() and (plugin_id is None or m.group(1) == plugin_id):
            out.append({'path': str(d), 'id': m.group(1), 'stamp': m.group(2), 'size': dir_size(d)})
    return out


def removal_plan(plugin_id):
    plugin_dir = PLUGINS_DIR / plugin_id
    kind = 'link' if plugin_dir.is_symlink() else 'git' if (plugin_dir / '.git').is_dir() else 'plain'
    name = plugin_id
    try:
        name = json.loads((plugin_dir / 'manifest.json').read_text()).get('name') or plugin_id
    except (OSError, ValueError, AttributeError):
        pass
    return {'id': plugin_id, 'name': name, 'folder': {'path': str(plugin_dir), 'kind': kind},
            'units': installed_plugin_units(plugin_id), 'data': plugin_data_dirs(plugin_id),
            'backups': plugin_backups(plugin_id)}


def cmd_remove_plan(args):
    if args.id == SELF_ID or not (PLUGINS_DIR / args.id).exists():
        print(json.dumps({'ok': False, 'message': f'No removable plugin called {args.id}.'}))
        return
    print(json.dumps(dict(removal_plan(args.id), ok=True)))


def trash(path):
    """Into the desktop trash, so a wrong guess about whose data it was can be undone."""
    rc, out, err = run(['gio', 'trash', str(path)], timeout=60)
    return rc == 0, (err or out).strip()


def stop_and_remove_units(names):
    messages, ok = [], True
    for name in names:
        run(['systemctl', '--user', 'disable', '--now', name], timeout=60)
        try:
            (USER_UNIT_DIR / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            ok = False
            messages.append(f'Could not remove {name}: {e}')
            continue
        messages.append(f'Stopped and removed {name}.')
    if names:
        run(['systemctl', '--user', 'daemon-reload'], timeout=30)
    return messages, ok


def cmd_remove(args):
    """`omarchy plugin remove`, plus what it leaves behind: the plugin's services are
    stopped first (they would crash-loop against a deleted folder) and their unit files
    removed once the plugin is gone; with --purge its data folders go to the trash, and
    without, they are remembered so Leftovers can offer them later. A hold on it is
    dropped. If omarchy refuses, the services are started again."""
    if args.id == SELF_ID:
        print(json.dumps({'ok': False, 'message': 'Refusing to remove the Dashboard from within itself -- '
                                                    'use `omarchy plugin remove` from a terminal instead.'}))
        return
    plan = removal_plan(args.id)
    units = plan['units']
    for name in units:
        run(['systemctl', '--user', 'stop', name], timeout=60)
    rc, out, err = run(['omarchy', 'plugin', 'remove', args.id, '--yes'], timeout=60)
    if rc != 0:
        for name in units:
            run(['systemctl', '--user', 'start', name], timeout=60)
        print(json.dumps({'ok': False, 'message': (err or out).strip() or 'omarchy plugin remove failed.'}))
        check_all()
        return
    messages = [(out or err).strip()]
    unit_messages, ok = stop_and_remove_units(units)
    messages += unit_messages
    kept = []
    for d in plan['data']:
        if getattr(args, 'purge', False):
            done, why = trash(d['path'])
            if done:
                messages.append(f"Moved {d['path']} to the trash.")
            else:
                ok = False
                kept.append(d['path'])
                messages.append(f"Could not move {d['path']} to the trash: {why}")
        else:
            kept.append(d['path'])
    if kept and not getattr(args, 'purge', False):
        messages.append('Kept its data (' + ', '.join(kept) + '); Leftovers lists it.')
    holds = load_holds()
    if holds.pop(args.id, None) is not None:
        save_holds(holds)
    if ok and args.id in read_shortcuts():
        result = set_shortcut(args.id, '', '')
        if not result['ok']:
            messages.append('Its keyboard shortcut is still in bindings.lua: ' + result['message'])
    append_history({
        'ts': time.time(), 'id': args.id, 'name': plan['name'], 'kind': 'plugin', 'action': 'remove',
        'ok': ok, 'before': '', 'after': '', 'fromVersion': '', 'toVersion': '', 'unitsInstalled': [],
        'dataKept': kept, 'message': '' if ok else messages[-1][:200],
    })
    print(json.dumps({'ok': ok, 'message': '\n'.join(m for m in messages if m)}))
    check_all()


def leftovers():
    """What removed plugins left behind: omarchy's backup folders, units that run a plugin
    that is no longer there, and data a removal kept. Each has a key cleanup acts on."""
    items = []
    for b in plugin_backups():
        stamp = b['stamp']
        name = b['id']
        try:
            name = json.loads((Path(b['path']) / 'manifest.json').read_text()).get('name') or name
        except (OSError, ValueError, AttributeError):
            pass
        items.append({'key': 'backup:' + Path(b['path']).name, 'kind': 'backup', 'path': b['path'],
                      'size': b['size'], 'label': f"{name} backup · {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"})
    try:
        units = sorted(USER_UNIT_DIR.iterdir())
    except OSError:
        units = []
    for unit in units:
        if unit.suffix not in UNIT_SUFFIXES or not unit.is_file():
            continue
        try:
            text = unit.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        m = re.search(r'\.config/omarchy/plugins/([A-Za-z0-9][A-Za-z0-9._-]*)', text)
        if m and not (PLUGINS_DIR / m.group(1)).exists():
            items.append({'key': 'unit:' + unit.name, 'kind': 'unit', 'path': str(unit), 'size': 0,
                          'label': f'{unit.name} runs {m.group(1)}, which is not installed'})
    seen = set()
    for e in reversed(load_history()):
        if e.get('action') != 'remove' or (PLUGINS_DIR / e.get('id', '')).exists():
            continue
        for path in e.get('dataKept') or []:
            if path in seen or not Path(path).is_dir():
                continue
            seen.add(path)
            items.append({'key': 'data:' + path, 'kind': 'data', 'path': path, 'size': dir_size(path),
                          'label': f"{e.get('name') or e.get('id')} data: {path.replace(str(Path.home()), '~', 1)}"})
    return items


def cmd_cleanup(args):
    """Clears one Leftovers entry, by its key -- only a key the current list has, so this
    never deletes a path it was merely handed."""
    item = next((i for i in leftovers() if i['key'] == args.id), None)
    if not item:
        print(json.dumps({'ok': False, 'message': 'That is no longer in Leftovers.'}))
        return
    if item['kind'] == 'unit':
        messages, ok = stop_and_remove_units([Path(item['path']).name])
        message = '\n'.join(messages)
    else:
        ok, why = trash(item['path'])
        message = f"Moved {item['path']} to the trash." if ok else f"Could not move {item['path']} to the trash: {why}"
    print(json.dumps({'ok': ok, 'message': message}))
    check_all()


# Discover: the official plugin catalog (what plugins.omarchy.org lists), cached here
# trimmed down -- the feed itself is ~15 MB, mostly verification bookkeeping the panel has
# no use for.
CATALOG_URL = 'https://plugins.omarchy.org/catalog.json'
CATALOG_MAX_BYTES = 64 * 1024 * 1024
CATALOG_CACHE_SECONDS = 6 * 3600
# Bumped when normalize_catalog keeps something new, so an older cache is refetched
# rather than read as "no data" (2: verifiedCommit).
CATALOG_SCHEMA = 2
_CATALOG_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z')
# Only plain GitHub repos: this URL is handed to `git clone`.
_CATALOG_REPO = re.compile(r'https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9._-]*\Z')
# Preview images are the catalog's own (relative to its site); only plain image paths there.
CATALOG_SITE = 'https://plugins.omarchy.org/'
_COMMIT = re.compile(r'[0-9a-f]{40}')
_CATALOG_PREVIEW = re.compile(r'assets/img/plugins/[A-Za-z0-9._-]+\.(?:webp|png|jpe?g)\Z')
# How long a just-added plugin may take to show up in the shell's registry before enabling it.
INSTALL_DISCOVER_SECONDS = 5


def catalog_path():
    return STATE / 'catalog.json'


def _clean(value, limit):
    return ''.join(c for c in str(value or '') if c.isprintable())[:limit].strip()


def catalog_repo(value):
    """The entry's GitHub repo without a trailing slash or .git, or '' if it is not one."""
    url = str(value or '').strip().rstrip('/')
    url = url[:-4] if url.endswith('.git') else url
    return url if _CATALOG_REPO.fullmatch(url) else ''


def normalize_catalog(data):
    """The catalog's community plugins, trimmed to what the panel shows. Omarchy's own
    built-ins are left out (the Dashboard never lists those), and so is any entry without
    a usable id; one bad entry does not cost the rest. `installable` is the catalog's own
    say-so that `omarchy plugin add` handles it alone -- a suite or a monorepo has its own
    installer, which `note` points at."""
    if not isinstance(data, dict) or not isinstance(data.get('plugins'), list):
        raise ValueError('not a plugin catalog')
    plugins, seen = [], set()
    for e in data['plugins']:
        if not isinstance(e, dict) or e.get('sourceType') != 'community':
            continue
        plugin_id = str(e.get('id') or '')
        if not _CATALOG_ID.fullmatch(plugin_id) or plugin_id in seen:
            continue
        seen.add(plugin_id)
        repo = catalog_repo(e.get('repo'))
        installable = (e.get('repositoryLayout') == 'root-plugin' and e.get('installAvailable') is True
                       and e.get('status') == 'Available' and bool(repo))
        if e.get('verificationStatus') == 'verified' and e.get('verificationCoverage') == 'snapshot-verified':
            verification = 'verified'
        elif e.get('verificationCoverage') == 'update-unverified':
            verification = 'update-unverified'
        else:
            verification = 'unverified'
        # The commit the marketplace's security baseline passed. When upstream has moved on
        # since ('update-unverified'), this is the older snapshot that was checked.
        checked = str(e.get('verificationCommit') or '').lower()
        if e.get('verificationSnapshotStatus') != 'verified' or not _COMMIT.fullmatch(checked):
            checked = ''
        stars = e.get('stars')
        thumb = str(e.get('previewThumbnail') or '')
        width, height = e.get('previewThumbnailWidth'), e.get('previewThumbnailHeight')
        plugins.append({
            'id': plugin_id, 'name': _clean(e.get('name'), 120) or plugin_id,
            'description': _clean(e.get('description'), 600), 'author': _clean(e.get('author'), 80),
            'version': _clean(e.get('version'), 40), 'category': _clean(e.get('category'), 40),
            'kind': _clean(e.get('kind'), 40),
            'tags': [_clean(t, 40) for t in (e.get('tags') or [])[:8] if isinstance(t, str)],
            'repo': repo, 'stars': stars if isinstance(stars, int) and stars >= 0 else 0,
            'added': _clean(e.get('addedAt'), 30), 'updated': _clean(e.get('repositoryUpdatedAt'), 30),
            'status': _clean(e.get('status'), 40), 'installable': installable,
            'note': '' if installable else (_clean(e.get('installNote'), 300)
                                            or 'Not installable from here; see its repository.'),
            'verification': verification, 'verifiedCommit': checked,
            # The card-sized thumbnail (720 px wide, ~30 KB), not the full detail image.
            'preview': CATALOG_SITE + thumb if _CATALOG_PREVIEW.fullmatch(thumb) else '',
            'previewAspect': round(width / height, 3) if isinstance(width, int) and isinstance(height, int)
                             and width > 0 and height > 0 else 16 / 9,
        })
    return plugins


def fetch_catalog():
    req = urllib.request.Request(CATALOG_URL, headers={'User-Agent': 'omarchy-plugin-dashboard',
                                                       'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if not resp.geturl().startswith('https://plugins.omarchy.org/'):
            raise ValueError('the catalog redirected somewhere else')
        payload = resp.read(CATALOG_MAX_BYTES + 1)
    if len(payload) > CATALOG_MAX_BYTES:
        raise ValueError('the catalog is larger than expected')
    data = json.loads(payload)
    return {'generatedAt': _clean(data.get('generatedAt') if isinstance(data, dict) else '', 40),
            'plugins': normalize_catalog(data)}


def refresh_catalog(force=False):
    """(catalog, error): catalog.json (the panel reads it), refetched unless it is younger
    than CATALOG_CACHE_SECONDS. A failed fetch keeps the old list, records why on it, and
    returns it -- or {} when there never was one."""
    cached = _read_json(catalog_path(), {})
    if not force and status_is_fresh(cached, max_age=CATALOG_CACHE_SECONDS) and not cached.get('error') \
            and cached.get('schema') == CATALOG_SCHEMA:
        return cached, ''
    try:
        fresh = fetch_catalog()
    except (OSError, ValueError) as e:
        message = f'Could not load the plugin catalog: {e}'
        if cached:
            cached.update(error=message, errorTs=time.time())
            _write_json(catalog_path(), cached, compact=True)
        return cached, message
    fresh['ts'] = time.time()
    fresh['schema'] = CATALOG_SCHEMA
    _write_json(catalog_path(), fresh, compact=True)
    return fresh, ''


def cmd_catalog(args):
    catalog, error = refresh_catalog(getattr(args, 'force', False))
    if error:
        print(json.dumps({'ok': False, 'message': error}))
    else:
        print(json.dumps({'ok': True, 'ts': catalog['ts'], 'count': len(catalog.get('plugins', []))}))


def catalog_index(catalog):
    """{'ids': {id: entry}, 'repos': {lowercased repo url: entry}} for finding an installed
    plugin in the catalog: by its id, or else by where it was cloned from (a plugin whose
    manifest id differs from its listing still matches)."""
    ids, repos = {}, {}
    for e in (catalog or {}).get('plugins') or []:
        ids[e.get('id')] = e
        if e.get('repo'):
            repos[e['repo'].lower()] = e
    return {'ids': ids, 'repos': repos}


def market_status(item, index):
    """What the marketplace says about the code this plugin runs -- and, when it is behind,
    about the code an update would bring -- or None when it is not listed. A commit is
    'verified' only if it is the exact one the marketplace's security baseline checked;
    anything else, including a newer upstream commit or local commits of your own, is
    'unverified'. Uncommitted edits are not covered either way; the row says EDITED."""
    entry = index['ids'].get(item['id'])
    path = (item.get('info') or {}).get('path') or ''
    if entry is None:
        entry = index['repos'].get(((item.get('info') or {}).get('webUrl') or '').lower())
    if entry is None:
        return None
    checked = entry.get('verifiedCommit') or ''

    def commit(ref):
        rc, out, _ = run(['git', '-C', path, 'rev-parse', '--verify', '--quiet', ref + '^{commit}'])
        return out.strip() if rc == 0 else ''

    def verdict(sha):
        return 'verified' if sha and checked and sha == checked else 'unverified'

    head = commit('HEAD') if path else ''
    result = {'id': entry['id'], 'url': CATALOG_SITE + 'plugin.html?id=' + entry['id'],
              'checked': checked[:7], 'installed': verdict(head), 'incoming': '', 'sinceChecked': -1}
    if item.get('updateState') == 'behind':
        result['incoming'] = verdict(commit('FETCH_HEAD'))
    # How far past the checked commit you are, when that commit is in the checkout.
    if head and checked and head != checked:
        rc, out, _ = run(['git', '-C', path, 'rev-list', '--count', checked + '..HEAD'])
        if rc == 0 and out.strip().isdigit():
            result['sinceChecked'] = int(out.strip())
    return result


def registry_has(plugin_id, wait=INSTALL_DISCOVER_SECONDS):
    """Whether the shell's registry lists this plugin yet; it rescans in the background."""
    deadline = time.time() + wait
    while True:
        rc, out, _ = run(['omarchy', 'plugin', 'list', '--json'])
        try:
            if rc == 0 and any(p.get('id') == plugin_id for p in json.loads(out)):
                return True
        except (ValueError, AttributeError, TypeError):
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def cmd_install(args):
    """Installs a catalog plugin: `omarchy plugin add` (clone, validate, refuse an id that is
    taken), then `omarchy plugin enable` at its manifest's own bar section. The repo comes
    from the cached catalog, never from the caller, and no command the catalog carries is
    run. A plugin that replaces the whole bar is added but left off. Like an update, it
    then reads the shell's log for whether the new plugin loaded."""
    entry = next((p for p in _read_json(catalog_path(), {}).get('plugins', [])
                  if isinstance(p, dict) and p.get('id') == args.id), None)
    if not entry:
        print(json.dumps({'ok': False, 'message': f'{args.id} is not in the plugin catalog; refresh Discover.'}))
        return
    repo = catalog_repo(entry.get('repo'))
    if not entry.get('installable') or not repo:
        print(json.dumps({'ok': False, 'message': entry.get('note') or 'The catalog does not offer it for install.'}))
        return
    if (PLUGINS_DIR / args.id).exists() or (PLUGINS_DIR / args.id).is_symlink():
        print(json.dumps({'ok': False, 'message': f'{entry["name"]} is already installed.'}))
        return
    log_mark = shell_log_mark()
    rc, out, err = run(['omarchy', 'plugin', 'add', repo + '.git', '--yes'], timeout=300,
                       stream_log=STATE / 'action.log')
    # omarchy installs under the id in the repo's manifest, which the catalog's id should
    # match but is not guaranteed to.
    m = re.search(r'^Added (\S+) into ', out or '', re.M)
    plugin_id = m.group(1) if m else args.id
    plugin_dir = PLUGINS_DIR / plugin_id
    if rc != 0 or not plugin_dir.is_dir():
        message = '\n'.join(t for t in ((out or '').strip(), (err or '').strip()) if t)[-4000:] \
            or 'omarchy plugin add failed.'
        append_history({'ts': time.time(), 'id': plugin_id, 'name': entry['name'], 'kind': 'plugin',
                        'action': 'install', 'ok': False, 'before': '', 'after': '', 'fromVersion': '',
                        'toVersion': '', 'unitsInstalled': [],
                        'message': (message.splitlines() or [''])[-1][:200]})
        print(json.dumps({'ok': False, 'message': message}))
        check_all()
        return
    messages, ok = [(out or '').strip()], True
    try:
        kinds = json.loads((plugin_dir / 'manifest.json').read_text()).get('kinds') or []
    except (OSError, ValueError, AttributeError):
        kinds = []
    if 'bar' in kinds:
        messages.append('It replaces the whole bar, so it is left off; turn it on from the list when you want it.')
    elif not registry_has(plugin_id):
        ok = False
        messages.append(f'Installed, but the shell did not pick it up to turn on; try `omarchy plugin enable {plugin_id}`.')
    else:
        erc, eout, eerr = run(['omarchy', 'plugin', 'enable', plugin_id], timeout=60)
        if erc != 0:
            ok = False
            messages.append('Installed, but turning it on failed:\n' + (eerr or eout).strip())
        elif (eout or '').strip():
            messages.append(eout.strip())
    # omarchy plugin add clones and enables; services the plugin's own installer would set
    # up are not, so say so rather than leave it half working without a word.
    units = sorted(shipped_units(plugin_dir))
    if units:
        messages.append('It ships background services (' + ', '.join(units) + ') that installing a plugin '
                        'does not set up; its README says how.')
    if ok:
        load_ok, load_text = check_plugin_loaded(plugin_id, plugin_dir, log_mark)
        if load_text:
            messages.append(load_text)
        ok = load_ok
    message = '\n'.join(t for t in messages if t)
    append_history({'ts': time.time(), 'id': plugin_id, 'name': entry['name'], 'kind': 'plugin',
                    'action': 'install', 'ok': ok, 'before': '', 'after': repo_head(plugin_dir),
                    'fromVersion': '', 'toVersion': manifest_version(plugin_id), 'unitsInstalled': [],
                    'message': '' if ok else (message.splitlines() or [''])[-1][:200]})
    print(json.dumps({'ok': ok, 'message': message, 'id': plugin_id}))
    check_all()


def job_path():
    return STATE / 'job.json'


def load_job():
    return _read_json(job_path(), {})


def save_job(job):
    _write_json(job_path(), job)


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def job_active(job, now=None):
    """Whether this job is still going: its worker is alive, or it was handed over so
    recently that the worker may not have taken it yet."""
    if job.get('state') == 'running':
        return pid_alive(job.get('pid'))
    if job.get('state') == 'starting':
        return (now if now is not None else time.time()) - job.get('startTs', 0) < JOB_START_GRACE_SECONDS
    return False


def reap_stale_job(now=None):
    """Closes a job whose worker died without closing it (killed, the machine went down),
    so the panel does not show it busy forever."""
    job = load_job()
    if job.get('state') not in ('starting', 'running') or job_active(job, now):
        return
    finished = {r.get('id') for r in job.get('results', [])}
    job['results'] = job.get('results', []) + [
        {'id': i, 'ok': False, 'message': 'Did not finish: the Dashboard\'s worker stopped first '
                                          '(journalctl --user -u "dashboard-job-*").'}
        for i in job.get('ids', []) if i not in finished]
    job.update(state='done', current='', finishedTs=now if now is not None else time.time())
    save_job(job)


def new_job(kind, ids, options=None):
    """Claims job.json for a new job, or returns None while another one is still going."""
    reap_stale_job()
    if job_active(load_job()):
        return None
    now = time.time()
    job = {'jobId': f'{int(now * 1000)}', 'kind': kind, 'ids': list(ids), 'state': 'starting',
           'current': ids[0] if ids else '', 'itemTs': now, 'startTs': now, 'results': [], 'pid': None,
           'options': options or {}}
    save_job(job)
    return job


def run_job_item(kind, item_id, options=None):
    """One item of a job through the same command the panel used to run directly; its
    printed JSON result is what the job records."""
    handler = {'update': cmd_update, 'rollback': cmd_rollback, 'remove': cmd_remove, 'cleanup': cmd_cleanup,
               'install': cmd_install}[kind]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            handler(argparse.Namespace(id=item_id, **(options or {})))
    except Exception as e:  # noqa: BLE001 -- one item's crash must not lose the rest of the job
        return {'ok': False, 'message': f'{type(e).__name__}: {e}'}
    for line in buf.getvalue().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and 'ok' in r:
            return {'ok': bool(r['ok']), 'message': r.get('message') or ''}
    return {'ok': False, 'message': 'It did not report a result.'}


def execute_job(job):
    """Runs a claimed job in this process, item by item, keeping job.json current so a
    panel (re)created at any point shows where it is."""
    job.update(state='running', pid=os.getpid())
    save_job(job)
    try:
        for item_id in job['ids']:
            job.update(current=item_id, itemTs=time.time())
            save_job(job)
            job['results'].append(dict(run_job_item(job['kind'], item_id, job.get('options')), id=item_id))
            save_job(job)
    finally:
        job.update(state='done', current='', finishedTs=time.time())
        save_job(job)
    return job


def cmd_start(args):
    """Hands a job to its own transient user unit and returns at once; see JOB_KINDS."""
    job = new_job(args.kind, args.ids, {'purge': True} if getattr(args, 'purge', False) else None)
    if job is None:
        print(json.dumps({'ok': False, 'message': 'Another update is still running; wait for it to finish.'}))
        return
    worker = [sys.executable, str(Path(__file__).resolve()), 'run-job', job['jobId']]
    rc, out, err = run(['systemd-run', '--user', '--collect', '--quiet',
                        f'--unit=dashboard-job-{job["jobId"]}', *worker], timeout=15)
    if rc != 0:
        # No user manager to ask: a new session still outlives the panel's process.
        try:
            subprocess.Popen(worker, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            job.update(state='done', current='', finishedTs=time.time(),
                       results=[{'id': i, 'ok': False, 'message': f'Could not start: {e}'} for i in job['ids']])
            save_job(job)
            print(json.dumps({'ok': False, 'message': f'Could not start: {e}'}))
            return
    print(json.dumps({'ok': True, 'jobId': job['jobId']}))


def cmd_run_job(args):
    job = load_job()
    if job.get('jobId') != args.job_id or job.get('state') != 'starting':
        return
    execute_job(job)


def cmd_diff(args):
    """Preview what an Update would actually apply, without applying it --
    the widget only ever offers this for 'behind'/'diverged' items, so
    FETCH_HEAD (plugins) or remote/branch (apps) is already fresh from the
    check that put them in that state."""
    ensure_apps_file()
    apps = {a['id']: a for a in load_apps()}
    if args.id in apps:
        a = apps[args.id]
        branch = a.get('branch', 'master')
        remote_ref = f"{a.get('remote', 'origin')}/{branch}"
        # Diff from the same base app_update_state counted 'behind' from: `branch`, unless it
        # already has upstream and only the checked-out branch (Flea's aarch64-local) lacks
        # it -- diffing `branch` there was empty. Three dots: only upstream's side of the
        # change, not our own local commits shown as if upstream would delete them.
        rc, _, _ = run(['git', '-C', a['repoDir'], 'merge-base', '--is-ancestor', remote_ref, branch])
        base = 'HEAD' if rc == 0 else branch
        rc, out, err = run(['git', '-C', a['repoDir'], 'diff', f'{base}...{remote_ref}'], timeout=20)
        if rc == 0:
            # No 'local' list for apps: their checked-out branch (Flea's aarch64-local)
            # carries our own patches by design, and those are not what the update changes.
            incoming, local = git_log(a['repoDir'], f'{base}..{remote_ref}'), []
    else:
        plugin_dir = PLUGINS_DIR / args.id
        rc, out, err = run(['git', '-C', str(plugin_dir), 'diff', 'HEAD', 'FETCH_HEAD'], timeout=20)
        if rc == 0:
            incoming, local = git_log(plugin_dir, 'HEAD..FETCH_HEAD'), git_log(plugin_dir, 'FETCH_HEAD..HEAD')
    if rc != 0:
        print(json.dumps({'ok': False, 'message': (err or 'Could not compute diff.').strip()[-2000:]}))
        return
    print(json.dumps({'ok': True, 'diff': out[-20000:] or '(no textual diff)',
                      'incoming': incoming, 'local': local}))


def pull_upstream(repo_dir, branch, remote):
    """For an app whose update command only builds: bring `remote/branch` into the checked-out
    `branch`, fast-forward only, so local work is never merged over or thrown away."""
    rc, head, _ = run(['git', '-C', str(repo_dir), 'rev-parse', '--abbrev-ref', 'HEAD'])
    if rc != 0 or head.strip() != branch:
        return 1, '', f"Pull upstream first needs {branch} checked out; it is on {head.strip() or '?'}."
    rc, out, err = run(['git', '-C', str(repo_dir), 'fetch', '--quiet', remote], timeout=60)
    if rc != 0:
        return rc, out, err
    rc, out, err = run(['git', '-C', str(repo_dir), 'merge', '--ff-only', f'{remote}/{branch}'], timeout=60)
    if rc != 0:
        err = (err.strip() + '\n' if err.strip() else '') + \
            f'Could not fast-forward {branch}: it has commits {remote}/{branch} does not. Merge them by hand.'
    return rc, out, err


def inspect_package(name):
    """(fields, notes) for the Add app form from an installed package, or (None, [why])."""
    pkg = name.strip()
    if not re.fullmatch(r'[A-Za-z0-9@._+-]+', pkg):
        return None, ['Enter a package name, e.g. brave-bin.']
    real = installed_name(pkg)
    if not real:
        # People type the app's name (brave); the package is often brave-bin.
        rc, out, _ = run(['pacman', '-Qq'])
        close = [n for n in out.split() if pkg.lower() in n.lower()][:5] if rc == 0 else []
        return None, [f'{pkg} is not installed (pacman -Q {pkg}).'
                      + (' Did you mean ' + ' or '.join(close) + '?' if close else '')]
    notes = []
    if real != pkg:
        notes.append(f'{pkg} is provided by {real}; tracking {real}.')
        pkg = real
    repo = sync_repo(pkg)
    if repo:
        notes.append(f'From the {repo} repo: Update runs omarchy update (the whole system -- '
                     'Arch does not support upgrading one repo package alone).')
    elif pkg.endswith(VCS_SUFFIXES):
        notes.append('A VCS package: the AUR cannot say when its source moves, so it will show no update source.')
    else:
        notes.append('From the AUR (or built locally): Update runs yay -S ' + pkg + '.')
    base = re.sub(r'-(bin|git|appimage)$', '', pkg)
    fields = {'source': 'package', 'pkgName': pkg, 'id': pkg,
              'name': ' '.join(w[:1].upper() + w[1:] for w in re.split(r'[-_]+', base) if w) or pkg}
    if fields['id'] in {a.get('id') for a in load_apps()}:
        notes.append(f"An app with id {fields['id']} is already tracked; saving replaces it.")
    return fields, notes


def cmd_inspect_package(args):
    ensure_apps_file()
    fields, notes = inspect_package(args.name)
    print(json.dumps({'ok': fields is not None, 'fields': fields or {}, 'notes': notes}))


def cmd_add_app(args):
    try:
        fields = json.loads(args.json)
    except ValueError:
        print(json.dumps({'ok': False, 'message': 'Invalid app data.'}))
        return
    if fields.get('source') == 'package':
        pkg = str(fields.get('pkgName') or '').strip()
        app_id = str(fields.get('id') or pkg).strip()
        name = str(fields.get('name') or app_id).strip()
        if not pkg or not app_id or not name:
            print(json.dumps({'ok': False, 'message': 'A package name is required.'}))
            return
        real = installed_name(pkg)
        if not real:
            print(json.dumps({'ok': False, 'message': f'{pkg} is not installed.'}))
            return
        if app_id == pkg:
            app_id = real
        pkg = real
        ensure_apps_file()
        apps = [a for a in load_apps() if a.get('id') != app_id]
        apps.append({'id': app_id, 'name': name, 'source': 'package', 'pkgName': pkg})
        save_apps(apps)
        print(json.dumps({'ok': True, 'message': 'Added ' + name + '.'}))
        check_all()
        return
    app_id = str(fields.get('id') or '').strip()
    name = str(fields.get('name') or '').strip()
    repo_dir = str(fields.get('repoDir') or '').strip()
    if not app_id or not name or not repo_dir:
        print(json.dumps({'ok': False, 'message': 'Name, id, and repo dir are all required.'}))
        return
    if not (Path(repo_dir).expanduser() / '.git').is_dir():
        print(json.dumps({'ok': False, 'message': repo_dir + ' is not a git repository.'}))
        return
    # Missing means the default; given but empty (the form's field cleared, or a repo with
    # no rebuild.sh) is an error rather than a silent rebuild.sh that may not exist.
    update_cmd = fields.get('updateCmd', ['./rebuild.sh', '--install'])
    if isinstance(update_cmd, str):
        update_cmd = update_cmd.split()
    if not update_cmd:
        print(json.dumps({'ok': False, 'message': 'An update command is required: the one that builds and installs it.'}))
        return
    entry = {
        'id': app_id, 'name': name, 'repoDir': repo_dir,
        'pkgName': str(fields.get('pkgName') or app_id),
        'branch': str(fields.get('branch') or 'master'),
        'remote': str(fields.get('remote') or 'origin'),
        'updateCmd': update_cmd,
        'pullFirst': bool(fields.get('pullFirst')),
    }
    ensure_apps_file()
    apps = [a for a in load_apps() if a.get('id') != app_id]
    apps.append(entry)
    save_apps(apps)
    print(json.dumps({'ok': True, 'message': 'Added ' + name + '.'}))
    check_all()


def inspect_repo(repo_dir):
    """Best guesses for the Add app form from the repo itself, so only what cannot be
    guessed needs typing: (fields, notes), or (None, [why not]) if it is not a git repo."""
    repo = Path(repo_dir).expanduser()
    if not (repo / '.git').exists():
        return None, [f'{repo} is not a git repository.']
    notes = []
    base = re.sub(r'[^a-z0-9._-]+', '-', repo.name.lower()).strip('-') or 'app'
    fields = {'name': repo.name[:1].upper() + repo.name[1:], 'id': base, 'repoDir': str(repo),
              'pkgName': base, 'branch': '', 'remote': '', 'updateCmd': '', 'pullFirst': False}
    srcinfo = {}
    try:
        for line in (repo / '.SRCINFO').read_text().splitlines():
            key, sep, value = line.strip().partition(' = ')
            if sep and key in ('pkgbase', 'pkgname') and key not in srcinfo:
                srcinfo[key] = value.strip()
    except (OSError, UnicodeDecodeError):
        notes.append('No .SRCINFO: the installed version will not be compared with the repo.')
    if srcinfo:
        fields['pkgName'] = srcinfo.get('pkgname') or srcinfo.get('pkgbase')
    rc, out, _ = run(['git', '-C', str(repo), 'remote'])
    remotes = out.split() if rc == 0 else []
    if not remotes:
        notes.append('No git remote: there is nothing to check for updates against.')
    else:
        fields['remote'] = 'origin' if 'origin' in remotes else remotes[0]
        # The branch that tracks upstream (Flea: master, tracking the AUR), not
        # necessarily the one checked out (Flea: aarch64-local, our patches on top).
        rc, out, _ = run(['git', '-C', str(repo), 'symbolic-ref', '--short', f"refs/remotes/{fields['remote']}/HEAD"])
        if rc == 0 and out.strip():
            fields['branch'] = out.strip().split('/', 1)[-1]
        else:
            for candidate in ('master', 'main'):
                if run(['git', '-C', str(repo), 'rev-parse', '--verify', '--quiet', candidate])[0] == 0:
                    fields['branch'] = candidate
                    break
    if os.access(repo / 'rebuild.sh', os.X_OK):
        fields['updateCmd'] = './rebuild.sh --install'
    else:
        # A rebuild.sh brings upstream in itself; anything else is taken to only build, so
        # the Dashboard pulls first.
        fields['pullFirst'] = True
        notes.append('No executable rebuild.sh: enter the command that builds and installs it. '
                     'The Dashboard pulls upstream (fast-forward) before running it.')
    if fields['id'] in {a.get('id') for a in load_apps()}:
        notes.append(f"An app with id {fields['id']} is already tracked; saving replaces it.")
    return fields, notes


def cmd_inspect_repo(args):
    ensure_apps_file()
    fields, notes = inspect_repo(args.dir)
    print(json.dumps({'ok': fields is not None, 'fields': fields or {}, 'notes': notes}))


def cmd_pick_folder(args):
    """The desktop's own folder chooser (the FileChooser portal, via omarchy-file-select)
    in a process of its own -- Qt's FileDialog inside the shell has aborted the whole
    shell before (omamail's attachment picker) -- then the same guesses as inspect-repo."""
    ensure_apps_file()
    rc, out, err = run(['omarchy-file-select', '--title', 'Choose the app\'s git repo', '--directory'], timeout=600)
    path = out.strip().splitlines()[0] if rc == 0 and out.strip() else ''
    if not path:
        print(json.dumps({'ok': False, 'cancelled': rc == 0 or not err.strip(), 'notes': [err.strip()] if err.strip() else []}))
        return
    fields, notes = inspect_repo(path)
    print(json.dumps({'ok': fields is not None, 'fields': fields or {'repoDir': path}, 'notes': notes}))


# Per-plugin keyboard shortcuts: a key combination that opens a plugin's popup, written to
# Hyprland's bindings.lua as a marked block the Dashboard owns (the way `flea --default`
# writes its own), so it can be changed or removed whole without touching anything else.
SHORTCUT_MODS = {'SUPER': 64, 'CTRL': 4, 'ALT': 8, 'SHIFT': 1}
SHORTCUT_KEY = re.compile(r'[A-Z0-9]|F(?:[1-9]|1[0-2])\Z')
SHORTCUT_KINDS = ('bar-widget', 'panel', 'overlay')   # what `omarchy-shell shell toggle` opens
_SHORTCUT_BLOCK = re.compile(r'\n?^-- plugin-dashboard shortcut: (?P<id>\S+) begin\.[^\n]*\n'
                             r'(?P<body>.*?)^-- plugin-dashboard shortcut: (?P=id) end\n?', re.M | re.S)


def normalize_combo(value):
    """("SUPER + ALT + P", modmask, "P") for a combination, or ValueError saying why not.
    One key, at least one of Super/Ctrl/Alt (Shift alone is typing), in a fixed order."""
    parts = [p.strip().upper().replace('CONTROL', 'CTRL').replace('META', 'SUPER')
             for p in str(value or '').split('+') if p.strip()]
    mods = [p for p in parts if p in SHORTCUT_MODS]
    keys = [p for p in parts if p not in SHORTCUT_MODS]
    if len(keys) != 1 or len(set(parts)) != len(parts) or not SHORTCUT_KEY.fullmatch(keys[0]):
        raise ValueError('Use a letter, a digit or F1-F12 with modifiers.')
    if not set(mods) & {'SUPER', 'CTRL', 'ALT'}:
        raise ValueError('Include Super, Ctrl or Alt.')
    ordered = [m for m in SHORTCUT_MODS if m in mods]
    return ' + '.join(ordered + keys), sum(SHORTCUT_MODS[m] for m in ordered), keys[0]


def shortcut_description(name):
    return f'{name} (Plugin Dashboard)'


def shortcut_block(plugin_id, name, combo):
    def lua(text):
        return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return (f'-- plugin-dashboard shortcut: {plugin_id} begin. Written by the Plugin Dashboard; '
            f'removing the shortcut there removes the block whole.\n'
            f'o.bind({lua(combo)}, {lua(shortcut_description(name))}, {lua("omarchy-shell shell toggle " + plugin_id)})\n'
            f'-- plugin-dashboard shortcut: {plugin_id} end\n')


def read_shortcuts(raw=None):
    """{plugin id: combination} for the Dashboard's own blocks in bindings.lua."""
    if raw is None:
        try:
            raw = BINDINGS_PATH.read_text()
        except OSError:
            return {}
    found = {}
    for m in _SHORTCUT_BLOCK.finditer(raw):
        bind = re.search(r'o\.bind\("([^"\n]+)"', m.group('body'))
        if bind:
            found[m.group('id')] = bind.group(1)
    return found


def shortcut_conflict(binds, combo, own_description):
    """Why this combination is not free, or ''. Compares with Hyprland's live bindings: the
    same modifiers (Caps and Num Lock aside) and key. A binding by keycode ("code:10")
    lists no key at all, so for a digit -- Omarchy binds digits that way -- whether it is
    taken cannot be told, and that is said instead of guessed."""
    _, mask, key = normalize_combo(combo)
    for b in binds if isinstance(binds, list) else []:
        if not isinstance(b, dict) or b.get('submap') or b.get('mouse'):
            continue
        if int(b.get('modmask') or 0) & ~(2 | 16) != mask:
            continue
        bound = str(b.get('key') or '').upper()
        what = b.get('description') or 'another binding'
        if bound == key:
            if b.get('description') == own_description:
                continue
            return f'{combo} is already used for {what}.'
        if (not bound or b.get('catch_all')) and not key.isalpha() and not key.startswith('F'):
            return f'Cannot tell whether {combo} is free ({what} is bound by key position). Try a letter or an F key.'
    return ''


def _bindings_file():
    """(text, mode) of bindings.lua, which must be the user's own regular file."""
    st = BINDINGS_PATH.lstat()
    if not (st.st_mode & 0o170000 == 0o100000) or st.st_uid != os.getuid() or st.st_size > 1024 * 1024:
        raise ValueError(f'{BINDINGS_PATH} is not a regular file of yours.')
    return BINDINGS_PATH.read_text(), st.st_mode & 0o7777


def _write_bindings(text, mode):
    tmp = BINDINGS_PATH.with_name('.' + BINDINGS_PATH.name + '.dashboard-tmp')
    tmp.write_text(text)
    os.chmod(tmp, mode)
    tmp.replace(BINDINGS_PATH)


def set_shortcut(plugin_id, name, combo):
    """Writes (combo) or removes ('') the plugin's block in bindings.lua and reloads
    Hyprland. Refuses a taken combination, and refuses to write at all while Hyprland
    already reports config errors (a reload would not prove anything). If the reload
    reports errors afterwards, the old file goes back."""
    if not BINDINGS_PATH.exists():
        return {'ok': False, 'message': f'No {BINDINGS_PATH}: shortcuts need Omarchy\'s Lua bindings file.'}
    if combo:
        try:
            combo = normalize_combo(combo)[0]
        except ValueError as e:
            return {'ok': False, 'message': str(e)}
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / 'bindings.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {'ok': False, 'message': 'Another shortcut change is being saved.'}
        try:
            raw, mode = _bindings_file()
        except (OSError, ValueError) as e:
            return {'ok': False, 'message': str(e)}
        base = _SHORTCUT_BLOCK.sub(lambda m: '' if m.group('id') == plugin_id else m.group(0), raw)
        if combo:
            rc, out, _ = run(['hyprctl', '-j', 'binds'])
            try:
                binds = json.loads(out) if rc == 0 else None
            except ValueError:
                binds = None
            if binds is None:
                return {'ok': False, 'message': 'Could not read Hyprland\'s current bindings.'}
            why = shortcut_conflict(binds, combo, shortcut_description(name))
            if why:
                return {'ok': False, 'message': why}
        rc, errors, _ = run(['hyprctl', 'configerrors'])
        if rc == 0 and errors.strip():
            return {'ok': False, 'message': 'Hyprland already reports config errors; fix those first: '
                                            + errors.strip().splitlines()[0]}
        updated = (base.rstrip('\n') + '\n\n' + shortcut_block(plugin_id, name, combo)) if combo else base
        if updated == raw:
            return {'ok': True, 'message': 'Shortcut unchanged.'}
        _write_bindings(updated, mode)
        run(['hyprctl', 'reload'])
        rc, errors, _ = run(['hyprctl', 'configerrors'])
        if rc == 0 and errors.strip():
            _write_bindings(raw, mode)
            run(['hyprctl', 'reload'])
            return {'ok': False, 'message': 'Hyprland rejected it, so bindings.lua was put back: '
                                            + errors.strip().splitlines()[0]}
    return {'ok': True, 'message': f'{combo} opens {name}.' if combo else f'Removed the shortcut for {name}.'}


def cmd_shortcut(args):
    plugin = next((p for p in discover_plugins() if p['id'] == args.id), None)
    if plugin is None:
        print(json.dumps({'ok': False, 'message': 'Not an installed third-party plugin.'}))
        return
    name = re.sub(r'[\x00-\x1f"\\]', '', str(plugin.get('name') or args.id))[:80] or args.id
    if args.combo and not set(plugin.get('kinds') or []) & set(SHORTCUT_KINDS):
        print(json.dumps({'ok': False, 'message': f'{name} has no popup or panel for a shortcut to open.'}))
        return
    result = set_shortcut(args.id, name, args.combo)
    print(json.dumps(result))
    if result['ok']:
        check_all()


def shell_cache_dir():
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'quickshell'


def clear_shell_cache():
    """Removes the shell's compiled QML and pipeline caches, so a restart compiles every
    plugin from its files again: what fixes a plugin still running old code after its files
    changed. Both are rebuilt on the next start; nothing else in the cache is touched."""
    cache = shell_cache_dir()
    for path in [cache / 'qmlcache', *cache.glob('qtpipelinecache-*')]:
        shutil.rmtree(path, ignore_errors=True)


def cmd_restart_shell(args):
    """Restart shell: clears the compiled-QML cache and runs omarchy-restart-shell. The
    restart kills the panel that asked for it, so the work runs in its own transient unit
    (`--now` is that unit), and a failure is reported as a notification. Refused while a
    Dashboard job runs: the update's load check reads the shell's log, and a restart in
    the middle would read as the plugin failing to load."""
    if args.now:
        clear_shell_cache()
        rc, out, err = run(['omarchy-restart-shell'], timeout=120)
        if rc != 0:
            lines = (err or out).strip().splitlines()
            run(['notify-send', '--app-name=Plugin Dashboard', '--icon=dialog-error', 'Shell not restarted',
                 lines[-1] if lines else 'omarchy-restart-shell failed.'], timeout=5)
        return
    reap_stale_job()
    if job_active(load_job()):
        print(json.dumps({'ok': False, 'message': 'An update is still running; restart once it is done.'}))
        return
    worker = [sys.executable, str(Path(__file__).resolve()), 'restart-shell', '--now']
    rc, _, _ = run(['systemd-run', '--user', '--collect', '--quiet',
                    f'--unit=dashboard-restart-shell-{int(time.time())}', *worker], timeout=15)
    if rc != 0:
        try:
            subprocess.Popen(worker, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            print(json.dumps({'ok': False, 'message': f'Could not start the restart: {e}'}))
            return
    print(json.dumps({'ok': True, 'message': 'Restarting the shell…'}))


def cmd_remove_app(args):
    ensure_apps_file()
    apps = load_apps()
    remaining = [a for a in apps if a.get('id') != args.id]
    if len(remaining) == len(apps):
        print(json.dumps({'ok': False, 'message': 'No tracked app with that id.'}))
        return
    save_apps(remaining)
    print(json.dumps({'ok': True, 'message': 'Stopped tracking ' + args.id + '.'}))
    check_all()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check').add_argument('--force', action='store_true')
    sub.add_parser('catalog').add_argument('--force', action='store_true')
    for name in ('enable', 'disable', 'update', 'remove', 'diff', 'remove-app', 'rollback', 'unhold'):
        p = sub.add_parser(name)
        p.add_argument('id')
    sub.add_parser('inspect-repo').add_argument('dir')
    sub.add_parser('pick-folder')
    sub.add_parser('inspect-package').add_argument('name')
    sub.add_parser('terminal-update').add_argument('ids', nargs='+')
    sub.add_parser('open-terminal-update').add_argument('ids', nargs='+')
    p_await = sub.add_parser('await-notification')
    p_await.add_argument('title')
    p_await.add_argument('body')
    p_await.add_argument('ids', nargs='*')
    p_hold = sub.add_parser('hold')
    p_hold.add_argument('id')
    p_hold.add_argument('--reason', default='')
    p_add_app = sub.add_parser('add-app')
    p_add_app.add_argument('json')
    p_start = sub.add_parser('start')
    p_start.add_argument('kind', choices=JOB_KINDS)
    p_start.add_argument('ids', nargs='+')
    p_start.add_argument('--purge', action='store_true')
    sub.add_parser('remove-plan').add_argument('id')
    sub.add_parser('cleanup').add_argument('id')
    sub.add_parser('run-job').add_argument('job_id')
    sub.add_parser('restart-shell').add_argument('--now', action='store_true')
    p_shortcut = sub.add_parser('shortcut')
    p_shortcut.add_argument('id')
    p_shortcut.add_argument('combo', nargs='?', default='')
    args = parser.parse_args()
    handlers = {
        'check': cmd_check, 'enable': cmd_enable, 'disable': cmd_disable, 'update': cmd_update,
        'remove': cmd_remove, 'diff': cmd_diff, 'add-app': cmd_add_app, 'remove-app': cmd_remove_app,
        'rollback': cmd_rollback, 'hold': cmd_hold, 'unhold': cmd_unhold,
        'await-notification': cmd_await_notification, 'start': cmd_start, 'run-job': cmd_run_job,
        'remove-plan': cmd_remove_plan, 'cleanup': cmd_cleanup, 'catalog': cmd_catalog,
        'inspect-repo': cmd_inspect_repo, 'pick-folder': cmd_pick_folder,
        'inspect-package': cmd_inspect_package, 'terminal-update': cmd_terminal_update,
        'restart-shell': cmd_restart_shell, 'shortcut': cmd_shortcut,
        'open-terminal-update': lambda a: print(json.dumps({'ok': open_terminal_update(a.ids)})),
    }
    handlers[args.command](args)


if __name__ == '__main__':
    main()
