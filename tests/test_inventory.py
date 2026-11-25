'''Inventory files: merging, validation of edits, writing.'''

import json
import os
import tempfile
import unittest
from unittest import mock

from backend import inventory


class InventoryTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = os.path.join(self.directory.name, 'ws')
        self.user_path = os.path.join(self.directory.name, 'user.json')
        patcher = mock.patch.object(inventory, 'USER_INVENTORY_PATH',
                                    self.user_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)
        self.writeProject([
            {'id': 'host', 'label': 'Host',
             'routes': [{'host': '10.0.0.1', 'user': 'lance'}]},
            {'id': 'guest', 'label': 'Guest',
             'routes': [{'host': '192.168.18.2', 'user': 'lance',
                         'via': 'host'}]},
        ])

    def writeProject(self, machines):
        path = os.path.join(self.workspace, inventory.PROJECT_INVENTORY_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as file:
            json.dump({'machines': machines}, file)

    def test_load_merges_user_entries_over_team_entries(self):
        with open(self.user_path, 'w') as file:
            json.dump({'machines': [
                {'id': 'host', 'label': 'My host',
                 'routes': [{'host': '10.0.0.9', 'user': 'me'}]}]}, file)
        machines = {machine.id: machine
                    for machine in inventory.loadInventory(self.workspace)}
        self.assertEqual(list(machines)[0], 'local')
        self.assertEqual(machines['host'].label, 'My host')
        self.assertEqual(machines['host'].routes[0].host, '10.0.0.9')
        self.assertEqual(machines['guest'].routes[0].via, 'host')

    def test_edit_rejects_duplicates_and_unknown_jumps(self):
        with self.assertRaises(inventory.InventoryError):
            inventory.validateEdit(self.workspace, 'user', [
                {'id': 'a', 'routes': []}, {'id': 'a', 'routes': []}])
        with self.assertRaises(inventory.InventoryError):
            inventory.validateEdit(self.workspace, 'user', [
                {'id': 'a', 'routes': [{'host': 'x', 'via': 'nowhere'}]}])

    def test_write_then_load(self):
        inventory.writeInventory(self.workspace, 'user', [
            {'id': 'bench', 'label': 'Bench',
             'routes': [{'host': 'bench.local', 'user': 'lance'}]}])
        ids = [machine.id
               for machine in inventory.loadInventory(self.workspace)]
        self.assertEqual(ids, ['local', 'host', 'guest', 'bench'])


if __name__ == '__main__':
    unittest.main()
