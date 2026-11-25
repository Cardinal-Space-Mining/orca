'''Editing scenario files from the app.'''

import json
import os
import tempfile
import unittest

from backend.scenarios import (ScenarioError, deleteScenario, loadScenarios,
                               parseScenarioText, readScenarioSource,
                               writeScenario)


SCENARIO = {
    'label': 'Test', 'roles': {'machine': {'label': 'Machine'}},
    'processes': [{'key': 'main', 'role': 'machine', 'command': 'true'}],
}


class ScenarioEditTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = self.directory.name

    def test_write_read_load_delete(self):
        text = json.dumps(SCENARIO, indent=4)
        path = writeScenario(self.workspace, 'my-test', text)
        self.assertTrue(path.endswith('src/project/orca/scenarios/'
                                      'my-test.json'))
        source = readScenarioSource(self.workspace, 'my-test')
        self.assertTrue(source['exists'])
        self.assertEqual(source['text'], text + '\n')
        scenarios, errors = loadScenarios(self.workspace)
        self.assertEqual([scenario['id'] for scenario in scenarios],
                         ['my-test'])
        self.assertEqual(errors, [])
        deleteScenario(self.workspace, 'my-test')
        self.assertFalse(os.path.exists(path))
        self.assertFalse(readScenarioSource(self.workspace,
                                            'my-test')['exists'])

    def test_rejects_bad_ids_text_and_scenarios(self):
        for bad_id in ('', 'Has Caps', '../escape', 'a/b'):
            with self.subTest(bad_id), self.assertRaises(ScenarioError):
                writeScenario(self.workspace, bad_id, json.dumps(SCENARIO))
        with self.assertRaisesRegex(ScenarioError, 'line 1'):
            parseScenarioText('x', '{"label": }')
        broken = dict(SCENARIO, processes=[{'key': 'a', 'role': 'nobody',
                                            'command': 'true'}])
        with self.assertRaises(ScenarioError):
            writeScenario(self.workspace, 'broken', json.dumps(broken))
        self.assertFalse(os.path.exists(os.path.join(
            self.workspace, 'src', 'project', 'orca', 'scenarios',
            'broken.json')))


if __name__ == '__main__':
    unittest.main()
