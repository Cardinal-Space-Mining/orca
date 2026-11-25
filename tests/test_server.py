'''The web server's refusal of requests made by other web pages.'''

import unittest

from backend.server import Request, foreignRequest, hostName


def request(method='POST', **headers):
    headers = {name.replace('_', '-'): value
               for name, value in headers.items()}
    headers.setdefault('host', 'localhost:8740')
    if method == 'POST':
        headers.setdefault('content-type', 'application/json')
    return Request(method, '/api/runs', {}, headers, b'{}')


class ForeignRequestTest(unittest.TestCase):

    def test_host_names(self):
        self.assertEqual(hostName('localhost:8740'), 'localhost')
        self.assertEqual(hostName('[::1]:8740'), '::1')
        self.assertEqual(hostName('127.0.0.1'), '127.0.0.1')
        self.assertEqual(hostName('Evil.Example:80'), 'evil.example')

    def test_own_page_is_allowed(self):
        self.assertIsNone(foreignRequest(request(
            origin='http://localhost:8740'), True))
        self.assertIsNone(foreignRequest(request(
            host='[::1]:8740', origin='http://[::1]:8740'), True))
        self.assertIsNone(foreignRequest(request('GET'), True))

    def test_other_origin_is_refused(self):
        self.assertIsNotNone(foreignRequest(request(
            origin='https://example.com'), True))
        self.assertIsNotNone(foreignRequest(request(origin='null'), True))
        self.assertIsNotNone(foreignRequest(request(
            'GET', origin='http://localhost:9999'), True))

    def test_form_posts_are_refused(self):
        self.assertIsNotNone(foreignRequest(request(
            content_type='text/plain'), True))
        self.assertIsNotNone(foreignRequest(request(
            content_type='application/x-www-form-urlencoded'), True))

    def test_rebound_host_is_refused_on_localhost_only(self):
        rebound = request('GET', host='attacker.example:8740')
        self.assertIsNotNone(foreignRequest(rebound, True))
        # Served on a LAN address on purpose: any Host is fine.
        self.assertIsNone(foreignRequest(rebound, False))


if __name__ == '__main__':
    unittest.main()
