'''The setup checklist's fix scripts and inventory roles.'''

import subprocess
import unittest

from backend.inventory import SETUP_TAGS, InventoryError, parseMachine
from backend.setup import FIXES, scriptFor


class SetupTest(unittest.TestCase):

    def test_every_fix_script_is_valid_bash(self):
        for check_id, (title, body) in FIXES.items():
            with self.subTest(check_id):
                script = scriptFor(check_id, title, body)
                result = subprocess.run(['bash', '-n'], input=script,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(script.startswith('#!/bin/bash'))
                self.assertIn('set -e', script)

    def test_sudoers_rules_are_narrow_and_checked(self):
        for check_id in ('can-sudo', 'vm-sudo'):
            body = FIXES[check_id][1]
            with self.subTest(check_id):
                self.assertIn('visudo -cf', body)
                self.assertIn('NOPASSWD:', body)
                self.assertNotIn('NOPASSWD: ALL', body)
                self.assertIn('-m 0440', body)

    def test_inventory_roles(self):
        machine = parseMachine({'id': 'robot', 'setup': ['can']}, 'test')
        self.assertEqual(machine.setup, ['can'])
        self.assertEqual(set(SETUP_TAGS), {'can', 'sharing'})
        with self.assertRaises(InventoryError):
            parseMachine({'id': 'robot', 'setup': ['flying']}, 'test')


if __name__ == '__main__':
    unittest.main()
