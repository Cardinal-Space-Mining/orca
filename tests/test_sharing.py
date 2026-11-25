'''Internet sharing: the root helper's checks and the pieces it builds, and
how Orca suggests subnets and lists devices.'''

import importlib.util
import ipaddress
import os
import unittest

from backend.sharing import HELPER_SOURCE_PATH, devicesOn, suggestSubnet


def loadHelper():
    spec = importlib.util.spec_from_file_location('share_helper',
                                                  HELPER_SOURCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = loadHelper()


class HelperTest(unittest.TestCase):

    def test_dhcp_range(self):
        server, first, last = helper.dhcpRange(
            ipaddress.ip_network('10.11.11.0/24'))
        self.assertEqual((str(server), str(first), str(last)),
                         ('10.11.11.1', '10.11.11.30', '10.11.11.254'))
        server, first, last = helper.dhcpRange(
            ipaddress.ip_network('10.0.0.0/29'))
        self.assertEqual((str(server), str(first), str(last)),
                         ('10.0.0.1', '10.0.0.2', '10.0.0.6'))

    def test_every_rule_is_tagged(self):
        rules = helper.firewallRules('pits', 'enx0', None, '10.11.11.0/24')
        for _, _, rule in rules:
            self.assertIn(helper.TAG + 'pits', rule)
        nat = [rule for table, _, rule in rules if table == 'nat'][0]
        self.assertNotIn('-o', nat)  # follows whatever has internet
        with_upstream = helper.firewallRules('pits', 'enx0', 'wlan0',
                                             '10.11.11.0/24')
        self.assertIn('wlan0', with_upstream[0][2])

    def test_rejects_bad_input(self):
        checks = [
            (helper.checkName, 'Pits!'),
            (helper.checkName, 'a' * 21),
            (helper.checkLease, '12 hours'),
            (helper.checkDns, '8.8.8.8,not-an-ip'),
            (helper.checkInterface, 'eth0; rm -rf /'),
        ]
        for check, value in checks:
            with self.subTest(value=value), \
                    self.assertRaises(helper.ShareError):
                if check is helper.checkInterface:
                    check(value, 'downstream')
                else:
                    check(value)
        self.assertEqual(helper.checkDns('1.1.1.1, 8.8.8.8'),
                         ['1.1.1.1', '8.8.8.8'])

    @unittest.skipUnless(os.path.exists('/sbin/ip')
                         or os.path.exists('/usr/sbin/ip'), 'needs ip')
    def test_subnet_must_be_private_and_sized(self):
        for subnet in ('8.8.8.0/24', '10.0.0.0/8', '10.0.0.0/30',
                       '10.11.11.1/24'):
            with self.subTest(subnet), self.assertRaises(helper.ShareError):
                helper.checkSubnet(subnet, 'nothing')


class OrcaSideTest(unittest.TestCase):

    def test_suggest_subnet_avoids_networks_in_use(self):
        ports = [{'name': 'eth0', 'ipv4': ['10.11.11.40/24']},
                 {'name': 'usb0', 'ipv4': ['10.11.12.1/24']}]
        self.assertEqual(suggestSubnet(ports), '10.11.13.0/24')
        # The port being shared doesn't count.
        self.assertEqual(suggestSubnet(ports, downstream='usb0'),
                         '10.11.12.0/24')

    def test_devices_merge_leases_and_neighbours(self):
        session = {
            'leases': [{'mac': 'aa', 'address': '10.11.11.40',
                        'hostname': 'robot', 'expires_s': 100}],
            'neighbours': [
                {'mac': 'aa', 'address': '10.11.11.40', 'state': 'reachable'},
                {'mac': 'bb', 'address': '10.11.11.3', 'state': 'stale'},
            ],
        }
        devices = devicesOn(session)
        self.assertEqual([device['mac'] for device in devices], ['bb', 'aa'])
        self.assertEqual(devices[1]['source'], 'lease')
        self.assertEqual(devices[1]['state'], 'reachable')
        self.assertEqual(devices[0]['source'], 'neighbour')


if __name__ == '__main__':
    unittest.main()
