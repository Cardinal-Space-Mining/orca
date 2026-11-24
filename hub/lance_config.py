'''
The LANCE launch configuration (lance/config), read with launch-utils' own
code so the hub resolves presets exactly the way `run.sh` will.

launch-utils' common.py imports ROS (ament_index_python) and netifaces at
module level (FINDINGS.md L4). The hub only needs its JSON loading, so when
those modules aren't installed it loads common.py against small stand-ins.

Overrides the CLI can't express (a single value, such as the peer's IP
address) go through an overlay file that the hub writes next to launch.json
in the target's install directory:

    {
        "pragma:enable_preproc": true,
        "pragma:import": ["launch.json"],
        "redux": {
            "hub_robot": {"pragma:derived": "robot", "client_hostname": "..."}
        }
    }

and the launch gets `json_path:=<overlay> redux:=hub_robot`. launch-utils
merges imported files under the overlay's own keys, so the new preset
inherits everything else from the preset that would have been chosen.
'''

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import sys
import types
from typing import Optional


CONFIG_RELATIVE_PATH = os.path.join('src', 'lance', 'config', 'launch.json')
LAUNCH_UTILS_RELATIVE_PATH = os.path.join(
    'src', 'launch-utils', 'src', 'launch_utils')
# Where `colcon build --symlink-install` puts the config. It's a real
# directory of per-file symlinks, so the overlay can go there without
# touching src/.
INSTALLED_CONFIG_DIRECTORY = os.path.join(
    'install', 'lance', 'share', 'lance', 'config')

CONFIGURED_PATTERN = re.compile(
    r"Configured action '([^']+)' with preset '([^']*)'")
DERIVED_TAG = 'pragma:derived'
DEFAULT_TAG = 'pragma:default'
OVERRIDES_TAG = 'pragma:action_overrides'
DESCRIPTION_TAG = 'pragma:description'
CONSTANTS_TAG = 'pragma:constants'


class ConfigError(Exception):
    pass



# --- Loading launch-utils -----------------------------------------------------



def installStandIns():
    '''Stand-ins for the ROS-only modules common.py imports, used only when
    the real ones are missing.'''
    try:
        import ament_index_python.packages  # noqa: F401
    except ImportError:
        ament = types.ModuleType('ament_index_python')
        packages = types.ModuleType('ament_index_python.packages')

        def getPackageShareDirectory(package):
            raise LookupError('ROS is not available to the hub: ' + package)

        packages.get_package_share_directory = getPackageShareDirectory
        ament.packages = packages
        sys.modules['ament_index_python'] = ament
        sys.modules['ament_index_python.packages'] = packages
    try:
        import netifaces  # noqa: F401
    except ImportError:
        sys.modules['netifaces'] = types.ModuleType('netifaces')


def loadModule(name: str, path: str):
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class LaunchUtils:
    '''launch-utils' preprocess.py and common.py from one workspace.'''

    def __init__(self, workspace_root: str):
        directory = os.path.join(workspace_root, LAUNCH_UTILS_RELATIVE_PATH)
        if not os.path.isdir(directory):
            raise ConfigError('launch-utils not found at ' + directory)
        installStandIns()
        self.preprocess = loadModule(
            'csm_hub_launch_utils_preprocess',
            os.path.join(directory, 'preprocess.py'))
        self.common = loadModule(
            'csm_hub_launch_utils_common', os.path.join(directory, 'common.py'))



# --- Config -------------------------------------------------------------------



class LanceConfig:
    '''The merged launch config of one workspace, reloaded when any of its
    files change.'''

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root
        self.config_path = os.path.join(workspace_root, CONFIG_RELATIVE_PATH)
        self.launch_utils = LaunchUtils(workspace_root)
        self.merged = None
        self.loaded_signature = None

    # --- Loading ---

    def signature(self):
        directory = os.path.dirname(self.config_path)
        stamps = []
        for root, _, names in os.walk(directory):
            for name in names:
                if name.endswith('.json'):
                    path = os.path.join(root, name)
                    stamps.append((path, os.stat(path).st_mtime))
        return tuple(sorted(stamps))

    def load(self) -> dict:
        signature = self.signature()
        if self.merged is None or signature != self.loaded_signature:
            common = self.launch_utils.common
            try:
                data, path = common.try_load_json(self.config_path)
                self.merged = common.resolve_json_imports(data, path)
            except RuntimeError as error:
                raise ConfigError(str(error))
            self.loaded_signature = signature
        return self.merged

    # --- Browsing ---

    def actions(self) -> list:
        '''Every action with its presets, for the config browser. "Setups"
        are actions whose presets select other actions' presets (robot,
        client, gz_full...); "components" configure one thing.'''
        merged = self.load()
        actions = []
        for name, block in merged.items():
            if name.startswith('pragma:') or not isinstance(block, dict):
                continue
            presets = []
            for preset_name, preset in block.items():
                if preset_name.startswith('pragma:') \
                        or not isinstance(preset, dict):
                    continue
                presets.append({
                    'name': preset_name,
                    'derived': preset.get(DERIVED_TAG),
                    'description': preset.get(DESCRIPTION_TAG),
                    'action_overrides': preset.get(OVERRIDES_TAG),
                    'raw': preset,
                })
            actions.append({
                'name': name,
                'default': block.get(DEFAULT_TAG),
                'description': block.get(DESCRIPTION_TAG),
                'is_setup': any(preset['action_overrides'] is not None
                                for preset in presets),
                'presets': presets,
            })
        return actions

    def resolvePreset(self, action: str, preset: str) -> dict:
        '''One preset after inheritance and constant substitution.'''
        merged = self.load()
        block = merged.get(action)
        if not isinstance(block, dict) or preset not in block:
            raise ConfigError('no preset %s:=%s' % (action, preset))
        preprocess = self.launch_utils.preprocess
        resolved = preprocess.resolve_preset(block, preset)
        preprocess.resolve_constants(
            resolved, copy.deepcopy(merged.get(CONSTANTS_TAG, {})))
        return resolved

    # --- Resolving a launch ---

    def resolve(self, arguments: dict, merged: Optional[dict] = None) -> dict:
        '''Run launch-utils' preprocessor on `key:=value` arguments.
        Returns {"selected": {action: preset}, "config": resolved}.'''
        merged = copy.deepcopy(merged if merged is not None else self.load())
        merged['pragma:enable_preproc'] = True
        log = io.StringIO()
        try:
            with contextlib.redirect_stdout(log):
                resolved = self.launch_utils.preprocess.preprocess_launch_json(
                    merged, dict(arguments))
        except ValueError as error:
            raise ConfigError(str(error))
        selected = dict(CONFIGURED_PATTERN.findall(log.getvalue()))
        return {'selected': selected, 'config': resolved}

    def overlayFor(self, arguments: dict, settings: dict, tag: str):
        '''Build the overlay that applies `settings` ({action: {key: value}})
        on top of what `arguments` select. Returns (overlay, extra arguments,
        resolution with the overlay applied).'''
        base = self.resolve(arguments)
        overlay = {'pragma:enable_preproc': True,
                   'pragma:import': ['launch.json']}
        extra_arguments = {}
        for action, values in settings.items():
            parent = base['selected'].get(action)
            if parent is None:
                raise ConfigError(
                    "can't set %s values: these presets don't enable the "
                    "'%s' action" % (action, action))
            derived_name = 'hub_' + tag
            preset = {DERIVED_TAG: parent}
            preset.update(values)
            overlay[action] = {derived_name: preset}
            extra_arguments[action] = derived_name

        # Resolve again with the overlay merged in, as launch-utils will.
        merged = copy.deepcopy(self.load())
        for action, presets in overlay.items():
            if not action.startswith('pragma:'):
                merged[action].update(copy.deepcopy(presets))
        final = self.resolve(dict(arguments, **extra_arguments), merged)
        return overlay, extra_arguments, final


def overlayJson(overlay: dict) -> str:
    return json.dumps(overlay, indent=4) + '\n'
