#!/usr/bin/env python3
"""Dashboard: every third-party/own Omarchy plugin plus tracked local apps,
checked against their real upstream. No bespoke update logic of its own --
every action shells out to the command that already owns it (`omarchy plugin
enable/disable/update` for plugins, each app's own rebuild.sh for apps) and
this script's only job is deciding when an Update button should show and
reporting what that command did.
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
# disk is what's read; edit it there to add or change tracked apps.
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


def ensure_apps_file():
    if APPS_PATH.exists():
        return
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = APPS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(DEFAULT_APPS, indent=2) + '\n')
    tmp.replace(APPS_PATH)


def discover_plugins():
    """Every plugin `omarchy plugin list` reports as not first-party, minus
    this dashboard's own id -- the exact "third-party and your own, not
    Omarchy's built-ins" boundary Omarchy itself already computes."""
    rc, out, _ = run(['omarchy', 'plugin', 'list', '--json'])
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [p for p in data if not p.get('firstParty') and p.get('id') != SELF_ID]


def manifest_version(plugin_id):
    path = PLUGINS_DIR / plugin_id / 'manifest.json'
    try:
        return json.loads(path.read_text()).get('version', '')
    except (OSError, ValueError):
        return ''


def pkg_version(pkg_name):
    rc, out, _ = run(['pacman', '-Q', pkg_name])
    if rc != 0:
        return ''
    parts = out.strip().split()
    return parts[1] if len(parts) > 1 else ''


def plugin_update_state(plugin_dir):
    """Mirrors omarchy-plugin-update's own check exactly (fetch origin's
    HEAD, compare against ours) so "update available" here always agrees
    with what `omarchy plugin update` would actually do."""
    plugin_dir = Path(plugin_dir)
    if not (plugin_dir / '.git').is_dir():
        return 'no-repo', 0
    rc, _, _ = run(['git', '-C', str(plugin_dir), 'fetch', '--quiet', 'origin', 'HEAD'], timeout=20)
    if rc != 0:
        return 'unreachable', 0
    rc1, head, _ = run(['git', '-C', str(plugin_dir), 'rev-parse', 'HEAD'])
    rc2, fetch_head, _ = run(['git', '-C', str(plugin_dir), 'rev-parse', 'FETCH_HEAD'])
    if rc1 != 0 or rc2 != 0:
        return 'unreachable', 0
    if head.strip() == fetch_head.strip():
        return 'up-to-date', 0
    rc3, count, _ = run(['git', '-C', str(plugin_dir), 'rev-list', '--count', 'HEAD..FETCH_HEAD'])
    behind = int(count.strip()) if rc3 == 0 and count.strip().isdigit() else 0
    if behind == 0:
        return 'up-to-date', 0
    rc4, dirty, _ = run(['git', '-C', str(plugin_dir), 'status', '--porcelain'])
    if dirty.strip():
        return 'dirty', behind
    return 'behind', behind


def app_update_state(repo_dir, branch, remote):
    """Mirrors rebuild.sh's own check exactly: is `remote/branch` a commit
    our local `branch` hasn't incorporated yet -- not "is HEAD different from
    origin/HEAD", which would always be true for a checkout like Flea's
    aarch64-local that's meant to diverge from the AUR-tracking branch."""
    repo_dir = Path(repo_dir)
    if not (repo_dir / '.git').is_dir():
        return 'no-repo', 0
    rc, _, _ = run(['git', '-C', str(repo_dir), 'fetch', '--quiet', remote], timeout=20)
    if rc != 0:
        return 'unreachable', 0
    remote_ref = f'{remote}/{branch}'
    rc1, _, _ = run(['git', '-C', str(repo_dir), 'rev-parse', '--verify', branch])
    rc2, _, _ = run(['git', '-C', str(repo_dir), 'rev-parse', '--verify', remote_ref])
    if rc1 != 0 or rc2 != 0:
        return 'unreachable', 0
    rc3, _, _ = run(['git', '-C', str(repo_dir), 'merge-base', '--is-ancestor', remote_ref, branch])
    if rc3 == 0:
        return 'up-to-date', 0
    rc4, count, _ = run(['git', '-C', str(repo_dir), 'rev-list', '--count', f'{branch}..{remote_ref}'])
    behind = int(count.strip()) if rc4 == 0 and count.strip().isdigit() else 0
    return ('behind', behind) if behind > 0 else ('up-to-date', 0)


def check_plugin(p):
    pid = p['id']
    state, behind = plugin_update_state(PLUGINS_DIR / pid)
    return {
        'id': pid, 'kind': 'plugin', 'name': p.get('name', pid),
        'version': manifest_version(pid), 'enabled': bool(p.get('enabled')),
        'canDisable': bool(p.get('canDisable', True)),
        'updateState': state, 'behind': behind,
    }


def check_app(a):
    branch = a.get('branch', 'master')
    remote = a.get('remote', 'origin')
    state, behind = app_update_state(a['repoDir'], branch, remote)
    return {
        'id': a['id'], 'kind': 'app', 'name': a.get('name', a['id']),
        'version': pkg_version(a.get('pkgName', a['id'])),
        'updateState': state, 'behind': behind,
    }


def check_all():
    items = [check_plugin(p) for p in discover_plugins()] + [check_app(a) for a in load_apps()]
    status = {
        'ts': time.time(),
        'items': items,
        'updatable': sum(1 for i in items if i['updateState'] == 'behind'),
    }
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
    # Long build logs (Flea's cargo test output, say) aren't useful in full to
    # the UI -- the tail carries the actual result or error.
    message = (out or err).strip()[-4000:]
    print(json.dumps({'ok': rc == 0, 'message': message}))
    check_all()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check')
    p_enable = sub.add_parser('enable')
    p_enable.add_argument('id')
    p_disable = sub.add_parser('disable')
    p_disable.add_argument('id')
    p_update = sub.add_parser('update')
    p_update.add_argument('id')
    args = parser.parse_args()
    {'check': cmd_check, 'enable': cmd_enable, 'disable': cmd_disable, 'update': cmd_update}[args.command](args)


if __name__ == '__main__':
    main()
