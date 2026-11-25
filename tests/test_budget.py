'''The bandwidth budget: averages, and which net_adapter side counts.'''

import unittest

from backend.budget import BudgetTracker
from backend.settings import DEFAULTS


class FakeOrca:

    def __init__(self):
        self.settings = {'budget': dict(DEFAULTS['budget'])}
        self.published = []

    def publish(self, event, data):
        self.published.append((event, data))


def bandwidth(side, tx, rx):
    return {'type': 'bandwidth', 'side': side, 'period_s': 1.0,
            'tx_bytes_per_s': tx, 'rx_bytes_per_s': rx,
            'channels': [{'topic': '/a', 'tx': True, 'bytes_per_s': tx,
                          'msgs_per_s': 10}]}


class BudgetTest(unittest.IsolatedAsyncioTestCase):

    async def test_limit_counts_cameras(self):
        tracker = BudgetTracker(FakeOrca())
        tracker.settings()['num_cameras'] = 2
        self.assertEqual(tracker.limitKbps(), 3000)

    async def test_interface_average(self):
        orca = FakeOrca()
        orca.settings['budget']['interface'] = {
            'machine_id': 'vm', 'name': 'wlan0'}
        tracker = BudgetTracker(orca)
        for time_s, bps in ((1.0, 1000.0), (2.0, 3000.0)):
            tracker.onStats('vm', {'time_s': time_s, 'interfaces': [
                {'name': 'wlan0', 'rx_bps': bps, 'tx_bps': 0.0,
                 'period_s': 1.0}]})
        tracker.onStats('other', {'time_s': 3.0, 'interfaces': [
            {'name': 'wlan0', 'rx_bps': 1e9, 'tx_bps': 0.0,
             'period_s': 1.0}]})
        summary = tracker.snapshot()['sources']['interface']
        self.assertAlmostEqual(summary['average_kbps'], 2.0)
        self.assertAlmostEqual(summary['peak_kbps'], 3.0)

    async def test_client_side_preferred(self):
        tracker = BudgetTracker(FakeOrca())
        tracker.onMonitorLines(
            ['noise', '[monitor] ORCA {"type": "ready"}'])
        self.assertEqual(tracker.monitor['state'], 'listening')
        tracker.onMonitorMessage(bandwidth('client', 100, 0))
        tracker.onMonitorMessage(bandwidth('robot', 999, 0))  # ignored
        summary = tracker.snapshot()['sources']['net_adapter']
        self.assertAlmostEqual(summary['current_kbps'], 0.8)
        self.assertEqual(tracker.channels[0]['direction'], 'to robot')


if __name__ == '__main__':
    unittest.main()
