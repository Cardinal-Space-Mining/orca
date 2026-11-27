'''Planning a scenario: a process that can't be planned stays in the plan
with its error, so the launch page keeps its fields.'''

import types
import unittest

from backend.lance_config import ConfigError
from backend.runs import Planner, customizeScenario


def fakeLink(machine_id, ipv4):
    machine = types.SimpleNamespace(
        id=machine_id, label=machine_id, workspace='~/ws', is_local=False,
        config_addresses={})
    return types.SimpleNamespace(machine=machine, connection=object(), state={
        'connection': {'state': 'online'},
        'workspace': {'has_install': True, 'fingerprint': 'f'},
        'info': {'home': '/home/' + machine_id},
        'stats': {'interfaces': [{'name': 'eth0', 'kind': 'ethernet',
                                  'ipv4': ipv4}]},
    })


class FakeConfig:

    def overlayFor(self, arguments, settings, key, constants):
        for name, value in arguments.items():
            if value == 'missing':
                raise ConfigError("Preset 'missing' not found for " + name)
        return None, {}, {'selected': dict(arguments)}, []


class FakeOrca:

    def __init__(self, robot_ipv4=('10.0.0.2/24',)):
        self.links = [fakeLink('robot', list(robot_ipv4)),
                      fakeLink('client', ['10.0.0.3/24'])]
        self.links_by_id = {link.machine.id: link for link in self.links}

    def referenceFingerprint(self):
        return 'f'

    def lanceConfig(self):
        return FakeConfig()


SCENARIO = {
    'id': 's', 'label': 'S', 'options': {},
    'roles': {'robot': {'default': 'robot'}, 'client': {'default': 'client'}},
    'processes': [
        {'key': 'robot', 'role': 'robot', 'lance': {'args': {}}},
        {'key': 'client', 'role': 'client', 'after': ['robot'],
         'lance': {'args': {'peer': '{ip:robot}'}}},
    ],
}


class PlannerTest(unittest.TestCase):

    def test_bad_extra_argument_keeps_every_process(self):
        plan = Planner(FakeOrca()).plan(SCENARIO, {
            'extra_arguments': {'robot': 'driver:=missing'}})
        self.assertEqual([p['key'] for p in plan['processes']],
                         ['robot', 'client'])
        robot, client = plan['processes']
        self.assertIn('not found', robot['error'])
        self.assertNotIn('error', client)
        self.assertEqual(len(plan['errors']), 1)
        self.assertTrue(plan['errors'][0].startswith('robot: '))

    def test_unclosed_quote_is_an_error_not_a_crash(self):
        plan = Planner(FakeOrca()).plan(SCENARIO, {
            'extra_arguments': {'client': "name:='a"}})
        self.assertIn('client', plan['errors'][0])
        self.assertIn('error', plan['processes'][1])

    def test_missing_address_keeps_its_picker(self):
        plan = Planner(FakeOrca(robot_ipv4=['192.168.9.2/24'])).plan(
            SCENARIO, {})
        client = plan['processes'][1]
        self.assertEqual(client['address_roles'], ['robot'])
        self.assertIn('set the address by hand', client['error'])
        # Set by hand, it plans.
        plan = Planner(FakeOrca(robot_ipv4=['192.168.9.2/24'])).plan(
            SCENARIO, {'addresses': {'client.robot': '192.168.9.2'}})
        self.assertEqual(plan['errors'], [])
        self.assertIn('peer:=192.168.9.2', plan['processes'][1]['display'])

    def test_plan_keeps_its_choices(self):
        request = {'assignment': {'robot': 'client'},
                   'extra_arguments': {'robot': 'a:=b'},
                   'addresses': {'client.robot': '10.0.0.9'}}
        choices = Planner(FakeOrca()).plan(SCENARIO, request)['choices']
        self.assertEqual(choices['assignment'],
                         {'robot': 'client', 'client': 'client'})
        self.assertEqual(choices['extra_arguments'], {'robot': 'a:=b'})
        self.assertEqual(choices['addresses'], {'client.robot': '10.0.0.9'})


class CustomizeTest(unittest.TestCase):

    def test_choices_become_defaults(self):
        data = {'label': 'S',
                'roles': {'robot': {'default': 'robot'}},
                'options': {'arena': {'default': 'ksc',
                                      'choices': {'ksc': 'K', 'ucf': 'U'}}},
                'processes': [
                    {'key': 'robot', 'role': 'robot',
                     'lance': {'args': {'robot': 'sim'}}},
                    {'key': 'tool', 'role': 'robot', 'command': 'ls'}]}
        custom = customizeScenario(data, {
            'assignment': {'robot': 'vm', 'gone': 'x'},
            'options': {'arena': 'ucf'},
            'extra_arguments': {'robot': "foxglove:=null name:='a b'",
                                'tool': '-la'}})
        self.assertEqual(custom['roles'], {'robot': {'default': 'vm'}})
        self.assertEqual(custom['options']['arena']['default'], 'ucf')
        self.assertEqual(custom['processes'][0]['lance']['args'],
                         {'robot': 'sim', 'foxglove': 'null', 'name': 'a b'})
        self.assertEqual(custom['processes'][1]['command'], 'ls -la')
        # The original is untouched.
        self.assertEqual(data['roles']['robot']['default'], 'robot')


if __name__ == '__main__':
    unittest.main()
