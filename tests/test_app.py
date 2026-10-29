import io
import time
import unittest
import zipfile
from unittest.mock import patch
from email.message import EmailMessage
from datetime import date
from web.app import create_app
from web.mailbox import scan_mailbox, validate_input, safe_filename, MailboxError

class FakeIMAP:
    capabilities = (b'IMAP4rev1', b'ID')
    instances = []
    def __init__(self, host, port, **kwargs):
        self.host = host; self.port = port; self.calls = []; self.instances.append(self)
        m = EmailMessage(); m['Subject'] = '电子发票'; m['From'] = '开票 <seller@example.com>'
        m['Date'] = 'Wed, 28 Oct 2026 10:00:00 +0800'; m.set_content('您的发票已开具 https://127.0.0.1/private')
        m.add_attachment(b'%PDF-1.7\nTest pdf\n%%EOF', maintype='application', subtype='pdf', filename='../../发票.pdf')
        self.message = m.as_bytes()
    def login(self, a, c): self.calls.append(('login',))
    def _simple_command(self, *a): self.calls.append(a); return 'OK', []
    def _untagged_response(self, *a): return 'OK', []
    def select(self, box, readonly=False): self.calls.append(('select',box,readonly)); return 'OK', [b'2']
    def uid(self, *args):
        self.calls.append(args)
        if args[0] == 'search': return 'OK', [b'1 2']
        if args[2] == '(RFC822.SIZE)': return 'OK', [f'1 (RFC822.SIZE {len(self.message)})'.encode()]
        return 'OK', [(b'1 BODY[]', self.message), b')']
    def logout(self): self.calls.append(('logout',))

class MailboxTests(unittest.TestCase):
    def test_readonly_dedup_and_163_id(self):
        result, blobs = scan_mailbox('163','me@163.com','abc123456789',date(2026,10,1),date(2026,10,28),'发票',FakeIMAP)
        self.assertEqual(result['scanned'],2); self.assertEqual(len(blobs),1)
        self.assertEqual(result['link_only'], 0)
        calls=FakeIMAP.instances[-1].calls
        self.assertIn(('select','INBOX',True),calls)
        self.assertTrue(any(c[0]=='ID' for c in calls))
        self.assertTrue(any('(BODY.PEEK[])' in c for c in calls))
        self.assertEqual(FakeIMAP.instances[-1].host,'imap.163.com')
        self.assertNotIn('/',result['items'][0]['name'])
    def test_links_are_counted_not_followed(self):
        class LinksIMAP(FakeIMAP):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                m = EmailMessage(); m['Subject'] = '发票下载'; m.set_content('请访问 https://127.0.0.1/secret')
                self.message = m.as_bytes()
        result, blobs = scan_mailbox('qq','me@qq.com','abc123456789',date(2026,10,1),date(2026,10,28),'发票',LinksIMAP)
        self.assertEqual(result['link_only'], 2)
        self.assertEqual(blobs, {})
        self.assertEqual(LinksIMAP.instances[-1].host, 'imap.qq.com')
    def test_non_pdf_payload_is_rejected(self):
        class InvalidPDF(FakeIMAP):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.message = self.message.replace(b'JVBERi0xLjc', b'PGh0bWw+eHg')
        result, blobs = scan_mailbox('qq','me@qq.com','abc123456789',date(2026,10,1),date(2026,10,28),'发票',InvalidPDF)
        self.assertEqual(blobs, {})
        self.assertGreater(result['skipped'], 0)
    def test_oversized_mail_is_not_fetched(self):
        class HugeIMAP(FakeIMAP):
            def uid(self, *args):
                if args[0] == 'fetch' and args[2] == '(RFC822.SIZE)':
                    self.calls.append(args)
                    return 'OK', [b'1 (RFC822.SIZE 999999999)']
                return super().uid(*args)
        result, blobs = scan_mailbox('qq','me@qq.com','abc123456789',date(2026,10,1),date(2026,10,28),'发票',HugeIMAP)
        self.assertEqual(blobs, {})
        self.assertEqual(result['skipped'], 2)
        self.assertFalse(any('(BODY.PEEK[])' in c for c in HugeIMAP.instances[-1].calls))
    def test_validation(self):
        valid=dict(provider='qq',email='a@qq.com',auth_code='abcdefghijklmnop',start='2026-10-01',end='2026-10-28',consent=True)
        self.assertEqual(validate_input(valid)[0],'qq')
        for key,value in [('provider','custom'),('provider',[]),('email','a@evil.test'),('auth_code','a\r\nbcdefgh'),('start','2020-01-01'),('consent',False)]:
            with self.subTest(key=key),self.assertRaises(MailboxError): validate_input(dict(valid,**{key:value}))
    def test_filename(self):
        self.assertNotIn('\\',safe_filename('..\\evil.pdf'))
        self.assertTrue(safe_filename('\x00').endswith('.pdf'))

class APITests(unittest.TestCase):
    def setUp(self):
        self.app=create_app({'TESTING':True,'APP_ACCESS_CODE':'test-access-code-12345'})
        self.client=self.app.test_client()
        self.token=self.client.get('/api/session').json['csrf']
        self.headers={'X-CSRF-Token':self.token}
    def login(self, client=None, token=None):
        return (client or self.client).post('/api/login',json={'access_code':'test-access-code-12345'},headers={'X-CSRF-Token':token or self.token})
    def search(self):
        with patch('web.app.scan_mailbox',return_value=({'items':[{'id':'1','name':'a.pdf','size':8,'subject':'发票','date':'','sender':''}], 'scanned':1,'matched':1,'skipped':0,'link_only':0,'warning':''},{'1':b'%PDF-1.7'})):
            return self.client.post('/api/search',json={'provider':'qq','email':'a@qq.com','auth_code':'abcdefghijklmnop','start':'2026-10-01','end':'2026-10-28','consent':True},headers=self.headers)
    def test_gate_csrf_and_origin(self):
        self.assertEqual(self.search().status_code,401)
        self.assertEqual(self.client.post('/api/login',json={}).status_code,403)
        self.assertEqual(self.client.post('/api/login',json={},headers={**self.headers,'Origin':'https://evil.test'}).status_code,403)
        self.assertEqual(self.login().status_code,200)
    def test_download_and_clear(self):
        self.login(); result=self.search(); self.assertEqual(result.status_code,200)
        job=result.json['job_id']
        download=self.client.post('/api/download',json={'job_id':job,'item_ids':['1']},headers=self.headers)
        self.assertEqual(download.status_code,200)
        archive=zipfile.ZipFile(io.BytesIO(download.data)); self.assertEqual(archive.read('001_a.pdf'),b'%PDF-1.7')
        self.assertIn('no-store',download.headers['Cache-Control'])
        self.assertEqual(self.client.delete('/api/results/'+job,headers=self.headers).status_code,200)
        self.assertEqual(self.client.post('/api/download',json={'job_id':job,'item_ids':['1']},headers=self.headers).status_code,410)
    def test_isolation(self):
        self.login(); result=self.search().json
        other=self.app.test_client(); token=other.get('/api/session').json['csrf']; self.login(other,token)
        self.assertEqual(other.post('/api/download',json={'job_id':result['job_id'],'item_ids':['1']},headers={'X-CSRF-Token':token}).status_code,410)
    def test_expiry_logout(self):
        self.login(); self.search()
        for s in self.app.extensions['invoice_sessions'].values(): s['job']['expires']=0
        self.client.get('/api/session')
        self.assertTrue(all(s['job'] is None for s in self.app.extensions['invoice_sessions'].values()))
        self.client.post('/api/logout',headers=self.headers)
        self.assertEqual(len(self.app.extensions['invoice_sessions']),0)
    def test_closed_when_unconfigured(self):
        app=create_app({'TESTING':True,'APP_ACCESS_CODE':''}); client=app.test_client(); token=client.get('/api/session').json['csrf']
        self.assertEqual(client.post('/api/login',json={'access_code':'anything'},headers={'X-CSRF-Token':token}).status_code,503)
    def test_invalid_json_and_unicode(self):
        self.assertEqual(self.client.post('/api/login',json=[],headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/api/login',json={'access_code':'测试口令'},headers=self.headers).status_code,401)
        self.assertEqual(self.client.post('/api/login',json={},headers={'X-CSRF-Token':'中文'}).status_code,403)
        self.login()
        self.assertEqual(self.client.post('/api/download',json=[],headers=self.headers).status_code,400)
    def test_malformed_provider_and_pdf_selection(self):
        self.login()
        self.assertEqual(self.client.post('/api/search',json={'provider':[]},headers=self.headers).status_code,400)
        result=self.search().json
        self.assertEqual(self.client.post('/api/download',json={'job_id':result['job_id'],'item_ids':['../../etc/passwd']},headers=self.headers).status_code,400)
    def test_secrets_not_in_results_or_sessions(self):
        self.login(); result=self.search()
        self.assertNotIn(b'abcdefghijklmnop',result.data)
        self.assertNotIn('abcdefghijklmnop',repr(self.app.extensions['invoice_sessions']))
    def test_plaintext_public_rejected(self):
        app=create_app({'APP_ACCESS_CODE':'test-access-code-12345'})
        self.assertEqual(app.test_client().get('/api/session',base_url='http://public.example').status_code,403)
        self.assertEqual(app.test_client().get('/healthz',base_url='http://public.example').status_code,200)
    def test_anonymous_pressure_preserves_login_and_results(self):
        self.login(); result = self.search().json
        # Issue cookie-less probes beyond capacity; authenticated data survives.
        for _ in range(220):
            visitor = self.app.test_client()
            self.assertEqual(visitor.get('/api/session').status_code, 200)
        self.assertLessEqual(len(self.app.extensions['invoice_sessions']), 200)
        newcomer = self.app.test_client()
        token = newcomer.get('/api/session').json['csrf']
        self.assertEqual(self.login(newcomer, token).status_code, 200)
        self.assertEqual(self.client.post('/api/download', json={'job_id':result['job_id'],'item_ids':['1']}, headers=self.headers).status_code, 200)
    def test_anonymous_expiry_is_short_login_renews(self):
        state = next(iter(self.app.extensions['invoice_sessions'].values()))
        self.assertLessEqual(state['expires'] - time.monotonic(), 300)
        self.login()
        self.assertGreater(state['expires'] - time.monotonic(), 3500)
    def test_failed_scan_never_leaks_provider_exception(self):
        self.login()
        with patch('web.app.scan_mailbox', side_effect=RuntimeError('secret-auth-code')):
            response = self.client.post('/api/search', json={'provider':'qq','email':'a@qq.com','auth_code':'abcdefghijklmnop','start':'2026-10-01','end':'2026-10-28','consent':True}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(b'secret-auth-code', response.data)
        self.assertFalse(next(iter(self.app.extensions['invoice_sessions'].values()))['busy'])
    def test_rate_limit(self):
        for _ in range(10): self.client.post('/api/login',json={'access_code':'wrong'},headers=self.headers)
        self.assertEqual(self.login().status_code,429)

if __name__=='__main__': unittest.main()
