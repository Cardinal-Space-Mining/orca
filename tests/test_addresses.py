'''Remembering machines' addresses and choosing which to try.'''

import ipaddress
import os
import tempfile
import unittest

from backend.addresses import AddressBook, networksToScan


def interface(name, kind, mac, *cidrs):
    return {'name': name, 'kind': kind, 'mac': mac, 'ipv4': list(cidrs)}


class AddressBookTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, 'addresses.json')
        self.book = AddressBook(self.path)
        self.book.learnInterfaces('vm', [
            interface('enp0s1', 'ethernet', 'aa:00', '10.20.0.82/16'),
            interface('wlx1', 'wifi', 'bb:00', '10.20.0.90/16'),
            interface('enp0s2', 'ethernet', 'cc:00', '192.168.18.2/24'),
            interface('docker0', 'bridge', 'dd:00', '172.17.0.1/16'),
            interface('enp0s3', 'ethernet', 'ee:00', '169.254.1.2/16'),
        ], now_s=1000.0)

    def test_learns_ethernet_and_wifi_only(self):
        self.assertEqual(set(self.book.machines['vm']),
                         {'10.20.0.82', '10.20.0.90', '192.168.18.2'})
        self.assertEqual(self.book.macs('vm'), {'aa:00', 'bb:00', 'cc:00'})

    def test_relearning_the_same_is_not_news(self):
        self.assertFalse(self.book.learnInterfaces('vm', [
            interface('enp0s1', 'ethernet', 'aa:00', '10.20.0.82/16')],
            now_s=2000.0))
        self.assertEqual(self.book.machines['vm']['10.20.0.82']['seen_s'],
                         2000.0)

    def test_candidates_on_link_ethernet_first(self):
        local = [ipaddress.ip_network('10.20.0.0/16')]
        self.assertEqual(self.book.candidates('vm', local, {}),
                         [('10.20.0.82', 'learned'),
                          ('10.20.0.90', 'learned')])

    def test_neighbour_beats_learned_and_excludes(self):
        local = [ipaddress.ip_network('10.20.0.0/16')]
        neighbours = {'10.20.0.200': 'aa:00', '10.20.0.5': 'ff:ff'}
        self.assertEqual(
            self.book.candidates('vm', local, neighbours,
                                 exclude={'10.20.0.82'}),
            [('10.20.0.200', 'neighbour'), ('10.20.0.90', 'learned')])

    def test_off_link_only_if_it_worked_before(self):
        local = [ipaddress.ip_network('10.11.11.0/24')]
        self.assertEqual(self.book.candidates('vm', local, {}), [])
        self.book.learnConnected('vm', '10.20.0.82', now_s=1500.0)
        self.assertEqual(self.book.candidates('vm', local, {}),
                         [('10.20.0.82', 'learned')])

    def test_shared_macs_identify_nobody(self):
        self.book.learnInterfaces('other-vm', [
            interface('enp0s1', 'ethernet', 'aa:00', '10.11.11.48/24')],
            now_s=1000.0)
        self.assertEqual(self.book.macs('vm'), {'bb:00', 'cc:00'})
        self.assertEqual(self.book.macs('other-vm'), set())
        local = [ipaddress.ip_network('10.20.0.0/16')]
        self.assertNotIn(('10.20.0.200', 'neighbour'), self.book.candidates(
            'vm', local, {'10.20.0.200': 'aa:00'}))

    def test_saved_and_loaded(self):
        self.book.save()
        loaded = AddressBook(self.path)
        # Saved long ago (time 1000), so everything was forgotten.
        self.assertEqual(loaded.machines, {})
        self.book.learnInterfaces('vm', [
            interface('enp0s1', 'ethernet', 'aa:00', '10.20.0.82/16')])
        self.book.save()
        self.assertIn('10.20.0.82', AddressBook(self.path).machines['vm'])


class NetworksToScanTest(unittest.TestCase):

    def test_small_networks_whole_big_ones_by_24(self):
        local = [ipaddress.ip_interface('10.11.11.40/24'),
                 ipaddress.ip_interface('10.20.0.40/16')]
        networks = networksToScan(local, ['10.20.3.7', '10.11.11.9',
                                          '192.168.1.4'])
        self.assertEqual([str(network) for network in networks],
                         ['10.11.11.0/24', '10.20.0.0/24', '10.20.3.0/24'])


if __name__ == '__main__':
    unittest.main()
