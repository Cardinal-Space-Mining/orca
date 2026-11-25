'''Scenario placeholders, scenario checks, launch ordering and extra
arguments.'''

import unittest

from backend.runs import PlanError, dependencyOrder, parseExtraArguments
from backend.scenarios import (ScenarioError, checkScenario, fillTemplate,
                               placeholdersIn)


class FillTemplateTest(unittest.TestCase):

    def test_whole_placeholder_keeps_type(self):
        self.assertEqual(fillTemplate('{rate}', {'rate': 5}, {}), 5)

    def test_placeholders_inside_text(self):
        self.assertEqual(
            fillTemplate('host {ip:robot}:{port}', {'port': 7447},
                         {'robot': '10.0.0.2'}),
            'host 10.0.0.2:7447')

    def test_nested_values(self):
        filled = fillTemplate({'a': ['{x}', {'b': '{ip:c}'}]}, {'x': 'y'},
                              {'c': '1.2.3.4'})
        self.assertEqual(filled, {'a': ['y', {'b': '1.2.3.4'}]})

    def test_unknown_placeholders(self):
        with self.assertRaises(ScenarioError):
            fillTemplate('{missing}', {}, {})
        with self.assertRaises(ScenarioError):
            fillTemplate('{ip:nobody}', {}, {})

    def test_placeholders_in(self):
        self.assertEqual(placeholdersIn(['{a}', {'k': '{ip:r} {b}'}]),
                         {('a', None), ('ip', 'r'), ('b', None)})


def scenario(**overrides):
    data = {
        'roles': {'robot': {}, 'client': {}},
        'options': {'world': {}},
        'processes': [
            {'key': 'robot', 'role': 'robot',
             'lance': {'args': {'gz': '{world}',
                                'client': '{ip:client}'}}},
            {'key': 'client', 'role': 'client', 'command': 'true',
             'after': ['robot']},
        ],
    }
    data.update(overrides)
    return data


class CheckScenarioTest(unittest.TestCase):

    def test_valid(self):
        checkScenario('valid', scenario())

    def test_problems(self):
        broken = {
            'duplicate key': [{'key': 'a', 'role': 'robot', 'command': 'x'},
                              {'key': 'a', 'role': 'robot', 'command': 'x'}],
            'unknown role': [{'key': 'a', 'role': 'nobody', 'command': 'x'}],
            'neither lance nor command': [{'key': 'a', 'role': 'robot'}],
            'unknown option': [{'key': 'a', 'role': 'robot',
                                'command': '{nope}'}],
            'unknown address role': [{'key': 'a', 'role': 'robot',
                                      'command': '{ip:nobody}'}],
            'unknown dependency': [{'key': 'a', 'role': 'robot',
                                    'command': 'x', 'after': ['b']}],
        }
        for name, processes in broken.items():
            with self.subTest(name), self.assertRaises(ScenarioError):
                checkScenario(name, scenario(processes=processes))


class OrderAndArgumentsTest(unittest.TestCase):

    def test_dependency_order(self):
        processes = [{'key': 'c', 'after': ['b']},
                     {'key': 'b', 'after': ['a']}, {'key': 'a'}]
        self.assertEqual([p['key'] for p in dependencyOrder(processes)],
                         ['a', 'b', 'c'])

    def test_dependency_cycle(self):
        with self.assertRaises(PlanError):
            dependencyOrder([{'key': 'a', 'after': ['b']},
                             {'key': 'b', 'after': ['a']}])

    def test_extra_arguments(self):
        self.assertEqual(
            parseExtraArguments("foxglove_bridge:=null name:='a b'"),
            {'foxglove_bridge': 'null', 'name': 'a b'})
        self.assertEqual(parseExtraArguments(''), {})
        with self.assertRaises(PlanError):
            parseExtraArguments('not-an-argument')


if __name__ == '__main__':
    unittest.main()
