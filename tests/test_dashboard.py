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
            self.assertEqual(dash.plugin_update_state(d), ('no-repo', 0))

    def test_fetch_failure_is_unreachable(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            with patch.object(dash, 'run', fake_run({('git', '-C', d, 'fetch'): (1, '', 'no network')})):
                self.assertEqual(dash.plugin_update_state(d), ('unreachable', 0))

    def test_matching_head_is_up_to_date(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', 'HEAD'): (0, 'abc123\n', ''),
                ('git', '-C', d, 'rev-parse', 'FETCH_HEAD'): (0, 'abc123\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.plugin_update_state(d), ('up-to-date', 0))

    def test_behind_and_clean_reports_behind_with_count(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', 'HEAD'): (0, 'abc123\n', ''),
                ('git', '-C', d, 'rev-parse', 'FETCH_HEAD'): (0, 'def456\n', ''),
                ('git', '-C', d, 'rev-list', '--count', 'HEAD..FETCH_HEAD'): (0, '3\n', ''),
                ('git', '-C', d, 'status', '--porcelain'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.plugin_update_state(d), ('behind', 3))

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
                self.assertEqual(dash.plugin_update_state(d), ('dirty', 3))


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
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('up-to-date', 0))

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
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('behind', 1))

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
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('up-to-date', 0))


class DiscoverPluginsTests(unittest.TestCase):
    def test_filters_first_party_and_self(self):
        payload = json.dumps([
            {'id': 'omarchy.clock', 'firstParty': True},
            {'id': 'martythedev-tech.dashboard', 'firstParty': False},
            {'id': 'sslvpn', 'firstParty': False, 'name': 'VPN'},
        ])
        with patch.object(dash, 'run', return_value=(0, payload, '')):
            result = dash.discover_plugins()
        self.assertEqual([p['id'] for p in result], ['sslvpn'])

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
                 patch.object(dash, 'plugin_update_state', side_effect=[('behind', 2), ('up-to-date', 0)]), \
                 patch.object(dash, 'app_update_state', return_value=('dirty', 1)):
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


if __name__ == '__main__':
    unittest.main()
