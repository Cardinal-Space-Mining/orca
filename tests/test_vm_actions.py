'''VM actions that name a saved session: only the sessions the host
reported, by their exact file name, reach run-vm.sh.'''

import unittest

from backend.runs import PlanError
from backend.vm import VmController


VM = {'is_running': False, 'saved_overlays': [
    {'name': 'ubuntu-aarch64-overlay.saved-20261126-231450.qcow2'},
    {'name': 'ubuntu-aarch64-overlay.backup.qcow2'}]}


class SavedNameTest(unittest.TestCase):

    def test_known_session(self):
        controller = VmController(None)
        for entry in VM['saved_overlays']:
            self.assertEqual(controller.savedName(VM, {'name': entry['name']}),
                             entry['name'])

    def test_unknown_or_odd_names_are_refused(self):
        controller = VmController(None)
        for name in ('', 'ubuntu-aarch64-overlay.qcow2',
                     'ubuntu-aarch64-base.qcow2', '../../etc/passwd',
                     'ubuntu-aarch64-overlay.saved-2026; rm -rf ~.qcow2',
                     'ubuntu-aarch64-overlay.saved-20990101-000000.qcow2'):
            with self.assertRaises(PlanError):
                controller.savedName(VM, {'name': name})


if __name__ == '__main__':
    unittest.main()
