'''Editing presets in place: the text edits on their own, and the editor
against a copy of this workspace's lance config (skipped outside a
workspace).'''

import difflib
import json
import os
import shutil
import tempfile
import unittest

from backend.config_edit import (ConfigEditor, insertMember, removeMember,
                                 replaceMember)
from backend.core import ORCA_ROOT
from backend.lance_config import ConfigError, LanceConfig
from backend.settings import validateChanges


WORKSPACE = os.path.dirname(os.path.dirname(ORCA_ROOT))
HAS_CONFIG = os.path.isfile(os.path.join(
    WORKSPACE, 'src', 'lance', 'config', 'launch.json'))

TEXT = '''{
    "dev":
    {
        "pragma:default": null,

        "one":
        {
            "a": 1
        },

        "two":
        {
            "pragma:derived": "one",
            "list": [ 1, 2 ]
        }
    }
}
'''


class TextEditTest(unittest.TestCase):

    def test_replace_changes_only_that_preset(self):
        edited = replaceMember(TEXT, ['dev', 'one'], {'a': 2, 'b': {'c': 3}})
        self.assertEqual(json.loads(edited)['dev']['one'],
                         {'a': 2, 'b': {'c': 3}})
        # Braces below keys, as the file has them; "two" untouched.
        self.assertIn('"b":\n            {\n', edited)
        self.assertIn('"list": [ 1, 2 ]', edited)

    def test_same_value_keeps_the_text(self):
        self.assertEqual(replaceMember(TEXT, ['dev', 'one'], {'a': 1}), TEXT)

    def test_insert_follows_the_layout(self):
        edited = insertMember(TEXT, ['dev', 'three'], {'x': True})
        self.assertEqual(json.loads(edited)['dev']['three'], {'x': True})
        self.assertIn('},\n\n        "three":\n        {\n            "x": true'
                      '\n        }\n    }\n}', edited)

    def test_insert_refuses_existing(self):
        with self.assertRaises(ConfigError):
            insertMember(TEXT, ['dev', 'one'], {})

    def test_remove_first_middle_and_last(self):
        for name in ('pragma:default', 'one', 'two'):
            expected = json.loads(TEXT)
            del expected['dev'][name]
            self.assertEqual(json.loads(removeMember(TEXT, ['dev', name])),
                             expected)

    def test_insert_into_empty_object(self):
        edited = insertMember('{"dev": {}}', ['dev', 'x'], {'a': 1})
        self.assertEqual(json.loads(edited), {'dev': {'x': {'a': 1}}})


@unittest.skipUnless(HAS_CONFIG, 'no lance workspace around Orca')
class EditorTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, 'src'))
        shutil.copytree(os.path.join(WORKSPACE, 'src', 'lance', 'config'),
                        os.path.join(self.root, 'src', 'lance', 'config'))
        os.symlink(os.path.join(WORKSPACE, 'src', 'launch-utils'),
                   os.path.join(self.root, 'src', 'launch-utils'))
        self.config = LanceConfig(self.root)
        self.editor = ConfigEditor(self.config, self.root)
        self.path = os.path.join(self.root, 'src', 'lance', 'config',
                                 'presets', 'dev_presets.json')

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_new_preset_goes_in_the_actions_file_and_resolves(self):
        value = {'pragma:derived': 'motor_sim',
                 'pragma:action_overrides': {'gz_gui': 'enabled'}}
        path = self.editor.edit('dev', 'test_new', value)
        self.assertEqual(path, os.path.realpath(self.path))
        with open(self.path) as file:
            self.assertEqual(json.load(file)['dev']['test_new'], value)
        selected = self.config.resolve({'dev': 'test_new'})['selected']
        self.assertEqual(selected['gz_gui'], 'enabled')
        self.assertEqual(selected['motor_sim'], 'lance2')

    def test_edit_keeps_the_rest_of_the_file(self):
        with open(self.path) as file:
            before = file.read()
        raw = json.loads(before)['dev']['motor_sim']
        raw['pragma:action_overrides']['gz_gui'] = 'enabled'
        self.editor.edit('dev', 'motor_sim', raw)
        with open(self.path) as file:
            after = file.read()
        changed = [line for line in difflib.ndiff(before.splitlines(),
                                                  after.splitlines())
                   if line[:2] in ('- ', '+ ')]
        self.assertEqual([line[0] + line[2:].strip() for line in changed],
                         ['-"foxglove_bridge": "all"',
                          '+"foxglove_bridge": "all",',
                          '+"gz_gui": "enabled"'])

    def test_check_reports_problems(self):
        result = self.editor.check('dev', 'x', {
            'pragma:action_overrides': {'no_such_action': 'a'}})
        self.assertEqual(result['problems'],
                         ['selects "no_such_action", which is not an action'])
        with self.assertRaises(ConfigError):
            self.editor.check('dev', 'x', {
                'pragma:action_overrides': {'redux': 'no_such_preset'}})
        with self.assertRaises(ConfigError):
            self.editor.check('dev', 'x', {'pragma:derived': 'missing'})
        with self.assertRaises(ConfigError):
            self.editor.check('dev', 'x', {'pragma:derived': 'x'})

    def test_delete_refuses_referenced_presets(self):
        with self.assertRaises(ConfigError):
            self.editor.delete('dev', 'lidar_dev_net1')  # net2 derives
        self.editor.delete('dev', 'lidar_dev_net2')
        with open(self.path) as file:
            self.assertNotIn('lidar_dev_net2', json.load(file)['dev'])

    def test_descriptions(self):
        self.editor.setDescription('dev:=motor_sim', ' Motors, simulated ')
        self.assertEqual(self.editor.descriptions(),
                         {'dev:=motor_sim': 'Motors, simulated'})
        self.editor.setDescription('dev:=motor_sim', '')
        self.assertEqual(self.editor.descriptions(), {})


class SettingsTest(unittest.TestCase):

    def test_validation(self):
        validateChanges({'budget': {'limit_kbps': 4000}, 'startup': {
            'port': 8740, 'open_browser': False}})
        for bad in ({'budget': {'limit_kbps': 'lots'}},
                    {'budget': {'num_cameras': -1}},
                    {'startup': {'port': 80}},
                    {'competition_mode': 'yes'}):
            with self.assertRaises(ValueError):
                validateChanges(bad)


if __name__ == '__main__':
    unittest.main()
