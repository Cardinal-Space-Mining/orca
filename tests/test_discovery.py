'''Turning what a new machine reports into an inventory entry.'''

import ipaddress
import unittest

from backend.discovery import (configAddresses, routeAddresses, scanNetworks,
                               suggestId)


INTERFACES = [
    {'name': 'en1', 'kind': 'wifi', 'ipv4': ['10.20.0.41/16']},
    {'name': 'en0', 'kind': 'ethernet', 'ipv4': ['10.20.0.39/16']},
    {'name': 'bridge101', 'kind': 'bridge', 'ipv4': ['192.168.18.1/24']},
    {'name': 'lo0', 'kind': 'loopback', 'ipv4': ['127.0.0.1/8']},
    {'name': 'usb0', 'kind': 'ethernet', 'ipv4': ['169.254.3.4/16']},
]


class DiscoveryTest(unittest.TestCase):

    def test_suggest_id(self):
        self.assertEqual(suggestId('Team-Mac-mini.local', set()),
                         'team-mac-mini')
        self.assertEqual(suggestId('ubuntu-vm', {'ubuntu-vm'}), 'ubuntu-vm-2')
        self.assertEqual(suggestId(None, set()), 'machine')
        self.assertEqual(suggestId('LattePanda Delta', set()),
                         'lattepanda-delta')

    def test_scan_networks(self):
        # Big networks become the /24 around the machine, listed once,
        # Ethernet first; loopback and link-local are skipped.
        networks = scanNetworks(INTERFACES)
        self.assertEqual([(entry['cidr'], entry['interface'])
                          for entry in networks],
                         [('10.20.0.0/24', 'en0'),
                          ('192.168.18.0/24', 'bridge101')])

    def test_route_addresses(self):
        local = [ipaddress.ip_network('10.20.0.0/16')]
        self.assertEqual(routeAddresses(INTERFACES, '10.20.0.41', local),
                         ['10.20.0.41', '10.20.0.39'])
        self.assertEqual(routeAddresses(INTERFACES, '10.11.11.233', local),
                         ['10.11.11.233', '10.20.0.39', '10.20.0.41'])
        # Nothing this machine can't reach directly.
        self.assertEqual(routeAddresses(INTERFACES, '10.11.11.233', []),
                         ['10.11.11.233'])

    def test_config_addresses(self):
        constants = {'ip.net1.mac_native_eth': '10.20.0.39',
                     'ip.net1.mac_native_wifi': '10.20.0.41',
                     'ip.net1.lidar': '10.11.11.3',
                     'ip.net1.dev_1': '10.20.0.41'}
        self.assertEqual(
            configAddresses(INTERFACES, constants, {'ip.net1.dev_1'}),
            {'ip.net1.mac_native_eth': 'ethernet',
             'ip.net1.mac_native_wifi': 'wifi'})


if __name__ == '__main__':
    unittest.main()
