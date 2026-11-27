'''The agent's view of a csm-vm checkout: edits that exist only on that
machine, and the state of the VM's overlay.'''

import importlib.util
import os
import shutil
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


class RunVmLibrariesTest(unittest.TestCase):

    def test_missing_libraries(self):
        with tempfile.TemporaryDirectory() as folder:
            os.mkdir(os.path.join(folder, 'lib'))
            open(os.path.join(folder, 'lib', 'common.sh'), 'w').close()
            text = ('source "$SCRIPTPATH/lib/common.sh"\n'
                    'source "$SCRIPTPATH/lib/modes.sh"\n'
                    '# source "$SCRIPTPATH/lib/old.sh"\n')
            self.assertEqual(agent.missingRunVmLibraries(
                os.path.join(folder, 'run-vm.sh'), text), ['lib/modes.sh'])
            self.assertEqual(agent.missingRunVmLibraries(
                os.path.join(folder, 'run-vm.sh'), 'echo one file'), [])


@unittest.skipUnless(shutil.which('git'), 'needs git')
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

    def test_vm_stopped_by_a_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            overlay = os.path.join(directory, 'overlay.qcow2')
            log = os.path.join(directory, 'qemu.log')
            open(overlay, 'w').close()
            with open(log, 'w') as file:
                file.write('qemu-system-aarch64: terminating on signal 15 '
                           'from pid 474 (<unknown process>)\n')
            self.assertTrue(agent.overlayStatus(overlay, False, log)
                            ['is_unclean'])
            # A power cut: QEMU's exit was never recorded.
            with open(log, 'w') as file:
                file.write('qemu-system-aarch64: libusb noise\n')
            self.assertTrue(agent.overlayStatus(overlay, False, log)
                            ['is_unclean'])
            with open(log, 'a') as file:
                file.write('--- QEMU exited with status 0 at 2026-11-26 ---\n')
            self.assertFalse(agent.overlayStatus(overlay, False, log)
                             ['is_unclean'])
            # While it runs, an old line means nothing.
            self.assertFalse(agent.overlayStatus(overlay, True, log)
                             ['is_unclean'])


class SavedOverlaysTest(unittest.TestCase):

    def test_lists_saved_sessions_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            names = ['ubuntu-aarch64-overlay.saved-20261126-231450.qcow2',
                     'ubuntu-aarch64-overlay.backup.qcow2',
                     'ubuntu-aarch64-overlay.qcow2',          # the current one
                     'ubuntu-aarch64-base.backup.qcow2']      # the base's backup
            for index, name in enumerate(names):
                path = os.path.join(directory, name)
                open(path, 'w').close()
                os.utime(path, (1000 + index, 1000 + index))
            self.assertEqual(
                [entry['name'] for entry in agent.savedOverlays(directory)],
                ['ubuntu-aarch64-overlay.backup.qcow2',
                 'ubuntu-aarch64-overlay.saved-20261126-231450.qcow2'])


if __name__ == '__main__':
    unittest.main()
