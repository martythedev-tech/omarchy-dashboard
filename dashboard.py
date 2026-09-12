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
"""
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

SELF_ID = 'martythedev-tech.dashboard'
PLUGINS_DIR = Path.home() / '.config/omarchy/plugins'
STATE = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'omarchy/plugins' / SELF_ID
STATUS_PATH = STATE / 'status.json'
APPS_PATH = STATE / 'apps.json'

# Seeded into APPS_PATH the first time it's missing; from then on the file on
# disk is what's read; edit it there (or via the "+ Add app" form) to add or
# change tracked apps.
DEFAULT_APPS = [
    {'id': 'flea', 'name': 'Flea', 'repoDir': str(Path.home() / 'Projects/flea'), 'pkgName': 'flea',
     'branch': 'master', 'remote': 'origin', 'updateCmd': ['./rebuild.sh', '--install']},
    {'id': 'howdy', 'name': 'Howdy', 'repoDir': str(Path.home() / 'Projects/howdy'), 'pkgName': 'howdy',
     'branch': 'master', 'remote': 'origin', 'updateCmd': ['./rebuild.sh', '--install']},
]


def run(args, cwd=None, timeout=20):
    """(returncode, stdout, stderr); never raises -- a missing binary or a
    timeout is just another kind of failure the caller already has to handle."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, check=False)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


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


def pkg_version(pkg_name):
    rc, out, _ = run(['pacman', '-Q', pkg_name])
    if rc != 0:
        return ''
    parts = out.strip().split()
    return parts[1] if len(parts) > 1 else ''


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
        return 'dirty', behind, ''
    rc5, ahead_count, _ = run(['git', '-C', str(plugin_dir), 'rev-list', '--count', 'FETCH_HEAD..HEAD'])
    ahead = int(ahead_count.strip()) if rc5 == 0 and ahead_count.strip().isdigit() else 0
    if ahead > 0:
        return 'diverged', behind, ''
    return 'behind', behind, ''


def app_update_state(repo_dir, branch, remote):
    """Mirrors rebuild.sh's own check exactly: is `remote/branch` a commit
    our local `branch` hasn't incorporated yet -- not "is HEAD different from
    origin/HEAD", which would always be true for a checkout like Flea's
    aarch64-local that's meant to diverge from the AUR-tracking branch."""
    repo_dir = Path(repo_dir)
    if not (repo_dir / '.git').is_dir():
        return 'no-repo', 0, ''
    rc, _, err = run(['git', '-C', str(repo_dir), 'fetch', '--quiet', remote], timeout=20)
    if rc != 0:
        return 'unreachable', 0, err.strip()[-500:]
    remote_ref = f'{remote}/{branch}'
    rc1, _, _ = run(['git', '-C', str(repo_dir), 'rev-parse', '--verify', branch])
    rc2, _, _ = run(['git', '-C', str(repo_dir), 'rev-parse', '--verify', remote_ref])
    if rc1 != 0 or rc2 != 0:
        return 'unreachable', 0, f'could not resolve {branch}/{remote_ref}'
    rc3, _, _ = run(['git', '-C', str(repo_dir), 'merge-base', '--is-ancestor', remote_ref, branch])
    if rc3 == 0:
        return 'up-to-date', 0, ''
    rc4, count, _ = run(['git', '-C', str(repo_dir), 'rev-list', '--count', f'{branch}..{remote_ref}'])
    behind = int(count.strip()) if rc4 == 0 and count.strip().isdigit() else 0
    return ('behind', behind, '') if behind > 0 else ('up-to-date', 0, '')


def check_plugin(p):
    pid = p['id']
    state, behind, reason = plugin_update_state(PLUGINS_DIR / pid)
    return {
        'id': pid, 'kind': 'plugin', 'name': p.get('name', pid),
        'version': manifest_version(pid), 'enabled': bool(p.get('enabled')),
        'canDisable': bool(p.get('canDisable', True)),
        'updateState': state, 'behind': behind, 'reason': reason,
    }


def check_app(a):
    branch = a.get('branch', 'master')
    remote = a.get('remote', 'origin')
    state, behind, reason = app_update_state(a['repoDir'], branch, remote)
    return {
        'id': a['id'], 'kind': 'app', 'name': a.get('name', a['id']),
        'version': pkg_version(a.get('pkgName', a['id'])),
        'updateState': state, 'behind': behind, 'reason': reason,
    }


def _previously_behind_ids():
    try:
        data = json.loads(STATUS_PATH.read_text())
        return {i['id'] for i in data.get('items', []) if i.get('updateState') == 'behind'}
    except (OSError, ValueError):
        return set()


def notify_new_updates(items, old_behind_ids):
    """A toast only for items that just *became* one-click-updatable since
    the last check -- not a repeat every cycle for something that's been
    sitting there, and not for 'dirty'/'diverged', which need a person
    regardless of how many checks go by."""
    newly = [i for i in items if i['updateState'] == 'behind' and i['id'] not in old_behind_ids]
    if not newly:
        return
    names = ', '.join(i['name'] for i in newly[:5])
    if len(newly) > 5:
        names += f' and {len(newly) - 5} more'
    title = 'Update available' if len(newly) == 1 else f'{len(newly)} updates available'
    run(['notify-send', '--app-name=Plugin Dashboard', '--icon=system-software-update',
         title, names], timeout=5)


def check_all():
    old_behind_ids = _previously_behind_ids()
    items = [check_plugin(p) for p in discover_plugins()] + [check_app(a) for a in load_apps()]
    status = {
        'ts': time.time(),
        'items': items,
        'updatable': sum(1 for i in items if i['updateState'] == 'behind'),
    }
    notify_new_updates(items, old_behind_ids)
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(status, indent=2))
    tmp.replace(STATUS_PATH)
    return status


def cmd_check(_args):
    ensure_apps_file()
    print(json.dumps(check_all()))


def cmd_enable(args):
    rc, out, err = run(['omarchy', 'plugin', 'enable', args.id])
    print(json.dumps({'ok': rc == 0, 'message': (out or err).strip()}))


def cmd_disable(args):
    if args.id == SELF_ID:
        print(json.dumps({'ok': False, 'message': 'Refusing to disable the Dashboard from within itself -- '
                                                    'use `omarchy plugin disable` from a terminal instead.'}))
        return
    rc, out, err = run(['omarchy', 'plugin', 'disable', args.id])
    print(json.dumps({'ok': rc == 0, 'message': (out or err).strip()}))


def cmd_update(args):
    ensure_apps_file()
    apps = {a['id']: a for a in load_apps()}
    if args.id in apps:
        a = apps[args.id]
        rc, out, err = run(a['updateCmd'], cwd=a['repoDir'], timeout=900)
    else:
        rc, out, err = run(['omarchy', 'plugin', 'update', args.id, '--yes'], timeout=180)
        plugin_dir = PLUGINS_DIR / args.id
        if rc == 0 and has_ensure_bootstrap(plugin_dir):
            erc, eout, eerr = run_ensure_bootstrap(plugin_dir)
            if erc != 0:
                rc = erc
                err = (err + '\n' if err.strip() else '') + \
                    'Update landed but the plugin did not come up cleanly:\n' + (eerr or eout)
            elif eout.strip():
                out = (out + '\n' if out.strip() else '') + eout
    # Long build logs (Flea's cargo test output, say) aren't useful in full to
    # the UI -- the tail carries the actual result or error.
    message = (out or err).strip()[-4000:]
    print(json.dumps({'ok': rc == 0, 'message': message}))
    check_all()


def cmd_remove(args):
    if args.id == SELF_ID:
        print(json.dumps({'ok': False, 'message': 'Refusing to remove the Dashboard from within itself -- '
                                                    'use `omarchy plugin remove` from a terminal instead.'}))
        return
    rc, out, err = run(['omarchy', 'plugin', 'remove', args.id, '--yes'], timeout=60)
    print(json.dumps({'ok': rc == 0, 'message': (out or err).strip()}))
    check_all()


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
        rc, out, err = run(['git', '-C', a['repoDir'], 'diff', branch, remote_ref], timeout=20)
    else:
        rc, out, err = run(['git', '-C', str(PLUGINS_DIR / args.id), 'diff', 'HEAD', 'FETCH_HEAD'], timeout=20)
    if rc != 0:
        print(json.dumps({'ok': False, 'message': (err or 'Could not compute diff.').strip()[-2000:]}))
        return
    print(json.dumps({'ok': True, 'diff': out[-20000:] or '(no textual diff)'}))


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
    update_cmd = fields.get('updateCmd') or ['./rebuild.sh', '--install']
    if isinstance(update_cmd, str):
        update_cmd = update_cmd.split()
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
    sub.add_parser('check')
    for name in ('enable', 'disable', 'update', 'remove', 'diff', 'remove-app'):
        p = sub.add_parser(name)
        p.add_argument('id')
    p_add_app = sub.add_parser('add-app')
    p_add_app.add_argument('json')
    args = parser.parse_args()
    handlers = {
        'check': cmd_check, 'enable': cmd_enable, 'disable': cmd_disable, 'update': cmd_update,
        'remove': cmd_remove, 'diff': cmd_diff, 'add-app': cmd_add_app, 'remove-app': cmd_remove_app,
    }
    handlers[args.command](args)


if __name__ == '__main__':
    main()
