"""Real loopback WSGI smoke test using only synthetic demo invoices."""
import http.cookiejar
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler, Request
import zipfile


class LocalServerSmokeTests(unittest.TestCase):
    def test_loopback_demo_download_stop_and_restart(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        root = Path(__file__).resolve().parents[1]
        url = f'http://127.0.0.1:{port}'
        command = [sys.executable, '-m', 'webapp.local_server', '--no-browser', '--port', str(port)]
        process = None
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def start():
            proc = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    self.fail('Local server exited before ready: ' + proc.communicate()[0])
                try:
                    with opener.open(url + '/healthz', timeout=.5) as response:
                        self.assertEqual(response.status, 200)
                    return proc
                except (URLError, TimeoutError):
                    time.sleep(.05)
            proc.terminate()
            proc.communicate(timeout=5)
            self.fail('Local server did not become ready')

        def stop(proc):
            if proc.poll() is None:
                if os.name == 'nt':
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=10)

        try:
            process = start()
            with opener.open(url + '/api/session') as response:
                session = json.load(response)
            self.assertEqual(session['runtime_mode'], 'local')
            headers = {'X-CSRF-Token':session['csrf_token'], 'Origin':url, 'Content-Type':'application/json'}
            with opener.open(Request(url + '/api/demo', data=b'{}', headers=headers, method='POST')) as response:
                ident = json.load(response)['id']
            with opener.open(url + '/api/jobs/' + ident) as response:
                job = json.load(response)
            self.assertEqual(len(job['files']), 3)
            data = json.dumps({'ids':[job['files'][0]['id']]}).encode()
            with opener.open(Request(url + '/api/jobs/' + ident + '/download', data=data, headers=headers, method='POST')) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
            self.assertEqual(len(archive.namelist()), 1)
            self.assertIn(b'NOT A VALID INVOICE', archive.read(archive.namelist()[0]))
            with self.assertRaises(HTTPError) as denied:
                opener.open(Request(url + '/api/session', headers={'Host':'evil.example'}))
            self.assertEqual(denied.exception.code, 403)
            # A second instance must fail rather than choose a public address/kill another process.
            duplicate = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(duplicate.returncode, 1)
            self.assertIn('不会关闭其他程序', duplicate.stdout)
            stop(process)
            process = None
            with socket.socket() as sock:
                self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)
            process = start()
            with opener.open(url + '/api/session') as response:
                restarted = json.load(response)
            self.assertIsNone(restarted['active_job_id'])
            self.assertNotEqual(restarted['csrf_token'], session['csrf_token'])
        finally:
            if process is not None:
                stop(process)


if __name__ == '__main__':
    unittest.main()
