import io
import json
import socket
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '发票下载工具/代码'))
import download_invoices as core
import safe_download
from public_app import create_app, validate
from public_jobs import Jobs, RESULT_TTL
from fixtures import FakeMailbox, run_fixture, run_slow

ORIGIN = 'https://trial.example'
SECRET = 'abcdefghijklmnop'


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.jobs = Jobs(Path(self.temp.name) / 'jobs', target=run_fixture, start_cleanup=False)
        self.app = create_app(self.jobs, ORIGIN, probe=False)
        self.app.testing = True
        self.a = self.app.test_client(); self.b = self.app.test_client()
        for client in [self.a, self.b]: client.get('/', base_url=ORIGIN)
        self.headers = {'Origin': ORIGIN, 'X-CSRF-Token': self.a.get('/api/session', base_url=ORIGIN).json['csrf']}
    def tearDown(self):
        self.jobs.close(); self.temp.cleanup()
    def post(self, path, body=None, headers=None):
        return self.a.post(path, base_url=ORIGIN, json=body or {}, headers=headers if headers is not None else self.headers)
    def payload(self):
        return {'email': 'sample@qq.com', 'authorization_code': SECRET,
                'start_date': date.today().isoformat(), 'end_date': date.today().isoformat(), 'consent': True}
    def wait_job(self):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            result = self.a.get('/api/status', base_url=ORIGIN).json
            if result['state'] != 'running': return result
            time.sleep(.02)
        self.fail('Fixture job did not finish')
    def test_mail_to_zip_and_visitors_are_isolated(self):
        self.assertEqual(self.post('/api/start', self.payload()).status_code, 202)
        result = self.wait_job()
        self.assertEqual(result['state'], 'success')
        self.assertEqual(result['summary']['saved_pdfs'], 1)
        response = self.a.get('/api/download', base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(len(archive.namelist()), 1)
            self.assertTrue(archive.read(archive.namelist()[0]).startswith(b'%PDF-'))
        response.close()
        self.assertEqual(self.b.get('/api/status', base_url=ORIGIN).json['state'], 'idle')
        self.assertEqual(self.b.get('/api/download', base_url=ORIGIN).status_code, 404)
        self.assertNotIn(SECRET, json.dumps(result))
        for path in Path(self.temp.name).rglob('*'):
            if path.is_file(): self.assertNotIn(SECRET.encode(), path.read_bytes())
    def test_origin_csrf_and_unknown_cookie_fail_closed(self):
        self.assertEqual(self.post('/api/demo', headers={}).status_code, 403)
        self.assertEqual(self.post('/api/demo', headers={**self.headers, 'Origin':'https://other.example'}).status_code,403)
        self.assertEqual(self.post('/api/demo', headers={**self.headers, 'X-CSRF-Token':'fake'}).status_code,403)
        stranger = self.app.test_client(); stranger.set_cookie('invoice_visitor','fake',domain='trial.example')
        self.assertEqual(stranger.get('/api/status',base_url=ORIGIN).status_code,401)
        self.assertEqual(stranger.get('/api/download',base_url=ORIGIN).status_code,401)
        self.assertFalse(list(self.jobs.root.iterdir()))
    def test_cookie_headers_host_and_request_limit(self):
        response = self.a.get('/',base_url=ORIGIN)
        for attribute in ['HttpOnly','Secure','SameSite=Lax']:self.assertIn(attribute,response.headers['Set-Cookie'])
        self.assertNotIn('unsafe-inline',response.headers['Content-Security-Policy'])
        self.assertEqual(self.a.get('/',base_url='https://evil.example').status_code,400)
        self.assertEqual(self.post('/api/start',{'x':'a'*9000}).status_code,413)
    def test_validation_and_failure_do_not_publish_partial_files(self):
        self.assertEqual(self.post('/api/start',{**self.payload(),'consent':False}).status_code,400)
        self.assertEqual(self.post('/api/start',{**self.payload(),'authorization_code':'qq-password'}).status_code,400)
        self.assertEqual(self.post('/api/start',{**self.payload(),'email':'fail@qq.com'}).status_code,202)
        self.assertEqual(self.wait_job()['state'],'error')
        self.assertEqual(self.a.get('/api/download',base_url=ORIGIN).status_code,404)
        self.assertFalse(any(p.is_file() for p in self.jobs.root.rglob('*')))
    def test_expiry_and_explicit_deletion(self):
        self.post('/api/demo'); self.assertEqual(self.wait_job()['state'],'success')
        _, session=self.jobs.session(self.a.get_cookie('invoice_visitor',domain='trial.example').value)
        session.job.finished-=RESULT_TTL+1
        self.assertEqual(self.a.get('/api/download',base_url=ORIGIN).status_code,404)
        self.assertFalse(list(self.jobs.root.iterdir()))
        self.post('/api/demo');self.wait_job();self.assertEqual(self.post('/api/clear').status_code,200)
        self.assertFalse(list(self.jobs.root.iterdir()))
    def test_cancel_actually_stops_worker(self):
        self.jobs.target=run_slow;self.post('/api/start',self.payload())
        _, session=self.jobs.session(self.a.get_cookie('invoice_visitor',domain='trial.example').value)
        process=session.job.process
        self.assertTrue(process.is_alive());self.post('/api/clear')
        self.assertFalse(process.is_alive());self.assertFalse(list(self.jobs.root.iterdir()))
    def test_global_capacity_and_duplicate_start(self):
        self.jobs.target=run_slow;self.assertEqual(self.post('/api/demo').status_code,202)
        self.assertEqual(self.post('/api/demo').status_code,429)
        csrf=self.b.get('/api/session',base_url=ORIGIN).json['csrf']
        self.assertEqual(self.b.post('/api/demo',base_url=ORIGIN,json={},headers={'Origin':ORIGIN,'X-CSRF-Token':csrf}).status_code,202)
        c=self.app.test_client();c.get('/',base_url=ORIGIN);csrf=c.get('/api/session',base_url=ORIGIN).json['csrf']
        self.assertEqual(c.post('/api/demo',base_url=ORIGIN,json={},headers={'Origin':ORIGIN,'X-CSRF-Token':csrf}).status_code,429)


class CoreTests(unittest.TestCase):
    def test_readonly_peek_and_limits(self):
        with tempfile.TemporaryDirectory() as path, patch.object(core.imaplib,'IMAP4_SSL',FakeMailbox):
            result=core.download_invoices('sample@qq.com',SECRET,date.today(),date.today(),Path(path),limits=core.DownloadLimits())
            self.assertEqual(result.saved_pdfs,1)
            calls=FakeMailbox.instances[-1].calls
            self.assertIn(('select','INBOX',True),calls)
            self.assertTrue(any(call[0]=='fetch' and 'BODY.PEEK' in call[1] for call in calls))
            self.assertEqual(calls[-1],('logout',))
            self.assertNotIn(SECRET,json.dumps(result.to_dict()))
        with tempfile.TemporaryDirectory() as path, patch.object(core.imaplib,'IMAP4_SSL',FakeMailbox):
            with self.assertRaises(core.DownloadError):
                core.download_invoices('sample@qq.com',SECRET,date.today(),date.today(),Path(path),limits=core.DownloadLimits(max_messages=0))
            self.assertFalse(list(Path(path).iterdir()))
        with tempfile.TemporaryDirectory() as path, patch.object(core.imaplib,'IMAP4_SSL',FakeMailbox):
            with self.assertRaises(core.DownloadError):
                core.download_invoices('sample@qq.com',SECRET,date.today(),date.today(),Path(path),limits=core.DownloadLimits(max_output_bytes=1))
    def test_link_security_rejects_private_and_dangerous_schemes(self):
        for url in ['file:///etc/passwd','http://user:pass@example.com/x','https://example.com:1234/x']:
            with self.assertRaises(safe_download.UnsafeDownload): safe_download.resolve_public(url)
        for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:127.0.0.1','100.64.0.1','224.0.0.1']:
            answers=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]
            with patch.object(socket,'getaddrinfo',return_value=answers),self.assertRaises(safe_download.UnsafeDownload):
                safe_download.resolve_public('https://example.com/a.pdf')
    def test_download_connects_to_validated_address_and_rechecks_redirect(self):
        answers=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.215.14',80))]
        with patch.object(socket,'getaddrinfo',return_value=answers) as dns,patch.object(socket,'socket') as sock,patch.object(safe_download.http.client,'HTTPConnection') as conn:
            response=conn.return_value.getresponse.return_value;response.status=200
            response.getheader.return_value='6';response.read.return_value=b'%PDF-x';response.headers={}
            data,_,_=safe_download.fetch_public('http://example.com/a.pdf',1024)
            self.assertEqual(data,b'%PDF-x');self.assertEqual(dns.call_count,1)
            sock.return_value.connect.assert_called_once_with(('93.184.215.14',80))
        with patch.object(socket,'getaddrinfo',side_effect=[answers,[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',80))]]),patch.object(socket,'socket'),patch.object(safe_download.http.client,'HTTPConnection') as conn:
            response=conn.return_value.getresponse.return_value;response.status=302;response.getheader.return_value='http://internal.example/secret'
            with self.assertRaises(safe_download.UnsafeDownload):safe_download.fetch_public('http://example.com/a.pdf',1024)
            self.assertEqual(conn.call_count,1)

    def test_oversized_mail_is_not_fetched(self):
        class HugeMail(FakeMailbox):
            def fetch(self, mid, query):
                self.calls.append(('fetch', query))
                if query == '(RFC822.SIZE)': return 'OK', [b'1 (RFC822.SIZE 999999999)']
                raise AssertionError('An oversized mail must never be downloaded')
        with tempfile.TemporaryDirectory() as path, patch.object(core.imaplib,'IMAP4_SSL',HugeMail):
            with self.assertRaises(core.DownloadError):
                core.download_invoices('sample@qq.com',SECRET,date.today(),date.today(),Path(path),limits=core.DownloadLimits())
            self.assertFalse(list(Path(path).iterdir()))

if __name__=='__main__':unittest.main()
