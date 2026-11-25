'''
The comm-link bandwidth budget: average use over a window (normally a run)
against the guidebook's 4,000 Kbps, measured two ways:

    interface     byte counters of the robot's link interface (e.g. the VM's
                  Wi-Fi adapter): everything on the link, including TCP/IP,
                  zenoh and Orca's own traffic. What the limit is
                  about.
    net_adapter   the payload net_adapter reports (NetBandwidth): what the
                  robot code sends, with a per-topic breakdown, but without
                  protocol overhead.
'''

from __future__ import annotations

import asyncio
import collections
import json
import time
from typing import Optional


SOURCES = ('interface', 'net_adapter')
HISTORY_S = 15 * 60
PUBLISH_PERIOD_S = 1.0
MONITOR_PROCESS_ID = 'orca-monitor'
MONITOR_PREFIX = 'ORCA '


class SourceTotals:
    '''Running totals for one source since the window started.'''

    def __init__(self):
        self.bits = 0.0
        self.seconds = 0.0
        self.current_bps = None
        self.peak_bps = 0.0
        self.last_sample_s = None
        self.history = collections.deque()  # (time_s, bits per second)

    def add(self, time_s: float, bits_per_s: float, period_s: float):
        self.bits += bits_per_s * period_s
        self.seconds += period_s
        self.current_bps = bits_per_s
        self.peak_bps = max(self.peak_bps, bits_per_s)
        self.last_sample_s = time_s
        self.history.append((time_s, bits_per_s))
        while self.history and self.history[0][0] < time_s - HISTORY_S:
            self.history.popleft()

    def summary(self) -> dict:
        return {
            'current_kbps': None if self.current_bps is None
            else self.current_bps / 1000.0,
            'average_kbps': self.bits / self.seconds / 1000.0
            if self.seconds else None,
            'peak_kbps': self.peak_bps / 1000.0,
            'seconds': self.seconds,
            'last_sample_s': self.last_sample_s,
        }


class BudgetTracker:

    def __init__(self, orca):
        self.orca = orca
        self.window_start_s = time.time()
        self.totals = {source: SourceTotals() for source in SOURCES}
        self.channels = []       # latest net_adapter per-topic breakdown
        self.latency_ms = None
        self.monitor = {'machine_id': None, 'state': 'stopped', 'error': None}
        self.pending_points = {source: [] for source in SOURCES}
        self.publish_handle = None

    def settings(self) -> dict:
        return self.orca.settings['budget']

    def limitKbps(self) -> float:
        settings = self.settings()
        return settings['limit_kbps'] \
            - settings['num_cameras'] * settings['camera_kbps']

    def reset(self):
        self.window_start_s = time.time()
        self.totals = {source: SourceTotals() for source in SOURCES}
        self.pending_points = {source: [] for source in SOURCES}
        self.orca.publish('budget', self.snapshot(with_history=True))

    # --- Samples ---

    def onStats(self, machine_id: str, stats: dict):
        chosen = self.settings().get('interface') or {}
        if chosen.get('machine_id') != machine_id:
            return
        for entry in stats.get('interfaces', []):
            if entry['name'] == chosen.get('name') \
                    and entry.get('rx_bps') is not None:
                self.add('interface', stats['time_s'],
                         entry['rx_bps'] + entry['tx_bps'],
                         entry['period_s'])

    def onMonitorLines(self, lines: list):
        for line in lines:
            start = line.find(MONITOR_PREFIX)
            if start < 0:
                continue
            try:
                message = json.loads(line[start + len(MONITOR_PREFIX):])
            except ValueError:
                continue
            self.onMonitorMessage(message)

    def onMonitorMessage(self, message: dict):
        if message['type'] == 'ready':
            self.monitor['state'] = 'listening'
            self.schedulePublish()
        elif message['type'] == 'latency':
            self.latency_ms = message['ms']
        elif message['type'] == 'bandwidth':
            # Both endpoints report the same traffic (one's tx is the other's
            # rx); use the client's when both are visible.
            if message['side'] == 'robot' and \
                    self.monitor.get('has_client_side'):
                return
            if message['side'] == 'client':
                self.monitor['has_client_side'] = True
            self.monitor['state'] = 'receiving'
            bits_per_s = 8.0 * (message['tx_bytes_per_s']
                                + message['rx_bytes_per_s'])
            self.add('net_adapter', time.time(), bits_per_s,
                     message['period_s'] or 1.0)
            self.channels = sorted(
                ({'topic': channel['topic'],
                  'direction': 'to robot' if channel['tx'] ==
                  (message['side'] == 'client') else 'from robot',
                  'kbps': channel['bytes_per_s'] * 8.0 / 1000.0,
                  'msgs_per_s': channel['msgs_per_s']}
                 for channel in message['channels']),
                key=lambda channel: channel['kbps'], reverse=True)

    def add(self, source: str, time_s: float, bits_per_s: float,
            period_s: float):
        self.totals[source].add(time_s, bits_per_s, period_s)
        self.pending_points[source].append([time_s, bits_per_s / 1000.0])
        self.schedulePublish()

    # --- Publishing ---

    def schedulePublish(self):
        if self.publish_handle is None:
            self.publish_handle = asyncio.get_running_loop().call_later(
                PUBLISH_PERIOD_S, self.publish)

    def publish(self):
        self.publish_handle = None
        snapshot = self.snapshot(with_history=False)
        snapshot['new_points'] = self.pending_points
        self.pending_points = {source: [] for source in SOURCES}
        self.orca.publish('budget', snapshot)

    def snapshot(self, with_history: bool = True) -> dict:
        snapshot = {
            'settings': self.settings(),
            'limit_kbps': self.limitKbps(),
            'window_start_s': self.window_start_s,
            'sources': {source: totals.summary()
                        for source, totals in self.totals.items()},
            'channels': self.channels,
            'latency_ms': self.latency_ms,
            'monitor': self.monitor,
        }
        if with_history:
            snapshot['history'] = {
                source: [[time_s, bits / 1000.0]
                         for time_s, bits in totals.history]
                for source, totals in self.totals.items()}
        return snapshot

    # --- The net_adapter monitor ---

    def setMonitorState(self, machine_id: Optional[str], state: str,
                        error: Optional[str] = None):
        self.monitor = {'machine_id': machine_id, 'state': state,
                        'error': error}
        self.schedulePublish()
