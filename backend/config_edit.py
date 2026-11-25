'''
Editing the lance launch config (src/lance/config) from the app: a preset's
JSON, new presets and deleting them, plus the descriptions shown beside
presets.

Edits change only the text of the preset they touch, so the rest of the file
keeps its layout and a commit shows just that preset. Every edit is checked
by parsing the result and comparing it to the intended data before anything
is written.

Descriptions don't go in the presets: launch-utils passes every key of a
resolved preset to the node as a ROS parameter (FINDINGS.md L3), so they live
in src/project/orchestrator/presets.json instead:

    {
        "dev": "What the action is for",
        "dev:=robot_motor_sim": "What this preset is for"
    }
'''

from __future__ import annotations

import copy
import json
import os
import re
from typing import Optional

from .lance_config import (ConfigError, DERIVED_TAG, LanceConfig,
                           OVERRIDES_TAG)


DESCRIPTIONS_RELATIVE_PATH = os.path.join('src', 'project', 'orchestrator',
                                          'presets.json')
PRESET_NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}$')
IMPORT_TAG = 'pragma:import'
NULL_NAMES = ('null', 'none', 'false')



# --- Locating members in JSON text -------------------------------------------



class Member:
    '''One "key": value of an object, by character offsets in the text.'''

    def __init__(self, key, key_start, value_start, value_end, value):
        self.key = key
        self.key_start = key_start
        self.value_start = value_start
        self.value_end = value_end
        self.value = value  # an ObjectSpan for objects, else None


class ObjectSpan:

    def __init__(self, start):
        self.start = start
        self.end = None
        self.members = []

    def member(self, key) -> Optional[Member]:
        for member in self.members:
            if member.key == key:
                return member
        return None


class SpanParser:
    '''A strict JSON reader that records where every object member is.'''

    def __init__(self, text: str):
        self.text = text
        self.decoder = json.JSONDecoder()

    def skipSpace(self, index: int) -> int:
        while index < len(self.text) and self.text[index] in ' \t\r\n':
            index += 1
        return index

    def parse(self) -> ObjectSpan:
        index = self.skipSpace(0)
        if index >= len(self.text) or self.text[index] != '{':
            raise ConfigError('the file is not a JSON object')
        span, index = self.parseObject(index)
        if self.skipSpace(index) != len(self.text):
            raise ConfigError('text after the JSON object')
        return span

    def parseValue(self, index: int):
        '''Returns (ObjectSpan or None, end index).'''
        if self.text[index] == '{':
            return self.parseObject(index)
        if self.text[index] == '[':
            return None, self.parseArray(index)
        _, end = self.decoder.raw_decode(self.text, index)
        return None, end

    def parseArray(self, index: int) -> int:
        index = self.skipSpace(index + 1)
        if self.text[index] == ']':
            return index + 1
        while True:
            _, index = self.parseValue(self.skipSpace(index))
            index = self.skipSpace(index)
            if self.text[index] == ']':
                return index + 1
            self.expect(index, ',')
            index += 1

    def parseObject(self, index: int):
        span = ObjectSpan(index)
        index = self.skipSpace(index + 1)
        if self.text[index] == '}':
            span.end = index + 1
            return span, index + 1
        while True:
            key_start = index
            self.expect(index, '"')
            key, index = self.decoder.raw_decode(self.text, index)
            index = self.skipSpace(index)
            self.expect(index, ':')
            value_start = self.skipSpace(index + 1)
            value, index = self.parseValue(value_start)
            span.members.append(Member(key, key_start, value_start, index,
                                       value))
            index = self.skipSpace(index)
            if self.text[index] == '}':
                span.end = index + 1
                return span, index + 1
            self.expect(index, ',')
            index = self.skipSpace(index + 1)

    def expect(self, index: int, character: str):
        if index >= len(self.text) or self.text[index] != character:
            raise ConfigError('unexpected JSON at character %d' % index)


def lineIndent(text: str, index: int) -> str:
    line_start = text.rfind('\n', 0, index) + 1
    line = text[line_start:index]
    return line[:len(line) - len(line.lstrip(' \t'))]


def formatValue(value, indent: str, braces_below: bool = False) -> str:
    """JSON for value, its lines after the first indented by indent. With
    braces_below, a nested object's brace goes on the line below its key,
    as the lance config files have it."""
    if not braces_below or not isinstance(value, dict) or not value:
        lines = json.dumps(value, indent=4, ensure_ascii=False).split('\n')
        return '\n'.join([lines[0]] + [indent + line for line in lines[1:]])
    inner = indent + '    '
    members = []
    for key, item in value.items():
        separator = ':\n' + inner if isinstance(item, dict) and item \
            else ': '
        members.append('%s%s%s%s' % (inner, json.dumps(
            key, ensure_ascii=False), separator, formatValue(
                item, inner, braces_below)))
    return '{\n%s\n%s}' % (',\n'.join(members), indent)


def bracesBelow(text: str) -> bool:
    return re.search(r'":[ \t]*\n[ \t]*\{', text) is not None


def findObject(root: ObjectSpan, path: list) -> ObjectSpan:
    span = root
    for key in path:
        member = span.member(key)
        if member is None or member.value is None:
            raise ConfigError('no object at ' + '/'.join(path))
        span = member.value
    return span


def replaceMember(text: str, path: list, value) -> str:
    '''Replace the value at path ([action, preset]) in the text.'''
    parent = findObject(SpanParser(text).parse(), path[:-1])
    member = parent.member(path[-1])
    if member is None:
        raise ConfigError('nothing at ' + '/'.join(path))
    indent = lineIndent(text, member.value_start)
    return (text[:member.value_start] + formatValue(
        value, indent, bracesBelow(text)) + text[member.value_end:])


def insertMember(text: str, path: list, value) -> str:
    '''Add path[-1] at the end of the object at path[:-1], laid out like
    the members before it.'''
    parent = findObject(SpanParser(text).parse(), path[:-1])
    if parent.member(path[-1]) is not None:
        raise ConfigError('"%s" already exists' % path[-1])
    key = json.dumps(path[-1], ensure_ascii=False)
    if parent.members:
        last = parent.members[-1]
        indent = lineIndent(text, last.key_start)
        between = text[last.key_start:last.value_start]
        between = between[between.index(':'):]
        # "key":\n<indent>{ or "key": {, as the last member has it; a blank
        # line between members if they have them.
        separator = ':\n' + indent if '\n' in between else ': '
        gap = '\n\n' if '\n\n' in text[parent.members[0].value_end:
                                        last.key_start] else '\n'
        addition = ',%s%s%s%s%s' % (gap, indent, key, separator,
                                    formatValue(value, indent,
                                                bracesBelow(text)))
        return text[:last.value_end] + addition + text[last.value_end:]
    indent = lineIndent(text, parent.start) + '    '
    close = parent.end - 1
    addition = '\n%s%s: %s\n%s' % (indent, key, formatValue(
        value, indent, bracesBelow(text)), lineIndent(text, close))
    return text[:parent.start + 1] + addition + text[close:]


def removeMember(text: str, path: list) -> str:
    parent = findObject(SpanParser(text).parse(), path[:-1])
    member = parent.member(path[-1])
    if member is None:
        raise ConfigError('nothing at ' + '/'.join(path))
    position = parent.members.index(member)
    if position > 0:
        # From the end of the member before (its comma goes too).
        start = parent.members[position - 1].value_end
        return text[:start] + text[member.value_end:]
    if len(parent.members) > 1:
        end = parent.members[1].key_start
        return text[:member.key_start] + text[end:]
    return text[:parent.start + 1] + text[parent.end - 1:]


def checkedEdit(text: str, expected: dict, edited: str) -> str:
    '''The edited text, if it parses to exactly what was meant.'''
    try:
        result = json.loads(edited)
    except ValueError as error:
        raise ConfigError('the edit would break the file: %s' % error)
    if result != expected:
        raise ConfigError("the edit didn't come out as intended; the file "
                          'was not changed')
    return edited



# --- Files and checks --------------------------------------------------------



def configFiles(config: LanceConfig) -> list:
    '''The config's files in import order: [(path, data)].'''
    files = []
    queue = [config.config_path]
    seen = set()
    while queue:
        path = os.path.realpath(queue.pop(0))
        if path in seen:
            continue
        seen.add(path)
        with open(path) as file:
            try:
                data = json.load(file)
            except ValueError as error:
                raise ConfigError('%s: %s' % (path, error))
        files.append((path, data))
        base = os.path.dirname(os.path.realpath(config.config_path))
        for name in data.get(IMPORT_TAG, []):
            queue.append(os.path.join(base, name))
    return files


def isNullName(name) -> bool:
    return name is None or str(name).lower() in NULL_NAMES


def overrideProblems(merged: dict, preset: dict) -> list:
    problems = []
    for action, name in (preset.get(OVERRIDES_TAG) or {}).items():
        block = merged.get(action)
        if not isinstance(block, dict) or action.startswith('pragma:'):
            problems.append('selects "%s", which is not an action' % action)
        elif not isNullName(name) and name != 'default' and name not in block:
            problems.append('selects %s:=%s, which does not exist'
                            % (action, name))
    return problems


class ConfigEditor:

    def __init__(self, config: LanceConfig, workspace_root: str):
        self.config = config
        self.descriptions_path = os.path.join(workspace_root,
                                              DESCRIPTIONS_RELATIVE_PATH)

    # --- Descriptions ---

    def descriptions(self) -> dict:
        try:
            with open(self.descriptions_path) as file:
                data = json.load(file)
        except OSError:
            return {}
        except ValueError as error:
            raise ConfigError('%s: %s' % (self.descriptions_path, error))
        return {key: value for key, value in data.items()
                if isinstance(value, str)}

    def setDescription(self, key: str, text: str):
        descriptions = self.descriptions()
        text = (text or '').strip()
        if text:
            descriptions[key] = text
        else:
            descriptions.pop(key, None)
        os.makedirs(os.path.dirname(self.descriptions_path), exist_ok=True)
        temporary = self.descriptions_path + '.tmp'
        with open(temporary, 'w') as file:
            json.dump(dict(sorted(descriptions.items())), file, indent=4,
                      ensure_ascii=False)
            file.write('\n')
        os.replace(temporary, self.descriptions_path)

    # --- Where things are ---

    def sourceOf(self, action: str, preset: Optional[str] = None) -> str:
        '''The file that defines the preset (or, with no preset, the one new
        presets of the action go in: the one with its "pragma:default").'''
        candidates = []
        for path, data in configFiles(self.config):
            block = data.get(action)
            if not isinstance(block, dict):
                continue
            if preset is None:
                candidates.append((0 if 'pragma:default' in block else 1,
                                   len(candidates), path))
            elif preset in block:
                return path
        if candidates:
            return min(candidates)[2]
        raise ConfigError('no file defines %s' % (
            '%s:=%s' % (action, preset) if preset else action))

    def relative(self, path: str) -> str:
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(self.config.config_path))))
        return os.path.relpath(path, root)

    def references(self, action: str, preset: str) -> list:
        '''What else names this preset: presets derived from it, setups that
        select it, the action's default.'''
        found = []
        merged = self.config.load()
        block = merged.get(action, {})
        if block.get('pragma:default') == preset:
            found.append("it is %s's default" % action)
        for name, other in block.items():
            if isinstance(other, dict) and other.get(DERIVED_TAG) == preset:
                found.append('%s:=%s derives from it' % (action, name))
        for other_action, other_block in merged.items():
            if other_action.startswith('pragma:') \
                    or not isinstance(other_block, dict):
                continue
            for name, other in other_block.items():
                if isinstance(other, dict) and (other.get(OVERRIDES_TAG)
                                                or {}).get(action) == preset:
                    found.append('%s:=%s selects it' % (other_action, name))
        return found

    # --- Checking ---

    def check(self, action: str, preset: str, value) -> dict:
        '''What would go wrong with value as action:=preset.'''
        merged = self.config.load()
        if not isinstance(merged.get(action), dict) \
                or action.startswith('pragma:'):
            raise ConfigError('no action "%s"' % action)
        if not PRESET_NAME.match(preset or '') or preset.startswith('pragma:'):
            raise ConfigError('a preset name is letters, digits, _ . -')
        if not isinstance(value, dict):
            raise ConfigError('a preset is a JSON object')
        merged = copy.deepcopy(merged)
        merged[action][preset] = copy.deepcopy(value)
        parent = value.get(DERIVED_TAG)
        if parent is not None and parent not in merged[action]:
            raise ConfigError('derives from %s:=%s, which does not exist'
                              % (action, parent))
        problems = overrideProblems(merged, value)
        # launch-utils fails on a missing preset but skips unknown actions.
        missing = [problem for problem in problems
                   if problem.endswith('does not exist')]
        if missing:
            raise ConfigError(missing[0])
        try:
            resolution = self.config.resolve({action: preset}, merged)
        except ConfigError as error:
            raise ConfigError('launch-utils rejects it: %s' % error)
        except RecursionError:
            raise ConfigError('circular derivation')
        return {'problems': problems,
                'resolved': resolution['config'].get(action),
                'selected': resolution['selected']}

    # --- Changing ---

    def edit(self, action: str, preset: str, value) -> str:
        '''Write action:=preset (new or changed); returns the file.'''
        self.check(action, preset, value)
        exists = isinstance(self.config.load().get(action), dict) \
            and preset in self.config.load()[action]
        path = self.sourceOf(action, preset if exists else None)
        with open(path) as file:
            text = file.read()
        data = json.loads(text)
        if exists and preset not in data.get(action, {}):
            raise ConfigError('%s:=%s is not in %s' % (action, preset, path))
        if not exists and preset in data.get(action, {}):
            raise ConfigError('%s:=%s already exists' % (action, preset))
        expected = copy.deepcopy(data)
        expected[action][preset] = value
        change = replaceMember if exists else insertMember
        self.write(path, checkedEdit(text, expected, change(
            text, [action, preset], value)))
        return path

    def delete(self, action: str, preset: str,
               other_references: tuple = ()) -> str:
        references = self.references(action, preset) + list(
            other_references)
        if references:
            raise ConfigError("can't delete %s:=%s: %s" % (
                action, preset, '; '.join(references)))
        path = self.sourceOf(action, preset)
        with open(path) as file:
            text = file.read()
        expected = json.loads(text)
        del expected[action][preset]
        self.write(path, checkedEdit(text, expected, removeMember(
            text, [action, preset])))
        self.setDescription('%s:=%s' % (action, preset), '')
        return path

    def write(self, path: str, text: str):
        temporary = path + '.orchestrator-tmp'
        with open(temporary, 'w') as file:
            file.write(text)
        os.replace(temporary, path)
