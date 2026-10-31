"""Synthetic mailbox for tests only; never imported by the public application."""
import imaplib
import time
from datetime import date
from email.message import EmailMessage
from email.utils import formatdate
from unittest.mock import patch

import download_invoices as core
from public_jobs import demo_pdf, run_job


def message():
    mail = EmailMessage()
    mail['Subject'] = '测试专用电子发票'
    mail['From'] = 'Receipt <receipts@example.com>'
    mail['To'] = 'sample@qq.com'
    mail['Date'] = formatdate(localtime=True)
    mail.set_content('这是一封只用于自动化测试的发票邮件。')
    mail.add_attachment(demo_pdf('Synthetic test'), maintype='application', subtype='pdf', filename='测试发票.pdf')
    return mail.as_bytes()


class FakeMailbox:
    instances = []
    def __init__(self, *args, **kwargs):
        self.calls = []; self.raw = message(); self.instances.append(self)
    def login(self, email, code):
        self.calls.append(('login', email))
        if email == 'fail@qq.com':
            raise imaplib.IMAP4.error('synthetic authentication failure')
        return 'OK', []
    def select(self, name, readonly=False):
        self.calls.append(('select', name, readonly)); return 'OK', [b'1']
    def search(self, *args):
        self.calls.append(('search', args)); return 'OK', [b'1']
    def fetch(self, mid, query):
        self.calls.append(('fetch', query))
        if query == '(RFC822.SIZE)': return 'OK', [('1 (RFC822.SIZE %d)' % len(self.raw)).encode()]
        return 'OK', [(b'1 (BODY[]', self.raw), b')']
    def logout(self): self.calls.append(('logout',)); return 'BYE', []
    def shutdown(self): self.calls.append(('shutdown',))


def run_fixture(*args):
    with patch.object(core.imaplib, 'IMAP4_SSL', FakeMailbox):
        run_job(*args)


def run_slow(*args):
    time.sleep(300)
