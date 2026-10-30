"""Protocol and malicious-input regression coverage; no real account required."""
import io
import socket
import unittest
import zipfile
from datetime import date
from email import policy
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from webapp import mailbox as m


def invoice(pdf=b"%PDF-1.4\nexample", name="电子发票.pdf", subject="您的电子发票", body="谢谢"):
    message = EmailMessage(policy=policy.SMTP)
    message["Subject"] = subject
    message["From"] = "Example <billing@example.com>"
    message["Date"] = "Fri, 02 Oct 2026 10:00:00 +0800"
    message.set_content(body)
    if pdf is not None:
        message.add_attachment(pdf, maintype="application", subtype="pdf", filename=name)
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, messages, sizes=None, ids=None):
        self.messages = messages
        self.sizes = sizes or {}
        self.ids = ids or list(messages)
        self.calls = []

    def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", []

    def _simple_command(self, *args):
        self.calls.append(args)
        return "OK", []

    def select(self, *args, **kwargs):
        self.calls.append(("select", args, kwargs))
        return "OK", []

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b" ".join(self.ids)]
        uid, query = args
        if "RFC822.SIZE" in query:
            size = self.sizes.get(uid, len(self.messages.get(uid, b"")))
            return "OK", [b'1 (UID ' + uid + f' RFC822.SIZE {size} INTERNALDATE "02-Oct-2026 10:00:00 +0800")'.encode()]
        return "OK", [(b"1 (BODY[] {100}", self.messages[uid]), b")"]

    def logout(self):
        self.calls.append(("logout",))

    def shutdown(self):
        self.calls.append(("shutdown",))


def run_scan(fake, provider="qq", progress=None, cancelled=lambda: False):
    with patch.object(m.imaplib, "IMAP4_SSL", return_value=fake) as connect:
        result = m.scan_mailbox(provider, f"test@{'qq.com' if provider == 'qq' else '163.com'}", "secret-code", date(2026, 10, 1), date(2026, 10, 31), progress or (lambda x: None), cancelled)
    return result, connect


class MailboxTests(unittest.TestCase):
    def test_qq_tls_inclusive_dates_readonly_peek_and_metadata(self):
        fake = FakeIMAP({b"1": invoice()})
        result, connect = run_scan(fake)
        self.assertEqual(connect.call_args.args, ("imap.qq.com", 993))
        self.assertEqual(connect.call_args.kwargs["timeout"], 15)
        self.assertEqual(fake.calls[1], ("select", ("INBOX",), {"readonly": True}))
        self.assertIn(("search", None, "SINCE", "01-Oct-2026", "BEFORE", "01-Nov-2026"), fake.calls)
        self.assertTrue(any("BODY.PEEK[]<0." in str(call) for call in fake.calls))
        self.assertFalse(any("STORE" in str(call) for call in fake.calls))
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["date"], "2026-10-02")
        self.assertEqual(result["files"][0]["source"], "附件")
        self.assertEqual(fake.calls[-1], ("logout",))
        self.assertNotIn("secret-code", repr(result))

    def test_netease_id_between_auth_and_select(self):
        fake = FakeIMAP({b"1": invoice()})
        result, connect = run_scan(fake, provider="163")
        self.assertEqual(connect.call_args.args, ("imap.163.com", 993))
        self.assertEqual([call[0] for call in fake.calls[:3]], ["login", "ID", "select"])
        self.assertEqual(len(result["files"]), 1)

    def test_large_mail_never_fetches_body(self):
        fake = FakeIMAP({b"1": invoice()}, sizes={b"1": m.MAX_MESSAGE_BYTES + 1})
        result, _ = run_scan(fake)
        self.assertEqual(result["files"], [])
        self.assertFalse(any("BODY.PEEK" in str(call) for call in fake.calls))
        self.assertTrue(result["warnings"])

    def test_wrong_size_body_is_rejected(self):
        fake = FakeIMAP({b"1": invoice()}, sizes={b"1": 5})
        result, _ = run_scan(fake)
        self.assertEqual(result["files"], [])
        self.assertTrue(result["warnings"])

    def test_duplicate_bytes_removed_but_different_contents_same_name_preserved(self):
        fake = FakeIMAP({b"1": invoice(), b"2": invoice(), b"3": invoice(b"%PDF-1.4\ndifferent")})
        result, _ = run_scan(fake)
        self.assertEqual(len(result["files"]), 2)
        names = [f["name"] for f in result["files"]]
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(any("去重" in w for w in result["warnings"]))

    def test_pdf_magic_and_invoice_filter(self):
        fake = FakeIMAP({b"1": invoice(b"<html>not pdf"), b"2": invoice(subject="普通通知", name="普通文件.pdf")})
        result, _ = run_scan(fake)
        self.assertEqual(result["files"], [])
        self.assertEqual(result["matched"], 1)

    def test_latest_500_and_warning(self):
        fake = FakeIMAP({str(i).encode(): invoice(pdf=None, subject="通知") for i in range(1, 503)})
        result, _ = run_scan(fake)
        self.assertEqual(result["scanned"], 500)
        fetches = [call for call in fake.calls if call[0] == "fetch"]
        self.assertEqual(fetches[0][1], b"502")
        self.assertEqual(fetches[-1][1], b"3")
        self.assertTrue(any("500" in w for w in result["warnings"]))

    def test_cancellation_prevents_connect(self):
        fake = FakeIMAP({b"1": invoice()})
        result, connect = run_scan(fake, cancelled=lambda: True)
        connect.assert_not_called()
        self.assertEqual(result["scanned"], 0)

    def test_provider_and_domain_rejected_before_connect(self):
        for provider, email in (("bad", "a@qq.com"), ("163", "a@qq.com"), ("qq", "x@127.0.0.1")):
            with self.subTest(provider=provider, email=email), self.assertRaises(m.MailboxError):
                m.scan_mailbox(provider, email, "abc", date(2026, 1, 1), date(2026, 1, 1), lambda p: None, lambda: False)

    def test_login_failure_does_not_disclose_provider_message(self):
        fake = FakeIMAP({})
        fake.login = MagicMock(side_effect=m.imaplib.IMAP4.error("AUTH secret-code"))
        with self.assertRaises(m.MailboxError) as caught:
            run_scan(fake)
        self.assertNotIn("secret-code", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(fake.calls[-1], ("shutdown",))

    def test_plain_text_pdf_link(self):
        fake = FakeIMAP({b"1": invoice(pdf=None, body="发票 https://billing.example.com/invoice.pdf")})
        headers = EmailMessage()
        with patch.object(m, "_fetch_link", return_value=(b"%PDF-1.4\nlink", headers, "https://billing.example.com/invoice.pdf")):
            result, _ = run_scan(fake)
        self.assertEqual(result["files"][0]["source"], "链接")

    def test_zip_pdf_only_no_path_traversal(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../../evil.pdf", b"%PDF-1.4\ninvoice")
            archive.writestr("other.xml", b"<invoice/>")
        fake = FakeIMAP({b"1": invoice(pdf=None, body="发票 https://billing.example.com/invoice.zip")})
        with patch.object(m, "_fetch_link", return_value=(data.getvalue(), EmailMessage(), "https://billing.example.com/invoice.zip")):
            result, _ = run_scan(fake)
        self.assertEqual(result["files"][0]["name"], "evil.pdf")
        self.assertEqual(len(result["files"]), 1)

    def test_html_response_returns_manual_link(self):
        fake = FakeIMAP({b"1": invoice(pdf=None, body="发票 https://billing.example.com/invoice.pdf")})
        with patch.object(m, "_fetch_link", return_value=(b"<html>login", EmailMessage(), "https://billing.example.com/invoice.pdf")):
            result, _ = run_scan(fake)
        self.assertEqual(result["files"], [])
        self.assertEqual(len(result["manual_links"]), 1)

    def test_subject_invoice_context_with_generic_download_link(self):
        message = EmailMessage(policy=policy.SMTP)
        message["Subject"] = "您的电子发票"
        message.set_content('<a href="https://billing.example.com/download?id=123">下载</a>', subtype="html")
        body, urls = m._body(message)
        self.assertEqual(urls, ["https://billing.example.com/download?id=123"])

    def test_network_interruption_retains_partial_results(self):
        fake = FakeIMAP({b"1": invoice(), b"2": invoice(b"%PDF-1.4\nsecond")})
        original = fake.uid
        def interrupt(command, *args):
            if command == "fetch" and args[0] == b"1":
                raise TimeoutError("test timeout")
            return original(command, *args)
        fake.uid = interrupt
        result, _ = run_scan(fake)
        self.assertEqual(len(result["files"]), 1)
        self.assertTrue(any("不完整" in w for w in result["warnings"]))

    def test_large_body_and_link_candidate_work_are_bounded(self):
        message = EmailMessage()
        message["Subject"] = "电子发票"
        message.set_content(" ".join(f"https://example.com/{n}.pdf" for n in range(1500)))
        warnings = []
        body, urls = m._body(message, warnings.append)
        self.assertEqual(len(urls), 1000)
        self.assertTrue(warnings)
        message.set_content("x" * (m.MAX_BODY_BYTES + 100))
        body, urls = m._body(message, warnings.append)
        self.assertLessEqual(len(body), m.MAX_BODY_BYTES)

    def test_file_count_limit(self):
        fake = FakeIMAP({str(i).encode(): invoice(f"%PDF-1.4\n{i}".encode()) for i in range(1, 103)})
        result, _ = run_scan(fake)
        self.assertEqual(len(result["files"]), 100)
        self.assertTrue(result["warnings"])


class NetworkTests(unittest.TestCase):
    def test_private_loopback_mapped_and_mixed_dns_blocked(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with self.subTest(address=address), patch.object(m.socket, "getaddrinfo", return_value=records), self.assertRaises(m._LinkError):
                m._public_target("https://example.com/invoice.pdf")
        mixed = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in ("1.1.1.1", "10.0.0.1")]
        with patch.object(m.socket, "getaddrinfo", return_value=mixed), self.assertRaises(m._LinkError):
            m._public_target("https://example.com/invoice.pdf")

    def test_schemes_credentials_and_special_ports_blocked(self):
        for url in ("file:///etc/passwd", "https://a:b@example.com/x", "https://example.com:8443/x", "https://example.com/\r\nHost:evil"):
            with self.subTest(url=url), self.assertRaises(m._LinkError):
                m._public_target(url)

    def test_connection_pins_resolved_ip_and_tls_verifies_hostname(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        with patch.object(m.socket, "getaddrinfo", return_value=records):
            _, hostname, ip, port = m._public_target("https://invoice.example.com/x.pdf")
        sock, wrapped = MagicMock(), MagicMock()
        context = MagicMock()
        context.wrap_socket.return_value = wrapped
        with patch.object(m.socket, "create_connection", return_value=sock) as connect, patch.object(m.ssl, "create_default_context", return_value=context):
            connection = m._PinnedHTTP(hostname, ip, port, 10, True)
            connection.connect()
        connect.assert_called_once_with(("1.1.1.1", 443), 10)
        context.wrap_socket.assert_called_once_with(sock, server_hostname="invoice.example.com")
        self.assertIs(connection.sock, wrapped)

    def test_redirect_to_internal_host_is_revalidated(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        response = MagicMock(status=302)
        response.getheader.return_value = "https://127.0.0.1/invoice.pdf"
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(m.socket, "getaddrinfo", side_effect=[public, private]), patch.object(m, "_PinnedHTTP", return_value=connection) as cls, self.assertRaises(m._LinkError):
            m._fetch_link("https://example.com/x", m.time.monotonic() + 60, lambda: False)
        cls.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
