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
         [port('enp3s0', 'ethernet', 'aa:01', '10.20.0.40/16'),
          port('enx001122334455', 'ethernet', 'aa:02', '10.11.11.1/24'),
          port('docker0', 'bridge', 'aa:03', '172.17.0.1/16'),
          port('wlp5s0', 'wifi', 'aa:04')],
         route={'interface': 'enp3s0', 'gateway': '10.20.0.1'},
         sharing={'sessions': [{'name': 'share',
                                'downstream': 'enx001122334455'}]}),
    link({'id': 'mac', 'label': 'Mac mini',
          'vm': {'csm_vm_path': '~/code/csm-vm', 'guest': 'mac-vm'}},
         [port('en0', 'ethernet', 'bb:01', '10.20.0.39/24'),
          port('bridge101', 'bridge', 'bb:02', '192.168.18.1/24')],
         route={'interface': 'en0', 'gateway': '10.20.0.1'},
         info={'platform': 'macos'}),
    link({'id': 'mac-vm', 'label': 'VM', 'host': 'mac'},
         [port('enp0s1', 'ethernet', 'cc:01', '10.11.11.38/24'),
          port('enp0s2', 'ethernet', 'cc:02', '192.168.18.2/24')]),
]


class NetworkMapTest(unittest.TestCase):

    def setUp(self):
        book = DeviceBook()
        book.see('dd:01', '10.20.0.1', 'neighbour', seen_by='local',
                 now_s=1000.0)
        book.see('dd:02', '10.11.11.233', 'lease', name='robot-laptop',
                 now_s=1000.0)
        book.see('cc:01', '10.11.11.38', 'lease', now_s=1000.0)  # the VM
        book.see('dd:03', '10.20.0.77', 'scan', now_s=1000.0)
        devices = book.snapshot(now_s=1001.0)
        self.model = buildNetworkModel(
            LINKS, devices, {'ip.net1.dev_1': '10.20.0.77'},
            {'machine_id': 'local', 'name': 'enp3s0'})
        self.networks = {entry['cidr']: entry
                         for entry in self.model['networks']}

    def test_networks_and_kinds(self):
        self.assertEqual(
            {cidr: entry['kind'] for cidr, entry in self.networks.items()},
            {'10.20.0.0/16': 'lan', '10.11.11.0/24': 'share',
             '192.168.18.0/24': 'host-only'})
        lan = self.networks['10.20.0.0/16']
        self.assertTrue(lan['internet'])
        self.assertEqual(lan['gateway'], '10.20.0.1')
        # The mac's /24 joined the desktop's /16.
        self.assertEqual({member['machine_id'] for member in lan['members']},
                         {'local', 'mac'})
        self.assertEqual(self.networks['10.11.11.0/24']['share'],
                         {'machine_id': 'local', 'port': 'enx001122334455',
                          'name': 'share'})

    def test_ports(self):
        machines = {machine['id']: machine
                    for machine in self.model['machines']}
        local = {entry['name']: entry for entry in machines['local']['ports']}
        self.assertNotIn('docker0', local)
        self.assertEqual(local['enx001122334455']['label'], 'USB Ethernet')
        self.assertTrue(local['enx001122334455']['sharing'])
        self.assertTrue(local['enp3s0']['is_robot_link'])
        self.assertIsNone(local['wlp5s0']['network'])
        self.assertEqual(machines['mac']['kind'], 'mac')
        self.assertEqual(machines['mac-vm']['kind'], 'vm')
        self.assertEqual(machines['mac']['guest'], 'mac-vm')

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

    def test_offline_machines_make_no_networks(self):
        # The VM went offline: its last stats still name 10.11.11.0/24 and
        # 192.168.18.0/24, but only the 192.168 one still has an online
        # member (the mac), and the desktop stopped sharing.
        links = [
            link({'id': 'local', 'label': 'Desktop', 'is_local': True},
                 [port('wlp5s0', 'wifi', 'aa:04', '10.0.0.104/24',
                       wifi={'ssid': 'home'})],
                 route={'interface': 'wlp5s0', 'gateway': '10.0.0.1'}),
            LINKS[1],
            link({'id': 'mac-vm', 'label': 'VM', 'host': 'mac'},
                 [port('enp0s1', 'wifi', 'cc:01', '10.11.11.38/24',
                       wifi={'ssid': 'old'}),
                  port('enp0s2', 'ethernet', 'cc:02', '192.168.18.2/24')],
                 route={'interface': 'enp0s1', 'gateway': '10.11.11.1'},
                 state='offline'),
            link({'id': 'robot', 'label': 'Robot'},
                 [port('eth0', 'ethernet', 'ee:01', '10.11.10.1/24')],
                 sharing={'sessions': [{'name': 'share',
                                        'downstream': 'eth0'}]},
                 state='offline'),
        ]
        book = DeviceBook()
        book.see('dd:09', '10.11.10.3', 'neighbour', now_s=1000.0)
        model = buildNetworkModel(links, book.snapshot(now_s=1001.0), {})
        networks = {entry['cidr']: entry for entry in model['networks']}
        self.assertEqual(set(networks), {'10.0.0.0/24', '10.20.0.0/24',
                                         '192.168.18.0/24'})
        self.assertEqual(networks['10.0.0.0/24']['ssids'], ['home'])
        # The offline VM still hangs from the network its host is on.
        self.assertEqual({member['machine_id'] for member
                          in networks['192.168.18.0/24']['members']},
                         {'mac', 'mac-vm'})
        vm = next(entry for entry in model['machines']
                  if entry['id'] == 'mac-vm')
        self.assertEqual({entry['name']: entry['network']
                          for entry in vm['ports']},
                         {'enp0s1': None, 'enp0s2': '192.168.18.0/24'})
        # A device on a vanished network isn't shown.
        self.assertEqual(model['devices'], [])

    def test_broadcast_isnt_a_device(self):
        book = DeviceBook()
        book.see('ff:ff:ff:ff:ff:ff', '192.168.18.255', 'neighbour')
        book.see('dd:07', '10.11.11.255', 'neighbour')  # odd MAC, still broadcast
        self.assertEqual(len(book.devices), 1)
        model = buildNetworkModel(LINKS, book.snapshot(), {})
        self.assertEqual(model['devices'], [])

    def test_machine_kind(self):
        self.assertEqual(machineKind({'id': 'x', 'icon': 'laptop'}, {}),
                         'laptop')
        self.assertEqual(machineKind({'id': 'robot', 'setup': ['can']}, {}),
                         'robot')
        self.assertEqual(machineKind({'id': 'mochapanda'}, {}), 'board')


if __name__ == '__main__':
    unittest.main()
