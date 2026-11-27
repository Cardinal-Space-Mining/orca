'''The agent's view of a csm-vm checkout: edits that exist only on that
machine, and the state of the VM's overlay.'''

import importlib.util
import os
import subprocess
import tempfile
import unittest

from backend.core import AGENT_SOURCE_PATH


spec = importlib.util.spec_from_file_location('orca_agent', AGENT_SOURCE_PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def git(directory, *args):
    subprocess.run(['git', '-C', directory, '-c', 'user.email=t@t',
                    '-c', 'user.name=t'] + list(args),
                   check=True, capture_output=True)


class RepositoryStatusTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = self.directory.name
        self.remote = os.path.join(root, 'remote.git')
        self.clone = os.path.join(root, 'csm-vm')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main',
                        self.remote], check=True)
        subprocess.run(['git', 'clone', '-q', self.remote, self.clone],
                       check=True, capture_output=True)
        git(self.clone, 'commit', '-q', '--allow-empty', '-m', 'one')
        git(self.clone, 'push', '-q', '-u', 'origin', 'main')

    def tearDown(self):
        self.directory.cleanup()

    def test_in_sync(self):
        status = agent.repositoryStatus(self.clone)
        self.assertEqual((status['branch'], status['upstream']),
                         ('main', 'origin/main'))
        self.assertEqual((status['num_changes'], status['num_ahead'],
                          status['num_unpushed']), (0, 0, 0))

    def test_uncommitted_and_unpushed(self):
        with open(os.path.join(self.clone, '.gitignore'), 'w') as file:
            file.write('*.qcow2\n')
        with open(os.path.join(self.clone, 'disk.qcow2'), 'w') as file:
            file.write('ignored')
        git(self.clone, 'commit', '-q', '--allow-empty', '-m', 'two')
        status = agent.repositoryStatus(self.clone)
        self.assertEqual(status['changed_files'], ['.gitignore'])
        self.assertEqual((status['num_ahead'], status['num_unpushed']),
                         (1, 1))

    def test_work_on_a_branch_never_pushed(self):
        git(self.clone, 'switch', '-q', '-c', 'fixes')
        git(self.clone, 'commit', '-q', '--allow-empty', '-m', 'fix')
        git(self.clone, 'switch', '-q', 'main')
        status = agent.repositoryStatus(self.clone)
        # main is in sync, but the other branch has a commit no remote has.
        self.assertEqual((status['num_ahead'], status['num_unpushed']),
                         (0, 1))

    def test_not_a_checkout(self):
        self.assertIsNone(agent.repositoryStatus(self.directory.name))


class OverlayStatusTest(unittest.TestCase):

    def test_marker_and_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            overlay = os.path.join(directory, 'overlay.qcow2')
            self.assertIsNone(agent.overlayStatus(overlay))
            open(overlay, 'w').close()
            status = agent.overlayStatus(overlay)
            self.assertFalse(status['is_unclean'])
            self.assertTrue(status['is_writable'])
            open(overlay + '.unclean', 'w').close()
            self.assertTrue(agent.overlayStatus(overlay)['is_unclean'])


if __name__ == '__main__':
    unittest.main()
