"""Local-only host guard and launcher regressions; no real mailbox access."""
import argparse
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import run_local
api = importlib.import_module('webapp.app')


class LocalGuardTests(unittest.TestCase):
    def setUp(self):
        self.previous = api.app.config['LOCAL_ONLY']
        api.app.config.update(TESTING=True, LOCAL_ONLY=True)
        self.client = api.app.test_client()

    def tearDown(self):
        api.app.config['LOCAL_ONLY'] = self.previous
        with api.LOCK:
            for job in api.JOBS.values():
                job['cancel'].set()
            api.JOBS.clear()
            api.ATTEMPTS.clear()

    def test_local_http_session_demo_and_download(self):
        response = self.client.get('/api/session', base_url='http://127.0.0.1:8765')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['runtime_mode'], 'local')
        self.assertNotIn('Secure;', response.headers['Set-Cookie'])
        headers = {'X-CSRF-Token': response.json['csrf_token'], 'Origin': 'http://127.0.0.1:8765'}
        result = self.client.post('/api/demo', base_url='http://127.0.0.1:8765', headers=headers)
        self.assertEqual(result.status_code, 202)
        job_id = result.json['id']
        result = self.client.get('/api/jobs/' + job_id, base_url='http://127.0.0.1:8765')
        file_id = result.json['files'][0]['id']
        response = self.client.post('/api/jobs/' + job_id + '/download',
                    base_url='http://127.0.0.1:8765', json={'ids': [file_id]}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b'PK'))
        response.close()

    def test_remote_peer_and_rebinding_host_rejected(self):
        for url, peer in [('http://evil.example', '127.0.0.1'),
                          ('http://127.0.0.1:8765', '192.168.1.2'),
                          ('http://localhost:8765', '203.0.113.2')]:
            response = self.client.get('/api/session', base_url=url, environ_overrides={'REMOTE_ADDR': peer})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn('Set-Cookie', response.headers)

    def test_local_origin_checks_remain_enabled(self):
        token = self.client.get('/api/session').json['csrf_token']
        response = self.client.post('/api/demo', headers={'X-CSRF-Token':token, 'Origin':'https://evil.example'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(api.JOBS)

    def test_forwarded_headers_cannot_turn_remote_peer_into_local(self):
        response = self.client.get('/api/session', base_url='http://127.0.0.1:8765',
                    environ_overrides={'REMOTE_ADDR':'192.168.1.2'},
                    headers={'X-Forwarded-For':'127.0.0.1', 'X-Forwarded-Host':'localhost', 'X-Forwarded-Proto':'https'})
        self.assertEqual(response.status_code, 403)

    def test_local_launch_ignores_inherited_render_proxy_trust(self):
        # Fresh interpreter reflects the actual launcher environment, not module caches.
        code = """
from webapp.app import app, HOSTED_PROXY
assert not HOSTED_PROXY
assert not app.config['SESSION_COOKIE_SECURE']
c = app.test_client()
r = c.get('/api/session', base_url='http://localhost', headers={'X-Forwarded-Proto':'https'}, environ_overrides={'REMOTE_ADDR':'192.168.1.2'})
assert r.status_code == 403
print('local-isolated')
"""
        env = dict(os.environ, INVOICE_LOCAL_ONLY='1', RENDER='true')
        result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, check=True)
        self.assertIn('local-isolated', result.stdout)


class LauncherTests(unittest.TestCase):
    def test_port_is_unprivileged_and_bounded(self):
        self.assertEqual(run_local.port_number('8765'), 8765)
        for value in ['0', '80', '65536', 'anything', '127.0.0.1:8765']:
            with self.assertRaises(argparse.ArgumentTypeError):
                run_local.port_number(value)

    def test_launcher_has_no_public_host_flag(self):
        with self.assertRaises(SystemExit) as result:
            run_local.main(['--host', '0.0.0.0'])
        self.assertNotEqual(result.exception.code, 0)

    def test_no_automatic_install_without_interactive_consent(self):
        with patch.object(run_local, 'dependencies_ready', return_value=False), \
             patch.object(sys.stdin, 'isatty', return_value=False), \
             patch.object(run_local, 'install') as install:
            self.assertEqual(run_local.main([]), 1)
            install.assert_not_called()

    def test_subprocess_receives_only_loopback_launcher_and_mode(self):
        with patch.object(run_local, 'dependencies_ready', return_value=True), \
             patch.object(run_local.subprocess, 'run') as start:
            start.return_value.returncode = 0
            self.assertEqual(run_local.main(['--no-browser', '--port', '8766']), 0)
            args, kwargs = start.call_args
            self.assertEqual(args[0][1:], ['-m', 'webapp.local_server', '--port', '8766', '--no-browser'])
            self.assertEqual(kwargs['env']['INVOICE_LOCAL_ONLY'], '1')
            self.assertEqual(kwargs['cwd'], run_local.ROOT)


if __name__ == '__main__':
    unittest.main()
