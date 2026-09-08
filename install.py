#!/usr/bin/env python3
"""Install the Plugin Dashboard with timestamped rollback copies; preserve other bar entries."""
from pathlib import Path
import datetime
import json
import shutil
import subprocess

source = Path(__file__).resolve().parent
home = Path.home()
config = home / '.config/omarchy/shell.json'
dest = home / '.config/omarchy/plugins/martythedev-tech.dashboard'
service = home / '.config/systemd/user/dashboard-check.service'
timer = home / '.config/systemd/user/dashboard-check.timer'
stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
backup = home / '.local/state/omarchy/backups' / ('dashboard-' + stamp)
backup.mkdir(parents=True)
shutil.copy2(config, backup / 'shell.json')
if dest.exists(): shutil.copytree(dest, backup / 'plugin')
if service.exists(): shutil.copy2(service, backup / 'dashboard-check.service')
if timer.exists(): shutil.copy2(timer, backup / 'dashboard-check.timer')
dest.mkdir(parents=True, exist_ok=True)
for name in ['manifest.json', 'Panel.qml', 'Model.js', 'dashboard.py', 'README.md']:
    shutil.copy2(source / name, dest / name)
service.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(source / 'dashboard-check.service', service)
shutil.copy2(source / 'dashboard-check.timer', timer)
# Read after copying, minimizing the time between config read and atomic write.
data = json.loads(config.read_text())
layout = data['bar']['layout']
for section in ('left', 'center', 'right'):
    layout[section] = [entry for entry in layout.get(section, [])
                        if (entry.get('id') if isinstance(entry, dict) else entry) != 'martythedev-tech.dashboard']
layout['right'].append({'id': 'martythedev-tech.dashboard'})
tmp = config.with_suffix('.dashboard.tmp')
tmp.write_text(json.dumps(data, indent=2) + '\n')
tmp.replace(config)
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
subprocess.run(['systemctl', '--user', 'enable', '--now', 'dashboard-check.timer'], check=True)
subprocess.run(['python3', str(dest / 'dashboard.py'), 'check'], check=False)
subprocess.run(['omarchy-shell', 'shell', 'rescanPlugins'], check=True)
print('Installed Plugin Dashboard. Backup: ' + str(backup))
