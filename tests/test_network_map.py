'''Building the network map from machine snapshots.'''

import unittest

from backend.network_map import DeviceBook, buildNetworkModel, machineKind


def link(machine, interfaces, route=None, sharing=None, info=None,
         state='online'):
    return {'machine': machine, 'connection': {'state': state},
            'info': info or {'platform': 'linux'},
            'stats': {'interfaces': interfaces, 'default_route': route},
            'sharing': sharing}


def port(name, kind, mac, *cidrs, **extra):
    return dict({'name': name, 'kind': kind, 'mac': mac, 'ipv4': list(cidrs),
                 'is_up': True, 'rx_bps': 1000.0, 'tx_bps': 2000.0}, **extra)


LINKS = [
    link({'id': 'local', 'label': 'Desktop', 'is_local': True},
         [port('enp3s0', 'ethernet', 'aa:01', '10.180.0.40/16'),
          port('enx00e04c583f40', 'ethernet', 'aa:02', '10.11.11.1/24'),
          port('docker0', 'bridge', 'aa:03', '172.17.0.1/16'),
          port('wlp5s0', 'wifi', 'aa:04')],
         route={'interface': 'enp3s0', 'gateway': '10.180.0.1'},
         sharing={'sessions': [{'name': 'share',
                                'downstream': 'enx00e04c583f40'}]}),
    link({'id': 'mac2', 'label': 'Mac mini 2',
          'vm': {'csm_vm_path': '~/code/csm-vm', 'guest': 'mac2-vm'}},
         [port('en0', 'ethernet', 'bb:01', '10.180.0.39/24'),
          port('bridge101', 'bridge', 'bb:02', '192.168.18.1/24')],
         route={'interface': 'en0', 'gateway': '10.180.0.1'},
         info={'platform': 'macos'}),
    link({'id': 'mac2-vm', 'label': 'VM', 'host': 'mac2'},
         [port('enp0s1', 'ethernet', 'cc:01', '10.11.11.38/24'),
          port('enp0s2', 'ethernet', 'cc:02', '192.168.18.2/24')]),
]


class NetworkMapTest(unittest.TestCase):

    def setUp(self):
        book = DeviceBook()
        book.see('dd:01', '10.180.0.1', 'neighbour', seen_by='local',
                 now_s=1000.0)
        book.see('dd:02', '10.11.11.233', 'lease', name='robot-laptop',
                 now_s=1000.0)
        book.see('cc:01', '10.11.11.38', 'lease', now_s=1000.0)  # the VM
        book.see('dd:03', '10.180.0.77', 'scan', now_s=1000.0)
        devices = book.snapshot(now_s=1001.0)
        self.model = buildNetworkModel(
            LINKS, devices, {'ip.net1.dev_1': '10.180.0.77'},
            {'machine_id': 'local', 'name': 'enp3s0'})
        self.networks = {entry['cidr']: entry
                         for entry in self.model['networks']}

    def test_networks_and_kinds(self):
        self.assertEqual(
            {cidr: entry['kind'] for cidr, entry in self.networks.items()},
            {'10.180.0.0/16': 'lan', '10.11.11.0/24': 'share',
             '192.168.18.0/24': 'host-only'})
        lan = self.networks['10.180.0.0/16']
        self.assertTrue(lan['internet'])
        self.assertEqual(lan['gateway'], '10.180.0.1')
        # The mac's /24 joined the desktop's /16.
        self.assertEqual({member['machine_id'] for member in lan['members']},
                         {'local', 'mac2'})
        self.assertEqual(self.networks['10.11.11.0/24']['share'],
                         {'machine_id': 'local', 'port': 'enx00e04c583f40',
                          'name': 'share'})

    def test_ports(self):
        machines = {machine['id']: machine
                    for machine in self.model['machines']}
        local = {entry['name']: entry for entry in machines['local']['ports']}
        self.assertNotIn('docker0', local)
        self.assertEqual(local['enx00e04c583f40']['label'], 'USB Ethernet')
        self.assertTrue(local['enx00e04c583f40']['sharing'])
        self.assertTrue(local['enp3s0']['is_robot_link'])
        self.assertIsNone(local['wlp5s0']['network'])
        self.assertEqual(machines['mac2']['kind'], 'mac')
        self.assertEqual(machines['mac2-vm']['kind'], 'vm')
        self.assertEqual(machines['mac2']['guest'], 'mac2-vm')

    def test_devices(self):
        devices = {device['mac']: device for device in self.model['devices']}
        self.assertNotIn('cc:01', devices)  # an inventory machine
        self.assertEqual(devices['dd:01']['kind'], 'router')
        self.assertEqual(devices['dd:02']['label'], 'robot-laptop')
        self.assertEqual(devices['dd:02']['network'], '10.11.11.0/24')
        self.assertEqual(devices['dd:03']['label'], 'dev_1')

    def test_devices_expire(self):
        book = DeviceBook()
        book.see('dd:01', '10.0.0.5', 'neighbour', now_s=0.0)
        self.assertEqual(book.snapshot(now_s=10.0)[0]['address'], '10.0.0.5')
        self.assertEqual(book.snapshot(now_s=100000.0), [])

    def test_machine_kind(self):
        self.assertEqual(machineKind({'id': 'x', 'icon': 'laptop'}, {}),
                         'laptop')
        self.assertEqual(machineKind({'id': 'robot', 'setup': ['can']}, {}),
                         'robot')
        self.assertEqual(machineKind({'id': 'mochapanda'}, {}), 'board')


if __name__ == '__main__':
    unittest.main()
