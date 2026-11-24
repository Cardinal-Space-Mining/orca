#!/usr/bin/env python3
'''
net_adapter's own traffic and latency, for the orchestrator's bandwidth budget.

The orchestrator copies this file to ~/.orchestrator/ and runs it (with ROS and
the workspace sourced, and the same DDS settings as `run.sh --local`) on a
machine that runs a net_adapter endpoint. It prints one line per message,
"ORCHESTRATOR <json>", which the orchestrator picks out of the process's
output.
'''

import json

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import Int32

from net_adapter.msg import NetBandwidth


PREFIX = 'ORCHESTRATOR '
# net_adapter publishes these best-effort; a reliable subscription would be
# incompatible and get nothing.
QOS = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)


class Monitor(Node):

    def __init__(self):
        super().__init__('orchestrator_monitor')
        for side in ('robot', 'client'):
            self.create_subscription(
                NetBandwidth, '/lance/net_bandwidth/' + side,
                lambda message, side=side: self.onBandwidth(side, message),
                QOS)
        self.create_subscription(Int32, '/lance/net_latency',
                                 self.onLatency, QOS)
        self.emit({'type': 'ready'})

    @staticmethod
    def emit(data):
        print(PREFIX + json.dumps(data, separators=(',', ':')), flush=True)

    def onBandwidth(self, side, message):
        self.emit({
            'type': 'bandwidth',
            'side': side,
            'period_s': message.period_s,
            'tx_bytes_per_s': message.tx_bytes_per_s,
            'rx_bytes_per_s': message.rx_bytes_per_s,
            'channels': [{
                'topic': channel.topic,
                'tx': channel.tx,
                'bytes_per_s': channel.bytes_per_s,
                'msgs_per_s': channel.msgs_per_s,
            } for channel in message.channels],
        })

    def onLatency(self, message):
        self.emit({'type': 'latency', 'ms': message.data})


def main():
    rclpy.init()
    node = Monitor()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
