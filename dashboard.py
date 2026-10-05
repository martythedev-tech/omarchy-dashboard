#!/usr/bin/env python3
"""Dashboard: every third-party/own Omarchy plugin plus tracked local apps,
checked against their real upstream. No bespoke update logic of its own --
every action shells out to the command that already owns it (`omarchy plugin
enable/disable/update/remove` for plugins, each app's own rebuild.sh for
apps) and this script's only job is deciding when an Update button should
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
"""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SELF_ID = 'martythedev-tech.dashboard'
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
JOB_KINDS = ('update', 'rollback', 'remove')
# A job that is 'starting' this long without its worker having taken it never will.
JOB_START_GRACE_SECONDS = 60

# Seeded into APPS_PATH the first time it's missing; from then on the file on
# disk is what's read; edit it there (or via the "+ Add app" form) to add or
# change tracked apps.
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


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
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
    return DEFAULT_APPS


def save_apps(apps):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = APPS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(apps, indent=2) + '\n')
    tmp.replace(APPS_PATH)


def ensure_apps_file():
    if APPS_PATH.exists():
        return
    save_apps(DEFAULT_APPS)


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
    return [p for p in data if not p.get('firstParty')]


def manifest_version(plugin_id):
    path = PLUGINS_DIR / plugin_id / 'manifest.json'
    try:
        return json.loads(path.read_text()).get('version', '')
    except (OSError, ValueError):
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
    if dirty.strip():
        lines = dirty.strip().split('\n')
        return 'dirty', behind, f'{len(lines)} file(s) changed locally'
    rc5, ahead_count, _ = run(['git', '-C', str(plugin_dir), 'rev-list', '--count', 'FETCH_HEAD..HEAD'])
    ahead = int(ahead_count.strip()) if rc5 == 0 and ahead_count.strip().isdigit() else 0
    if ahead > 0:
        return 'diverged', behind, f'{ahead} local commit(s) not upstream'
    return 'behind', behind, ''


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
        'version': version, 'upstreamVersion': incoming,
        'versionChange': version_change(version, incoming), 'enabled': bool(p.get('enabled')),
        'canDisable': bool(p.get('canDisable', True)),
        'updateState': state, 'behind': behind, 'reason': reason, 'ahead': ahead,
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
        'version': version, 'upstreamVersion': incoming,
        'versionChange': version_change(version, incoming),
        'updateState': state, 'behind': behind, 'reason': reason, 'ahead': ahead,
        'info': repo_info(a['repoDir'], remote, f'{remote}/{branch}' if compared else None),
    }


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
    wanted = {i['id'] for i in status.get('items', [])
              if i['id'] in args.ids and i.get('updateState') in UPDATABLE_STATES and not i.get('held')}
    names = {i['id']: i.get('name', i['id']) for i in status.get('items', [])}
    done, failed = [], []
    ids = [i for i in args.ids if i in wanted]
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
        body = 'Nothing left to update.'
    else:
        body = ', '.join(filter(None, ['Updated ' + ', '.join(done) if done else '',
                                       'failed: ' + ', '.join(failed) if failed else '']))
    run(['notify-send', '--app-name=Plugin Dashboard',
         '--icon=' + ('dialog-error' if failed else 'system-software-update'),
         'Update failed' if failed else 'Updated', body + (' — open the Dashboard for details.' if failed else '')],
        timeout=5)


def check_all():
    old_behind_ids = _previously_behind_ids()
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        jobs = [pool.submit(check_plugin, p) for p in discover_plugins()] + \
               [pool.submit(check_app, a) for a in load_apps()]
        items = [job.result() for job in jobs]
    holds = load_holds()
    history = load_history()
    now = time.time()
    display_names = {i['id']: i.get('name') for i in items}
    for item in items:
        item['held'] = item['id'] in holds
        item['holdReason'] = holds.get(item['id'], {}).get('reason', '')
        candidate = rollback_candidate(item['id'], history, now) if item.get('kind') == 'plugin' else None
        item['rollback'] = ({'to': candidate['before'][:7], 'toVersion': candidate.get('fromVersion', ''),
                             'fromVersion': candidate.get('toVersion', '')} if candidate else None)
    status = {
        'ts': now,
        'items': items,
        'updatable': sum(1 for i in items if alerts(i)),
        # Plugin entries were recorded under their id (cmd_update knows no display name), so
        # Recent read "com.omastorm.radar updated"; show the name the rows use instead.
        'history': [dict({k: e.get(k) for k in ('id', 'name', 'kind', 'action', 'ok', 'fromVersion',
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
    repo_dir = app['repoDir'] if app else PLUGINS_DIR / args.id
    before = repo_head(repo_dir)
    from_version = item_version(args.id, app)
    units_installed_before = set()
    if app:
        a = app
        rc, out, err = run(a['updateCmd'], cwd=a['repoDir'], timeout=900, stream_log=STATE / 'action.log')
    else:
        plugin_dir = PLUGINS_DIR / args.id
        units_before = shipped_units(plugin_dir)
        units_installed_before = {name for name in units_before if (USER_UNIT_DIR / name).exists()}
        log_mark = shell_log_mark()
        rc, out, err = run(['omarchy', 'plugin', 'update', args.id, '--yes'], timeout=180, stream_log=STATE / 'action.log')
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
    _, dirty, _ = run(['git', '-C', str(plugin_dir), 'status', '--porcelain'])
    if dirty.strip():
        return done(False, f'{plugin_dir} has local changes a rollback would discard; commit or stash them first.')
    log_mark = shell_log_mark()
    rc, out, err = run(['git', '-C', str(plugin_dir), 'reset', '--hard', entry['before']])
    if rc != 0:
        return done(False, 'git reset failed: ' + (err or out).strip())
    rc, out, err = run(['omarchy', 'plugin', 'validate', str(plugin_dir)], timeout=60)
    if rc != 0:
        run(['git', '-C', str(plugin_dir), 'reset', '--hard', entry['after']])
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


def cmd_remove(args):
    if args.id == SELF_ID:
        print(json.dumps({'ok': False, 'message': 'Refusing to remove the Dashboard from within itself -- '
                                                    'use `omarchy plugin remove` from a terminal instead.'}))
        return
    rc, out, err = run(['omarchy', 'plugin', 'remove', args.id, '--yes'], timeout=60)
    print(json.dumps({'ok': rc == 0, 'message': (out or err).strip()}))
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


def new_job(kind, ids):
    """Claims job.json for a new job, or returns None while another one is still going."""
    reap_stale_job()
    if job_active(load_job()):
        return None
    now = time.time()
    job = {'jobId': f'{int(now * 1000)}', 'kind': kind, 'ids': list(ids), 'state': 'starting',
           'current': ids[0] if ids else '', 'itemTs': now, 'startTs': now, 'results': [], 'pid': None}
    save_job(job)
    return job


def run_job_item(kind, item_id):
    """One item of a job through the same command the panel used to run directly; its
    printed JSON result is what the job records."""
    handler = {'update': cmd_update, 'rollback': cmd_rollback, 'remove': cmd_remove}[kind]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            handler(argparse.Namespace(id=item_id))
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
            job['results'].append(dict(run_job_item(job['kind'], item_id), id=item_id))
            save_job(job)
    finally:
        job.update(state='done', current='', finishedTs=time.time())
        save_job(job)
    return job


def cmd_start(args):
    """Hands a job to its own transient user unit and returns at once; see JOB_KINDS."""
    job = new_job(args.kind, args.ids)
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


def cmd_add_app(args):
    try:
        fields = json.loads(args.json)
    except ValueError:
        print(json.dumps({'ok': False, 'message': 'Invalid app data.'}))
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
              'pkgName': base, 'branch': '', 'remote': '', 'updateCmd': ''}
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
        notes.append('No executable rebuild.sh: enter the command that builds and installs it.')
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
    for name in ('enable', 'disable', 'update', 'remove', 'diff', 'remove-app', 'rollback', 'unhold'):
        p = sub.add_parser(name)
        p.add_argument('id')
    sub.add_parser('inspect-repo').add_argument('dir')
    sub.add_parser('pick-folder')
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
    sub.add_parser('run-job').add_argument('job_id')
    args = parser.parse_args()
    handlers = {
        'check': cmd_check, 'enable': cmd_enable, 'disable': cmd_disable, 'update': cmd_update,
        'remove': cmd_remove, 'diff': cmd_diff, 'add-app': cmd_add_app, 'remove-app': cmd_remove_app,
        'rollback': cmd_rollback, 'hold': cmd_hold, 'unhold': cmd_unhold,
        'await-notification': cmd_await_notification, 'start': cmd_start, 'run-job': cmd_run_job,
        'inspect-repo': cmd_inspect_repo, 'pick-folder': cmd_pick_folder,
    }
    handlers[args.command](args)


if __name__ == '__main__':
    main()
