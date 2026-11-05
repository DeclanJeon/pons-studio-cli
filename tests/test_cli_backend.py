"""Trusted terminal sessions exercise the real HTTP surface."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_core as core
from pons_cli.backend import connect, read_endpoint
from pons_cli.client import Client
from server.store import Problem


class SessionHTTPTests(unittest.TestCase):
    setUp = core.HTTPTests.setUp
    tearDown = core.HTTPTests.tearDown
    call = core.HTTPTests.call

    def test_exchange_authenticates_cli_without_browser_cookie(self):
        # Given
        client = Client(f'http://127.0.0.1:{self.port}')
        # When
        connected = client.authenticate(self.app.desktop.token)
        projects = connected.request('GET', '/api/projects')
        # Then
        self.assertEqual(projects, [])
        with self.assertRaises(Problem) as error:
            client.request('GET', '/api/projects')
        self.assertEqual(error.exception.status, 401)

    def test_exchange_rejects_wrong_token_and_browser_origin(self):
        # Given / When
        wrong = self.call('/api/cli/session', 'POST', {}, {'X-Pons-Endpoint': 'wrong'}, auth=False)
        browser = self.call('/api/cli/session', 'POST', {}, {'X-Pons-Endpoint': self.app.desktop.token, 'Origin': 'https://evil.example'}, auth=False)
        # Then
        self.assertEqual(wrong[0], 403)
        self.assertEqual(browser[0], 403)

    def test_expired_cli_session_cannot_access_api(self):
        # Given
        client = Client(f'http://127.0.0.1:{self.port}').authenticate(self.app.desktop.token)
        # When / Then
        with patch('server.desktop.time.monotonic', return_value=float('inf')):
            with self.assertRaises(Problem) as error:
                client.request('GET', '/api/projects')
        self.assertEqual(error.exception.status, 401)

    def test_cli_session_must_not_authorize_browser_origin(self):
        # Given
        client = Client(f'http://127.0.0.1:{self.port}').authenticate(self.app.desktop.token)
        # When
        response = self.call('/api/projects', headers={'X-Pons-Session': client.token, 'Origin': 'https://evil.example'}, auth=False)
        # Then
        self.assertEqual(response[0], 403)

    def test_old_backend_is_not_replaced_by_another_worker(self):
        # Given
        self.app.base_url = f'http://127.0.0.1:{self.port}'
        self.app.desktop.publish()
        old = {'name': 'Pons Studio', 'protocol': 1, 'data_id': self.app.desktop.identity, 'busy': False}
        # When / Then
        with patch.object(self.app.desktop, 'health', return_value=old), patch('pons_cli.backend.subprocess.Popen', side_effect=AssertionError('must not spawn another worker')):
            with self.assertRaises(Problem) as error:
                connect(self.app.store.root)
        self.assertEqual(error.exception.status, 409)

    def test_event_log_is_finite_and_resumes_after_sequence(self):
        # Given
        p = self.app.store.create(core.request(), 'events')
        seq = self.app.store.events(p['id'])[-1]['seq']
        self.app.store.event(p['id'], 'progress', 'actual stage')
        client = Client(f'http://127.0.0.1:{self.port}').authenticate(self.app.desktop.token)
        # When
        result = client.request('GET', f'/api/projects/{p["id"]}/event-log?after={seq}')
        # Then
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['kind'], 'progress')


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.record = self.root / 'desktop-endpoint.json'
        import hashlib
        self.info = {'url': 'http://127.0.0.1:8765', 'token': 'private', 'data_id': hashlib.sha256(str(self.root).encode()).hexdigest(), 'protocol': 1, 'pid': os.getpid()}
        self.record.write_text(json.dumps(self.info))
        self.record.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_private_endpoint_is_accepted(self):
        # Given / When
        endpoint = read_endpoint(self.root)
        # Then
        self.assertEqual(endpoint.url, 'http://127.0.0.1:8765')
        self.assertEqual(endpoint.data_id, self.info['data_id'])

    def test_endpoint_rejects_remote_identity_mismatch_and_public_permissions(self):
        # Given / When / Then
        for field, value in [('url', 'https://evil.example'), ('data_id', 'other')]:
            with self.subTest(field=field):
                self.record.write_text(json.dumps({**self.info, field: value}))
                with self.assertRaises(Problem):
                    read_endpoint(self.root)
        self.record.write_text(json.dumps(self.info))
        self.record.chmod(0o644)
        with self.assertRaises(Problem):
            read_endpoint(self.root)

    def test_symlink_endpoint_is_rejected(self):
        # Given
        target = self.root / 'actual'
        self.record.rename(target)
        self.record.symlink_to(target)
        # When / Then
        with self.assertRaises(Problem):
            read_endpoint(self.root)
