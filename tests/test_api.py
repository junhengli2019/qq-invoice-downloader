"""Public API regressions; never connects to a real mailbox."""
import importlib
import io
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

api = importlib.import_module('webapp.app')


class ApiTests(unittest.TestCase):
    def setUp(self):
        api.app.config['TESTING'] = True
        with api.LOCK:
            for job in api.JOBS.values():
                job['cancel'].set()
            api.JOBS.clear()
            api.ATTEMPTS.clear()
        self.client = api.app.test_client()
        response = self.client.get('/api/session')
        self.headers = {'X-CSRF-Token': response.json['csrf_token']}

    def tearDown(self):
        with api.LOCK:
            for job in api.JOBS.values():
                job['cancel'].set()
            api.JOBS.clear()
            api.ATTEMPTS.clear()

    def make_demo(self, client=None, headers=None):
        response = (client or self.client).post('/api/demo', headers=headers or self.headers)
        self.assertEqual(response.status_code, 202, response.json)
        return response.json['id']

    @staticmethod
    def valid_form(**updates):
        data = dict(provider='qq', email='example@qq.com', auth_code='not-a-real-code',
                    start_date='2026-10-01', end_date='2026-10-29', consent=True)
        return dict(data, **updates)

    def test_cookie_and_security_headers(self):
        client = api.app.test_client()
        response = client.get('/api/session')
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertNotIn('sid', response.json)

    def test_csrf_and_cross_site_requests_are_rejected(self):
        self.assertEqual(self.client.post('/api/demo').status_code, 403)
        self.assertEqual(self.client.post('/api/demo', headers={'X-CSRF-Token': 'wrong'}).status_code, 403)
        for extra in [{'Origin': 'https://attacker.example'}, {'Sec-Fetch-Site': 'cross-site'}]:
            response = self.client.post('/api/demo', headers=dict(self.headers, **extra))
            self.assertEqual(response.status_code, 403)
        self.assertFalse(api.JOBS)

    def test_cross_browser_access_and_download_denied(self):
        ident = self.make_demo()
        other = api.app.test_client()
        other_headers = {'X-CSRF-Token': other.get('/api/session').json['csrf_token']}
        file_id = self.client.get('/api/jobs/' + ident).json['files'][0]['id']
        self.assertEqual(other.get('/api/jobs/' + ident).status_code, 404)
        self.assertEqual(other.post('/api/jobs/' + ident + '/download',
                                   json={'ids': [file_id]}, headers=other_headers).status_code, 404)
        self.assertEqual(other.delete('/api/jobs/' + ident, headers=other_headers).status_code, 404)
        self.assertEqual(self.client.get('/api/jobs/' + ident).status_code, 200)

    def test_demo_download_contains_only_selected_pdfs(self):
        ident = self.make_demo()
        result = self.client.get('/api/jobs/' + ident).json
        self.assertTrue(result['demo'])
        self.assertEqual(result['status'], 'done')
        self.assertEqual(len(result['files']), 3)
        for item in result['files']:
            self.assertNotIn('data', item)
        self.assertNotIn('owner', result)
        chosen = result['files'][1]
        response = self.client.post('/api/jobs/' + ident + '/download',
                                    json={'ids': [chosen['id'], chosen['id']]}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/zip')
        self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertIn('DEMO_', response.headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), [chosen['name']])
            content = archive.read(chosen['name'])
            self.assertTrue(content.startswith(b'%PDF-'))
            self.assertIn(b'NOT A VALID INVOICE', content)
            self.assertEqual(archive.testzip(), None)
        response.close()

    def test_download_refuses_unknown_or_invalid_selections(self):
        ident = self.make_demo()
        for ids in [[], ['../../secrets'], ['unknown'], [None], 'file-id', ['x'] * 101]:
            response = self.client.post('/api/jobs/' + ident + '/download',
                                       json={'ids': ids}, headers=self.headers)
            self.assertEqual(response.status_code, 400)

    def test_deletion_removes_result_and_download(self):
        ident = self.make_demo()
        file_id = self.client.get('/api/jobs/' + ident).json['files'][0]['id']
        response = self.client.delete('/api/jobs/' + ident, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'ok': True})
        self.assertEqual(self.client.get('/api/jobs/' + ident).status_code, 404)
        self.assertEqual(self.client.post('/api/jobs/' + ident + '/download',
                        json={'ids': [file_id]}, headers=self.headers).status_code, 404)

    def test_expired_results_are_purged_on_next_request(self):
        ident = self.make_demo()
        with api.LOCK:
            job = api.JOBS[ident]
            job['expires_at'] = time.time() - 1
        self.assertEqual(self.client.get('/api/jobs/' + ident).status_code, 404)
        self.assertTrue(job['cancel'].is_set())
        self.assertEqual(job['files'], [])
        self.assertNotIn(ident, api.JOBS)

    def test_single_job_per_browser(self):
        ident = self.make_demo()
        self.assertEqual(self.client.post('/api/demo', headers=self.headers).status_code, 409)
        self.client.delete('/api/jobs/' + ident, headers=self.headers)
        self.make_demo()

    def test_global_job_cap_is_enforced(self):
        for _ in range(api.MAX_JOBS):
            client = api.app.test_client()
            headers = {'X-CSRF-Token': client.get('/api/session').json['csrf_token']}
            self.make_demo(client, headers)
        self.assertEqual(self.client.post('/api/demo', headers=self.headers).status_code, 503)

    def test_rate_limit_survives_deleting_results(self):
        for _ in range(8):
            ident = self.make_demo()
            self.client.delete('/api/jobs/' + ident, headers=self.headers)
        self.assertEqual(self.client.post('/api/demo', headers=self.headers).status_code, 429)

    def test_validation_does_not_connect_to_mailbox(self):
        bad_forms = [
            self.valid_form(provider='custom'), self.valid_form(provider=['qq']),
            self.valid_form(email='example@163.com'), self.valid_form(email='@qq.com'),
            self.valid_form(email='a\r\nb@qq.com'), self.valid_form(auth_code=''),
            self.valid_form(auth_code='pass\nword'), self.valid_form(consent=False),
            self.valid_form(consent='true'), self.valid_form(start_date='invalid'),
            self.valid_form(start_date='2026-10-30'), self.valid_form(start_date='2020-01-01'),
            self.valid_form(start_date='9999-12-31', end_date='9999-12-31'),
        ]
        with patch.object(api.POOL, 'submit') as submit:
            for data in bad_forms:
                response = self.client.post('/api/jobs', json=data, headers=self.headers)
                self.assertEqual(response.status_code, 400, data)
            submit.assert_not_called()
        self.assertFalse(api.JOBS)

    def test_scan_result_publishes_metadata_without_credentials(self):
        result = dict(files=[dict(name='invoice.pdf', data=b'%PDF-test', subject='invoice',
                                  sender='Example', date='2026-10-01', source='附件')],
                      scanned=1, matched=1, manual_links=[], warnings=[])
        with patch.object(api, 'scan_mailbox', return_value=result) as scanner:
            with patch.object(api.POOL, 'submit', side_effect=lambda fn, *args: fn(*args)):
                response = self.client.post('/api/jobs', json=self.valid_form(), headers=self.headers)
        self.assertEqual(response.status_code, 202)
        scanner.assert_called_once()
        status = self.client.get('/api/jobs/' + response.json['id'])
        self.assertEqual(status.json['status'], 'done')
        self.assertEqual(status.json['files'][0]['size'], 9)
        self.assertEqual(status.json['progress']['scanned'], 1)
        self.assertNotIn('not-a-real-code', status.get_data(as_text=True))
        self.assertNotIn('example@qq.com', status.get_data(as_text=True))
        self.assertEqual(api.ACTIVE_SCANS, 0)

    def test_provider_error_cannot_echo_authorization_code(self):
        with patch.object(api, 'scan_mailbox', side_effect=api.MailboxError('failed not-a-real-code')):
            with patch.object(api.POOL, 'submit', side_effect=lambda fn, *args: fn(*args)):
                response = self.client.post('/api/jobs', json=self.valid_form(), headers=self.headers)
        status = self.client.get('/api/jobs/' + response.json['id'])
        self.assertEqual(status.json['status'], 'error')
        self.assertNotIn('not-a-real-code', status.get_data(as_text=True))
        self.assertEqual(api.ACTIVE_SCANS, 0)

    def test_deleting_running_jobs_does_not_bypass_worker_limit(self):
        release = threading.Event()
        entered = threading.Event()
        def blocking_scan(*args):
            entered.set()
            release.wait(5)
            return dict(files=[], scanned=0, matched=0)
        futures = []
        real_submit = api.POOL.submit
        def track_submit(*args):
            future = real_submit(*args)
            futures.append(future)
            return future
        try:
            with patch.object(api, 'scan_mailbox', side_effect=blocking_scan):
                with patch.object(api.POOL, 'submit', side_effect=track_submit):
                    for _ in range(2):
                        response = self.client.post('/api/jobs', json=self.valid_form(), headers=self.headers)
                        self.assertEqual(response.status_code, 202, response.json)
                        self.assertTrue(entered.wait(1))
                        self.client.delete('/api/jobs/' + response.json['id'], headers=self.headers)
                    response = self.client.post('/api/jobs', json=self.valid_form(), headers=self.headers)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(api.ACTIVE_SCANS, 2)
                release.set()
                for future in futures:
                    future.result(timeout=2)
        finally:
            release.set()
            for future in futures:
                future.result(timeout=6)
        self.assertEqual(api.ACTIVE_SCANS, 0)
        self.assertFalse(api.JOBS)

    def test_download_memory_slot_released_only_after_response_closes(self):
        ident = self.make_demo()
        file_id = self.client.get('/api/jobs/' + ident).json['files'][0]['id']
        url = '/api/jobs/' + ident + '/download'
        first = self.client.post(url, json={'ids': [file_id]}, headers=self.headers)
        self.assertEqual(first.status_code, 200)
        second = self.client.post(url, json={'ids': [file_id]}, headers=self.headers)
        self.assertEqual(second.status_code, 503)
        first.close()
        third = self.client.post(url, json={'ids': [file_id]}, headers=self.headers)
        self.assertEqual(third.status_code, 200)
        third.close()

    def test_request_body_is_bounded(self):
        response = self.client.post('/api/jobs', json={'x': 'a' * 9000}, headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertIsInstance(response.json['error'], str)

    def test_private_paths_are_not_served(self):
        for url in ['/app.py', '/mailbox.py', '/.git/config', '/../requirements.txt']:
            self.assertEqual(self.client.get(url).status_code, 404)


if __name__ == '__main__':
    unittest.main()
