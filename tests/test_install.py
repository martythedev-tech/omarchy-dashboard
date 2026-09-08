import datetime
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


class InstallTests(unittest.TestCase):
    def test_layout_dedupes_and_appends_preserving_other_entries(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            config = home / '.config/omarchy/shell.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'bar': {'layout': {
                'left': ['omarchy.clock', 'martythedev-tech.dashboard'],
                'right': [{'id': 'another.plugin', 'option': 42}]
            }}, 'unrelated': True}))
            with patch.object(Path, 'home', return_value=home), \
                 patch('subprocess.run'), patch('datetime.datetime') as clock, patch('builtins.print'):
                clock.now.return_value = datetime.datetime(2026, 9, 8, 12, 0, 0)
                runpy.run_path(str(Path(__file__).resolve().parents[1] / 'install.py'))

            data = json.loads(config.read_text())
            self.assertTrue(data['unrelated'])
            self.assertEqual(data['bar']['layout'], {
                'left': ['omarchy.clock'], 'center': [],
                'right': [{'id': 'another.plugin', 'option': 42},
                          {'id': 'martythedev-tech.dashboard'}]
            })

    def test_deploys_plugin_files_and_systemd_units(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            config = home / '.config/omarchy/shell.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'bar': {'layout': {'left': [], 'center': [], 'right': []}}}))
            with patch.object(Path, 'home', return_value=home), \
                 patch('subprocess.run'), patch('builtins.print'):
                runpy.run_path(str(Path(__file__).resolve().parents[1] / 'install.py'))

            dest = home / '.config/omarchy/plugins/martythedev-tech.dashboard'
            for name in ('manifest.json', 'Panel.qml', 'Model.js', 'dashboard.py', 'README.md'):
                self.assertTrue((dest / name).exists(), name + ' was not deployed')
            self.assertTrue((home / '.config/systemd/user/dashboard-check.service').exists())
            self.assertTrue((home / '.config/systemd/user/dashboard-check.timer').exists())
            self.assertEqual(len(list((home / '.local/state/omarchy/backups').iterdir())), 1)

    def test_repeat_install_backs_up_the_previous_deployment(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            config = home / '.config/omarchy/shell.json'
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({'bar': {'layout': {'left': [], 'center': [], 'right': []}}}))
            times = [datetime.datetime(2026, 9, 8, 12, 0, 0, i) for i in (1, 2)]
            with patch.object(Path, 'home', return_value=home), patch('subprocess.run'), \
                 patch('datetime.datetime') as clock, patch('builtins.print'):
                clock.now.side_effect = times
                for _ in times:
                    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'install.py'))
            self.assertEqual(len(list((home / '.local/state/omarchy/backups').iterdir())), 2)


if __name__ == '__main__':
    unittest.main()
