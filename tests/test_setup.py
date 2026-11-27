'''The setup checklist's fix scripts and inventory roles.'''

import asyncio
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from backend import setup
from backend.inventory import SETUP_TAGS, InventoryError, parseMachine
from backend.runs import PlanError
from backend.setup import FIXES, Setup, scriptFor


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


class PushRunVmTest(unittest.TestCase):
    '''"Update run-vm.sh" copies this laptop's run-vm.sh and its lib/ to a
    machine; the script it writes swaps them in.'''

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = directory.name
        self.laptop = os.path.join(root, 'laptop', 'csm-vm')
        self.home = os.path.join(root, 'machine-home')
        self.machine = os.path.join(self.home, 'code', 'csm-vm')
        os.makedirs(os.path.join(self.laptop, 'lib'))
        os.makedirs(os.path.join(self.machine, 'lib'))
        self.write(self.laptop, 'run-vm.sh',
                   '#!/bin/bash\nsource "$SCRIPTPATH/lib/common.sh"\n'
                   'echo "--overlay new"\n')
        self.write(self.laptop, 'lib/common.sh', 'new common\n')
        self.write(self.machine, 'run-vm.sh', '#!/bin/bash\necho old\n')
        self.write(self.machine, 'lib/common.sh', 'old common\n')

    def write(self, folder, name, text):
        with open(os.path.join(folder, name), 'w') as file:
            file.write(text)

    def read(self, folder, name):
        with open(os.path.join(folder, name)) as file:
            return file.read()

    def push(self):
        home = self.home

        async def call(method, params):
            assert method == 'file.write'
            path = params['path'].replace('~', home, 1)
            with open(path, 'w') as file:
                file.write(params['content'])

        link = types.SimpleNamespace(
            call=call, machine=types.SimpleNamespace(
                vm=types.SimpleNamespace(csm_vm_path='~/code/csm-vm')))
        with mock.patch.object(setup, 'localRunVm', return_value=os.path.join(
                self.laptop, 'run-vm.sh')):
            return asyncio.run(Setup(None).pushRunVm(link))

    def test_swaps_in_the_script_and_its_libraries(self):
        title, body = self.push()
        result = subprocess.run(
            ['bash', '-c', scriptFor('csm-vm', title, body)],
            env=dict(os.environ, HOME=self.home), capture_output=True,
            text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--overlay new', result.stdout)
        self.assertEqual(self.read(self.machine, 'lib/common.sh'),
                         'new common\n')
        self.assertEqual(self.read(self.machine, 'lib.bak/common.sh'),
                         'old common\n')
        self.assertIn('echo old', self.read(self.machine, 'run-vm.sh.bak'))
        self.assertEqual(sorted(os.listdir(self.machine)),
                         ['lib', 'lib.bak', 'run-vm.sh', 'run-vm.sh.bak'])

    def test_refuses_a_checkout_missing_a_library(self):
        os.remove(os.path.join(self.laptop, 'lib', 'common.sh'))
        with self.assertRaises(PlanError):
            self.push()


if __name__ == '__main__':
    unittest.main()
