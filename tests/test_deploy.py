'''Reading rsync's itemized output.'''

import unittest

from backend.deploy import summarizeItemized


class SummarizeItemizedTest(unittest.TestCase):

    def test_counts_code_and_git_separately(self):
        output = '\n'.join([
            '*deleting   src/old.py',
            '>f+++++++++ src/new.py',
            '>f.st...... src/changed.cpp',
            '>f.st...... src/.git/index',
            'cd+++++++++ src/newdir/',
        ])
        summary = summarizeItemized(output)
        self.assertEqual(summary['num_code_new'], 1)
        self.assertEqual(summary['num_code_updated'], 1)
        self.assertEqual(summary['num_code_deleted'], 1)
        self.assertEqual(summary['num_git'], 1)
        self.assertFalse(summary['is_up_to_date'])
        self.assertTrue(summary['changes'][-1]['is_git'])

    def test_only_git_files_is_up_to_date(self):
        summary = summarizeItemized('>f.st...... src/.git/index\n')
        self.assertTrue(summary['is_up_to_date'])


if __name__ == '__main__':
    unittest.main()
