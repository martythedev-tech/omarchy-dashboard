import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as dash


def fake_run(table):
    """Returns a `run()` replacement driven by a {command_prefix_tuple: (rc, out, err)}
    table, matched by the longest prefix of the actual argv -- lets a test say
    "any git -C <dir> fetch" without hardcoding the temp dir path."""
    def _run(args, cwd=None, timeout=20):
        best = None
        for prefix, result in table.items():
            if tuple(args[:len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is None:
            raise AssertionError('unexpected command: ' + ' '.join(args))
        return table[best]
    return _run


class PluginUpdateStateTests(unittest.TestCase):
    def test_no_git_dir_is_no_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dash.plugin_update_state(d), ('no-repo', 0, ''))

    def test_fetch_failure_is_unreachable_with_reason(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            with patch.object(dash, 'run', fake_run({('git', '-C', d, 'fetch'): (1, '', 'no network')})):
                self.assertEqual(dash.plugin_update_state(d), ('unreachable', 0, 'no network'))

    def test_matching_head_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', 'HEAD'): (0, 'abc123\n', ''),
                ('git', '-C', d, 'rev-parse', 'FETCH_HEAD'): (0, 'abc123\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.plugin_update_state(d), ('up-to-date', 0, ''))

    def test_behind_and_clean_reports_behind_with_count(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', 'HEAD'): (0, 'abc123\n', ''),
                ('git', '-C', d, 'rev-parse', 'FETCH_HEAD'): (0, 'def456\n', ''),
                ('git', '-C', d, 'rev-list', '--count', 'HEAD..FETCH_HEAD'): (0, '3\n', ''),
                ('git', '-C', d, 'status', '--porcelain'): (0, '', ''),
                ('git', '-C', d, 'rev-list', '--count', 'FETCH_HEAD..HEAD'): (0, '0\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.plugin_update_state(d), ('behind', 3, ''))

    def test_behind_but_dirty_refuses_the_update_button(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', 'HEAD'): (0, 'abc123\n', ''),
                ('git', '-C', d, 'rev-parse', 'FETCH_HEAD'): (0, 'def456\n', ''),
                ('git', '-C', d, 'rev-list', '--count', 'HEAD..FETCH_HEAD'): (0, '3\n', ''),
                ('git', '-C', d, 'status', '--porcelain'): (0, ' M some/file.qml\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.plugin_update_state(d), ('dirty', 3, ''))

    def test_diverged_when_clean_but_locally_ahead(self):
        # Clean working tree, but HEAD carries a commit FETCH_HEAD doesn't --
        # `omarchy plugin update`'s fast-forward-only merge would fail on this
        # even though nothing is "dirty", so it must not be reported as plain
        # 'behind' (which would offer an Update button that's doomed to fail).
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', 'HEAD'): (0, 'abc123\n', ''),
                ('git', '-C', d, 'rev-parse', 'FETCH_HEAD'): (0, 'def456\n', ''),
                ('git', '-C', d, 'rev-list', '--count', 'HEAD..FETCH_HEAD'): (0, '2\n', ''),
                ('git', '-C', d, 'status', '--porcelain'): (0, '', ''),
                ('git', '-C', d, 'rev-list', '--count', 'FETCH_HEAD..HEAD'): (0, '1\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.plugin_update_state(d), ('diverged', 2, ''))


class AppUpdateStateTests(unittest.TestCase):
    def test_ancestor_means_up_to_date(self):
        # Mirrors rebuild.sh: origin/branch already an ancestor of branch.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'origin/master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'master'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('up-to-date', 0, ''))

    def test_not_ancestor_counts_behind(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'origin/master'): (0, 'def\n', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'master'): (1, '', ''),
                ('git', '-C', d, 'rev-list', '--count', 'master..origin/master'): (0, '1\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('behind', 1, ''))

    def test_local_branch_ahead_of_aur_is_not_reported_as_behind(self):
        # This is the exact Flea shape: aarch64-local (what's checked out) is
        # meant to diverge ahead of origin/master, and must never be reported
        # as "behind" because of that divergence alone.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'origin/master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'master'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                # Note: checked against 'master', never 'aarch64-local'.
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('up-to-date', 0, ''))

    def test_app_fetch_failure_is_unreachable_with_reason(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            with patch.object(dash, 'run', fake_run({('git', '-C', d, 'fetch'): (1, '', 'ssh: connect timed out')})):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('unreachable', 0, 'ssh: connect timed out'))


class DiscoverPluginsTests(unittest.TestCase):
    def test_filters_first_party_but_includes_self(self):
        # SELF_ID is no longer excluded here -- the dashboard checks and can
        # update itself; it's cmd_disable/cmd_remove that separately refuse
        # to act on SELF_ID, since those two would pull the shell out from
        # under its own running widget.
        payload = json.dumps([
            {'id': 'omarchy.clock', 'firstParty': True},
            {'id': 'martythedev-tech.dashboard', 'firstParty': False},
            {'id': 'sslvpn', 'firstParty': False, 'name': 'VPN'},
        ])
        with patch.object(dash, 'run', return_value=(0, payload, '')):
            result = dash.discover_plugins()
        self.assertEqual(sorted(p['id'] for p in result), ['martythedev-tech.dashboard', 'sslvpn'])

    def test_non_zero_exit_yields_empty_list(self):
        with patch.object(dash, 'run', return_value=(1, '', 'boom')):
            self.assertEqual(dash.discover_plugins(), [])

    def test_garbage_json_yields_empty_list(self):
        with patch.object(dash, 'run', return_value=(0, 'not json', '')):
            self.assertEqual(dash.discover_plugins(), [])


class CheckAllTests(unittest.TestCase):
    def test_aggregates_and_counts_only_behind_as_updatable(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            with patch.object(Path, 'home', return_value=home), \
                 patch.object(dash, 'discover_plugins', return_value=[
                     {'id': 'a.plugin', 'name': 'A', 'enabled': True, 'canDisable': True},
                     {'id': 'b.plugin', 'name': 'B', 'enabled': False, 'canDisable': True},
                 ]), \
                 patch.object(dash, 'load_apps', return_value=[
                     {'id': 'flea', 'name': 'Flea', 'repoDir': '/x', 'pkgName': 'flea', 'branch': 'master', 'remote': 'origin'},
                 ]), \
                 patch.object(dash, 'manifest_version', return_value='1.0.0'), \
                 patch.object(dash, 'pkg_version', return_value='0.1.6'), \
                 patch.object(dash, 'plugin_update_state', side_effect=[('behind', 2, ''), ('up-to-date', 0, '')]), \
                 patch.object(dash, 'app_update_state', return_value=('dirty', 1, '')), \
                 patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
                dash.PLUGINS_DIR = home / '.config/omarchy/plugins'
                dash.STATE = home / '.local/state/omarchy/plugins/martythedev-tech.dashboard'
                dash.STATUS_PATH = dash.STATE / 'status.json'
                status = dash.check_all()

            self.assertEqual(status['updatable'], 1)
            ids = {i['id']: i for i in status['items']}
            self.assertEqual(ids['a.plugin']['updateState'], 'behind')
            self.assertEqual(ids['b.plugin']['updateState'], 'up-to-date')
            self.assertEqual(ids['flea']['updateState'], 'dirty')
            self.assertTrue(dash.STATUS_PATH.exists())
            # No prior status.json existed, so 'a.plugin' becoming 'behind' is
            # new -- notify-send should have fired once.
            self.assertEqual(run_mock.call_count, 1)
            self.assertEqual(run_mock.call_args.args[0][0], 'notify-send')


class NotifyNewUpdatesTests(unittest.TestCase):
    def test_fires_only_for_newly_behind_ids(self):
        items = [
            {'id': 'a', 'name': 'A', 'updateState': 'behind'},
            {'id': 'b', 'name': 'B', 'updateState': 'behind'},
            {'id': 'c', 'name': 'C', 'updateState': 'dirty'},
        ]
        with patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
            dash.notify_new_updates(items, old_behind_ids={'a'})
        run_mock.assert_called_once()
        args = run_mock.call_args.args[0]
        self.assertEqual(args[0], 'notify-send')
        self.assertIn('B', args[-1])
        self.assertNotIn('A', args[-1])

    def test_no_new_ids_means_no_notification(self):
        items = [{'id': 'a', 'name': 'A', 'updateState': 'behind'}]
        with patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
            dash.notify_new_updates(items, old_behind_ids={'a'})
        run_mock.assert_not_called()

    def test_dirty_and_diverged_never_notify(self):
        items = [{'id': 'a', 'name': 'A', 'updateState': 'dirty'}, {'id': 'b', 'name': 'B', 'updateState': 'diverged'}]
        with patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
            dash.notify_new_updates(items, old_behind_ids=set())
        run_mock.assert_not_called()


class CmdUpdateDispatchTests(unittest.TestCase):
    def test_known_app_id_runs_its_own_update_command_in_its_repo_dir(self):
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[
                 {'id': 'howdy', 'name': 'Howdy', 'repoDir': '/home/x/Projects/howdy',
                  'updateCmd': ['./rebuild.sh', '--install']},
             ]), \
             patch.object(dash, 'run', return_value=(0, 'built', '')) as run_mock, \
             patch.object(dash, 'check_all'), patch('builtins.print'):
            args = type('A', (), {'id': 'howdy'})()
            dash.cmd_update(args)
        run_mock.assert_called_once_with(['./rebuild.sh', '--install'], cwd='/home/x/Projects/howdy', timeout=900)

    def test_unknown_id_falls_through_to_omarchy_plugin_update(self):
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[]), \
             patch.object(dash, 'run', return_value=(0, 'Updated sslvpn.', '')) as run_mock, \
             patch.object(dash, 'check_all'), patch('builtins.print'):
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_update(args)
        run_mock.assert_called_once_with(['omarchy', 'plugin', 'update', 'sslvpn', '--yes'], timeout=180)

    def test_self_id_is_a_normal_update_not_special_cased(self):
        # Self-update is intentional: only disable/remove refuse SELF_ID.
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[]), \
             patch.object(dash, 'run', return_value=(0, 'Updated.', '')) as run_mock, \
             patch.object(dash, 'check_all'), patch('builtins.print'):
            args = type('A', (), {'id': dash.SELF_ID})()
            dash.cmd_update(args)
        run_mock.assert_called_once_with(['omarchy', 'plugin', 'update', dash.SELF_ID, '--yes'], timeout=180)


class CmdDisableTests(unittest.TestCase):
    def test_refuses_to_disable_self(self):
        with patch.object(dash, 'run') as run_mock, patch('builtins.print') as print_mock:
            args = type('A', (), {'id': dash.SELF_ID})()
            dash.cmd_disable(args)
        run_mock.assert_not_called()
        payload = json.loads(print_mock.call_args.args[0])
        self.assertFalse(payload['ok'])

    def test_disables_other_plugins_normally(self):
        with patch.object(dash, 'run', return_value=(0, 'Disabled.', '')) as run_mock, patch('builtins.print'):
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_disable(args)
        run_mock.assert_called_once_with(['omarchy', 'plugin', 'disable', 'sslvpn'])


class CmdRemoveTests(unittest.TestCase):
    def test_refuses_to_remove_self(self):
        with patch.object(dash, 'run') as run_mock, patch.object(dash, 'check_all'), patch('builtins.print') as print_mock:
            args = type('A', (), {'id': dash.SELF_ID})()
            dash.cmd_remove(args)
        run_mock.assert_not_called()
        payload = json.loads(print_mock.call_args.args[0])
        self.assertFalse(payload['ok'])

    def test_removes_other_plugins_via_omarchy_cli(self):
        with patch.object(dash, 'run', return_value=(0, 'Removed sslvpn.', '')) as run_mock, \
             patch.object(dash, 'check_all'), patch('builtins.print'):
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_remove(args)
        run_mock.assert_called_once_with(['omarchy', 'plugin', 'remove', 'sslvpn', '--yes'], timeout=60)


class CmdDiffTests(unittest.TestCase):
    def test_plugin_diff_uses_head_and_fetch_head(self):
        with patch.object(dash, 'ensure_apps_file'), patch.object(dash, 'load_apps', return_value=[]), \
             patch.object(dash, 'run', return_value=(0, 'diff --git a b\n', '')) as run_mock, \
             patch('builtins.print') as print_mock:
            dash.PLUGINS_DIR = Path('/home/x/.config/omarchy/plugins')
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_diff(args)
        run_mock.assert_called_once_with(
            ['git', '-C', '/home/x/.config/omarchy/plugins/sslvpn', 'diff', 'HEAD', 'FETCH_HEAD'], timeout=20)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertTrue(payload['ok'])
        self.assertIn('diff --git', payload['diff'])

    def test_app_diff_uses_branch_and_remote_ref(self):
        with patch.object(dash, 'ensure_apps_file'), patch.object(dash, 'load_apps', return_value=[
                 {'id': 'howdy', 'repoDir': '/home/x/Projects/howdy', 'branch': 'master', 'remote': 'origin'}]), \
             patch.object(dash, 'run', return_value=(0, 'diff --git a b\n', '')) as run_mock, \
             patch('builtins.print'):
            args = type('A', (), {'id': 'howdy'})()
            dash.cmd_diff(args)
        run_mock.assert_called_once_with(
            ['git', '-C', '/home/x/Projects/howdy', 'diff', 'master', 'origin/master'], timeout=20)

    def test_diff_failure_reports_error_message(self):
        with patch.object(dash, 'ensure_apps_file'), patch.object(dash, 'load_apps', return_value=[]), \
             patch.object(dash, 'run', return_value=(1, '', 'fatal: bad revision')), \
             patch('builtins.print') as print_mock:
            dash.PLUGINS_DIR = Path('/home/x/.config/omarchy/plugins')
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_diff(args)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertFalse(payload['ok'])
        self.assertIn('bad revision', payload['message'])


class AddAndRemoveAppTests(unittest.TestCase):
    def test_add_app_requires_name_id_and_repo_dir(self):
        with patch('builtins.print') as print_mock:
            args = type('A', (), {'json': json.dumps({'name': 'Only Name'})})()
            dash.cmd_add_app(args)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertFalse(payload['ok'])

    def test_add_app_rejects_non_git_repo_dir(self):
        with tempfile.TemporaryDirectory() as d:
            with patch('builtins.print') as print_mock:
                args = type('A', (), {'json': json.dumps({'name': 'X', 'id': 'x', 'repoDir': d})})()
                dash.cmd_add_app(args)
            payload = json.loads(print_mock.call_args.args[0])
            self.assertFalse(payload['ok'])
            self.assertIn('not a git repository', payload['message'])

    def test_add_app_writes_entry_and_defaults_optional_fields(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            saved = {}
            with patch.object(dash, 'ensure_apps_file'), patch.object(dash, 'load_apps', return_value=[]), \
                 patch.object(dash, 'save_apps', side_effect=lambda apps: saved.setdefault('apps', apps)), \
                 patch.object(dash, 'check_all'), patch('builtins.print'):
                args = type('A', (), {'json': json.dumps({'name': 'X', 'id': 'x', 'repoDir': d})})()
                dash.cmd_add_app(args)
            self.assertEqual(len(saved['apps']), 1)
            entry = saved['apps'][0]
            self.assertEqual(entry['id'], 'x')
            self.assertEqual(entry['pkgName'], 'x')
            self.assertEqual(entry['branch'], 'master')
            self.assertEqual(entry['remote'], 'origin')
            self.assertEqual(entry['updateCmd'], ['./rebuild.sh', '--install'])

    def test_add_app_splits_string_update_cmd(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            saved = {}
            with patch.object(dash, 'ensure_apps_file'), patch.object(dash, 'load_apps', return_value=[]), \
                 patch.object(dash, 'save_apps', side_effect=lambda apps: saved.setdefault('apps', apps)), \
                 patch.object(dash, 'check_all'), patch('builtins.print'):
                args = type('A', (), {'json': json.dumps({'name': 'X', 'id': 'x', 'repoDir': d, 'updateCmd': './go.sh --now'})})()
                dash.cmd_add_app(args)
            self.assertEqual(saved['apps'][0]['updateCmd'], ['./go.sh', '--now'])

    def test_add_app_replaces_existing_entry_with_same_id(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            saved = {}
            with patch.object(dash, 'ensure_apps_file'), \
                 patch.object(dash, 'load_apps', return_value=[{'id': 'x', 'name': 'Old'}]), \
                 patch.object(dash, 'save_apps', side_effect=lambda apps: saved.setdefault('apps', apps)), \
                 patch.object(dash, 'check_all'), patch('builtins.print'):
                args = type('A', (), {'json': json.dumps({'name': 'New', 'id': 'x', 'repoDir': d})})()
                dash.cmd_add_app(args)
            self.assertEqual(len(saved['apps']), 1)
            self.assertEqual(saved['apps'][0]['name'], 'New')

    def test_remove_app_drops_matching_id(self):
        saved = {}
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[{'id': 'flea'}, {'id': 'howdy'}]), \
             patch.object(dash, 'save_apps', side_effect=lambda apps: saved.setdefault('apps', apps)), \
             patch.object(dash, 'check_all'), patch('builtins.print') as print_mock:
            args = type('A', (), {'id': 'flea'})()
            dash.cmd_remove_app(args)
        self.assertEqual([a['id'] for a in saved['apps']], ['howdy'])
        payload = json.loads(print_mock.call_args.args[0])
        self.assertTrue(payload['ok'])

    def test_remove_app_reports_when_id_not_found(self):
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[{'id': 'howdy'}]), \
             patch.object(dash, 'save_apps') as save_mock, \
             patch.object(dash, 'check_all'), patch('builtins.print') as print_mock:
            args = type('A', (), {'id': 'nonexistent'})()
            dash.cmd_remove_app(args)
        save_mock.assert_not_called()
        payload = json.loads(print_mock.call_args.args[0])
        self.assertFalse(payload['ok'])


if __name__ == '__main__':
    unittest.main()
