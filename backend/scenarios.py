'''
Scenarios: named setups that start processes on one or more machines.

One JSON file per scenario in <workspace>/src/project/orchestrator/scenarios/
(the file name is its id):

    {
        "label": "Competition run",
        "description": "What it does and when to use it.",
        "order": 10,                      // position in the list
        "roles": {                        // filled with machines at launch
            "robot": {"label": "Robot computer", "default": "robot-vm"},
            "client": {"label": "Driver station", "default": "local"}
        },
        "options": {
            "arena": {"label": "Arena", "default": "ksc",
                      "choices": {"ksc": "KSC", "ucf_left": "UCF left"}},
            "bag": {"label": "Bag", "type": "text", "default": ""}
        },
        "processes": [
            {
                "key": "robot",
                "label": "Robot stack",
                "role": "robot",
                "lance": {                // ./src/run.sh <flags> <args>
                    "flags": ["--canbus", "--local"],
                    "args": {"robot": "lance2_{arena}"},
                    "set": {"redux": {"client_hostname": "{ip:client}"}}
                }
            },
            {
                "key": "client",
                "role": "client",
                "after": ["robot"],       // start once these are running
                "start_delay_s": 2,
                "lance": {"flags": ["--local"],
                          "args": {"client": "lance2_{arena}"},
                          "set": {"redux": {"robot_hostname": "{ip:robot}"}}}
            },
            {"key": "tool", "role": "client", "command": "ros2 topic list"}
        ]
    }

Placeholders in args, set values and commands:
    {option}    an option's value
    {ip:role}   the address of the role's machine, as reached from the
                machine this process runs on (the orchestrator picks one on a
                shared network; it can be overridden at launch)

"set" changes single values of the actions the presets select, which the
`key:=value` command line can't do (see lance_config.py).
'''

from __future__ import annotations

import glob
import json
import os
import re


SCENARIO_DIRECTORY = os.path.join('src', 'project', 'orchestrator',
                                  'scenarios')
PLACEHOLDER = re.compile(r'\{([A-Za-z_][\w-]*)(?::([\w-]+))?\}')


class ScenarioError(Exception):
    pass



# --- Templates ----------------------------------------------------------------



def placeholdersIn(value) -> set:
    '''Every {name} or {ip:role} placeholder inside a string, list or dict.'''
    found = set()
    if isinstance(value, str):
        for match in PLACEHOLDER.finditer(value):
            found.add((match.group(1), match.group(2)))
    elif isinstance(value, list):
        for item in value:
            found |= placeholdersIn(item)
    elif isinstance(value, dict):
        for item in value.values():
            found |= placeholdersIn(item)
    return found


def fillTemplate(value, options: dict, addresses: dict):
    '''Replace placeholders. A string that is exactly one placeholder keeps
    the replacement's type (so numbers stay numbers).'''
    if isinstance(value, list):
        return [fillTemplate(item, options, addresses) for item in value]
    if isinstance(value, dict):
        return {key: fillTemplate(item, options, addresses)
                for key, item in value.items()}
    if not isinstance(value, str):
        return value

    def lookup(match):
        name, argument = match.group(1), match.group(2)
        if name == 'ip':
            if argument not in addresses:
                raise ScenarioError('no address for role ' + str(argument))
            return addresses[argument]
        if argument is not None or name not in options:
            raise ScenarioError('unknown placeholder ' + match.group(0))
        return options[name]

    whole = PLACEHOLDER.fullmatch(value)
    if whole:
        return lookup(whole)
    return PLACEHOLDER.sub(lambda match: str(lookup(match)), value)



# --- Loading ------------------------------------------------------------------



def checkScenario(scenario_id: str, data: dict) -> None:
    roles = data.get('roles') or {}
    options = data.get('options') or {}
    keys = set()
    for process in data.get('processes') or []:
        key = process.get('key')
        if not key or key in keys:
            raise ScenarioError('%s: each process needs a unique "key"'
                                % scenario_id)
        keys.add(key)
        if process.get('role') not in roles:
            raise ScenarioError('%s: process %s uses unknown role %s'
                                % (scenario_id, key, process.get('role')))
        if ('lance' in process) == ('command' in process):
            raise ScenarioError('%s: process %s needs "lance" or "command"'
                                % (scenario_id, key))
        for name, argument in placeholdersIn(process):
            known = argument in roles if name == 'ip' else name in options
            if not known:
                placeholder = name + (':' + argument if argument else '')
                raise ScenarioError('%s: process %s uses unknown {%s}'
                                    % (scenario_id, key, placeholder))
    for process in data.get('processes') or []:
        for dependency in process.get('after') or []:
            if dependency not in keys:
                raise ScenarioError('%s: process %s waits for unknown %s'
                                    % (scenario_id, process['key'], dependency))


def loadScenarios(workspace_root) -> tuple:
    '''Returns (scenarios sorted for display, errors). A broken file is
    reported instead of stopping the orchestrator.'''
    if not workspace_root:
        return [], []
    scenarios = []
    errors = []
    pattern = os.path.join(workspace_root, SCENARIO_DIRECTORY, '*.json')
    for path in sorted(glob.glob(pattern)):
        scenario_id = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path) as file:
                data = json.load(file)
            checkScenario(scenario_id, data)
        except (ValueError, ScenarioError) as error:
            errors.append({'path': path, 'error': str(error)})
            continue
        data['id'] = scenario_id
        data['path'] = path
        data.setdefault('roles', {})
        data.setdefault('options', {})
        scenarios.append(data)
    scenarios.sort(key=lambda scenario: (scenario.get('order', 100),
                                         scenario.get('label', '')))
    return scenarios, errors



# --- Editing (the launch page's scenario editor) ----------------------------

SCENARIO_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,40}$')


def scenarioPath(workspace_root, scenario_id: str) -> str:
    if not workspace_root:
        raise ScenarioError('the orchestrator has no workspace')
    if not SCENARIO_ID.match(scenario_id or ''):
        raise ScenarioError('a scenario id is lowercase letters, digits and '
                            'dashes')
    return os.path.join(workspace_root, SCENARIO_DIRECTORY,
                        scenario_id + '.json')


def readScenarioSource(workspace_root, scenario_id: str) -> dict:
    path = scenarioPath(workspace_root, scenario_id)
    try:
        with open(path) as file:
            text = file.read()
    except FileNotFoundError:
        return {'id': scenario_id, 'path': path, 'exists': False, 'text': ''}
    return {'id': scenario_id, 'path': path, 'exists': True, 'text': text}


def parseScenarioText(scenario_id: str, text: str) -> dict:
    """The scenario a file's text describes, checked. Raises ScenarioError
    (with the line for JSON mistakes)."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise ScenarioError('not valid JSON: %s' % error)
    if not isinstance(data, dict):
        raise ScenarioError('a scenario is a JSON object')
    checkScenario(scenario_id, data)
    data.setdefault('roles', {})
    data.setdefault('options', {})
    return dict(data, id=scenario_id)


def writeScenario(workspace_root, scenario_id: str, text: str) -> str:
    path = scenarioPath(workspace_root, scenario_id)
    parseScenarioText(scenario_id, text)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = path + '.tmp'
    with open(temporary_path, 'w') as file:
        file.write(text if text.endswith('\n') else text + '\n')
    os.replace(temporary_path, path)
    return path


def deleteScenario(workspace_root, scenario_id: str) -> str:
    path = scenarioPath(workspace_root, scenario_id)
    if not os.path.isfile(path):
        raise ScenarioError('no scenario ' + scenario_id)
    os.remove(path)
    return path
