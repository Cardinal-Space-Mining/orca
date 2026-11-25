'''
Orchestrator settings that persist between runs, in
~/.config/orchestrator/settings.json.
'''

from __future__ import annotations

import copy
import json
import os


SETTINGS_PATH = os.path.expanduser('~/.config/orchestrator/settings.json')

DEFAULTS = {
    # Less orchestrator traffic on the robot's link: slower status, no live
    # output from other machines (see docs on the Network page).
    'competition_mode': False,
    'budget': {
        # Lunabotics guidebook 14.1.19: average comm-link use <= 4,000 Kbps;
        # 14.1.18: each NASA situational camera counts 500 Kbps.
        'limit_kbps': 4000,
        'camera_kbps': 500,
        'num_cameras': 0,
        # The robot's link to the router: a machine and interface whose
        # counters measure everything on the comm link.
        'interface': None,  # {"machine_id": ..., "name": ...}
        # Where to read net_adapter's own stats (a machine running an
        # endpoint with ROS), and whether that launch used run.sh --local.
        'monitor_machine_id': None,
        'monitor_local_dds': True,
    },
    # Last internet-sharing choices per machine id (downstream port,
    # subnet...), offered again next time.
    'sharing': {},
}


def mergeDefaults(saved: dict, defaults: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = mergeDefaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def loadSettings() -> dict:
    try:
        with open(SETTINGS_PATH) as file:
            return mergeDefaults(json.load(file), DEFAULTS)
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULTS)


def saveSettings(settings: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    temporary_path = SETTINGS_PATH + '.tmp'
    with open(temporary_path, 'w') as file:
        json.dump(settings, file, indent=4)
        file.write('\n')
    os.replace(temporary_path, SETTINGS_PATH)
