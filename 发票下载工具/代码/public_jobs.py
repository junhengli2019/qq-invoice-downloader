"""Bounded, ephemeral jobs. Credentials never enter the retained job records."""
import atexit
import hashlib
import hmac
import multiprocessing
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from download_invoices import DownloadError, DownloadLimits, download_invoices

RESULT_TTL = 15 * 60
JOB_TIMEOUT = 180
MAX_ACTIVE = 2
MAX_SESSIONS = 200
MAX_JOBS = 20
MAX_RETAINED_BYTES = 240 * 1024 * 1024


def demo_pdf(label):
    text = 'BT /F1 22 Tf 48 760 Td (INVOICE DEMO) Tj 0 -48 Td /F1 14 Tf (' + label + ') Tj 0 -36 Td (NOT A VALID TAX INVOICE - FOR PREVIEW ONLY) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               ('<< /Length %d >>\nstream\n%s\nendstream' % (len(text), text)).encode()]
    data = b'%PDF-1.4\n'; offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data)); data += ('%d 0 obj\n' % index).encode() + obj + b'\nendobj\n'
    xref = len(data)
    data += ('xref\n0 %d\n0000000000 65535 f \n' % len(offsets)).encode()
    data += b''.join(('%010d 00000 n \n' % offset).encode() for offset in offsets[1:])
    return data + ('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(offsets), xref)).encode()


def run_job(channel, directory, demo, email_address, authorization_code, start, end):
    """Spawned process: no access to the Web process's other visitors or cookies."""
    root = Path(directory)
    try:
        def progress(line):
            # Do not forward raw mailbox subjects, senders, URLs or diagnostics.
            match = re.search(r'正在检查第 (\d+)/(\d+) 封', line)
            if match:
                channel.send({'state': 'running', 'stage': 'scan', 'checked': int(match[1]), 'total': int(match[2])})
            elif '日期范围内找到' in line:
                channel.send({'state': 'running', 'stage': 'scan'})
        if demo:
            channel.send({'state': 'running', 'stage': 'scan', 'checked': 2, 'total': 2})
            folder = root / 'downloads'; folder.mkdir()
            (folder / '示例-交通发票-不可报销.pdf').write_bytes(demo_pdf('Transport sample / CNY 48.00'))
            (folder / '示例-办公用品-不可报销.pdf').write_bytes(demo_pdf('Office supplies sample / CNY 128.00'))
            summary = {'scanned_messages': 2, 'matched_messages': 2, 'saved_pdfs': 2, 'manual_links': 0, 'failed_items': 0}
        else:
            result = download_invoices(email_address, authorization_code, date.fromisoformat(start),
                                       date.fromisoformat(end), root, progress, limits=DownloadLimits())
            summary = result.to_dict(); summary.pop('logs', None)
        authorization_code = ''
        channel.send({'state': 'running', 'stage': 'pack'})
        files = sorted((root / 'downloads').glob('*.pdf'))
        manual = root / '需要手动下载的发票链接.txt'
        if manual.exists():
            files.append(manual)
        if files:
            with zipfile.ZipFile(root / 'invoices.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for file in files:
                    archive.write(file, file.name)
        channel.send({'state': 'success', 'summary': summary, 'files': [p.name for p in files], 'has_download': bool(files)})
    except DownloadError as exc:
        channel.send({'state': 'error', 'message': str(exc)})
    except Exception:
        # No exception traceback: provider responses can contain private data.
        channel.send({'state': 'error', 'message': '这次整理没有完成，请稍后重试或缩小日期范围。'})
    finally:
        authorization_code = ''
        channel.close()


@dataclass
class Session:
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    seen: float = field(default_factory=time.monotonic)
    job: object = None


@dataclass
class Job:
    root: Path
    process: object
    channel: object
    demo: bool
    started: float = field(default_factory=time.monotonic)
    finished: float = 0
    result: dict = field(default_factory=lambda: {'state': 'running', 'stage': 'connect'})


class Busy(Exception):
    pass


class Jobs:
    def __init__(self, root=None, target=run_job, start_cleanup=True):
        if root is None:
            # One Web worker by contract. Clear remnants after a worker/container restart.
            self.root = Path(tempfile.gettempdir()) / 'invoice-public-trial'
            shutil.rmtree(self.root, ignore_errors=True)
        else:
            self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True); self.root.chmod(0o700)
        self.target = target
        self.sessions = {}; self.rates = {}; self.lock = threading.RLock()
        self.salt = secrets.token_bytes(32)
        self.context = multiprocessing.get_context('spawn')
        self.stop = threading.Event()
        if start_cleanup:
            threading.Thread(target=self._clean_loop, daemon=True, name='invoice-cleanup').start()
            atexit.register(self.close)

    def _clean_loop(self):
        while not self.stop.wait(2):
            with self.lock:
                self._sweep()

    def _remove_job(self, session):
        job = session.job
        if job:
            if job.process.is_alive():
                job.process.terminate(); job.process.join(1)
                if job.process.is_alive():
                    job.process.kill(); job.process.join(1)
            job.channel.close()
            shutil.rmtree(job.root, ignore_errors=True)
            session.job = None

    def _refresh(self, job):
        if job.finished:
            return
        try:
            while job.channel.poll():
                event = job.channel.recv()
                job.result.update(event)
        except (EOFError, OSError):
            pass
        if job.result['state'] in {'success', 'error'}:
            # Wait for normal worker exit before declaring its secret-bearing process gone.
            job.process.join(0.2)
            if job.process.is_alive():
                job.process.terminate(); job.process.join(1)
            job.finished = time.monotonic()
        elif time.monotonic() - job.started > JOB_TIMEOUT:
            job.process.terminate(); job.process.join(1)
            if job.process.is_alive():
                job.process.kill(); job.process.join(1)
            job.result = {'state': 'error', 'message': '整理超过 3 分钟，已停止。请选更短的日期范围后重试。'}
            job.finished = time.monotonic()
        elif not job.process.is_alive():
            job.result = {'state': 'error', 'message': '整理中断了，请重新开始。'}
            job.finished = time.monotonic()
        if job.finished and job.result['state'] == 'error':
            shutil.rmtree(job.root, ignore_errors=True)
        if job.finished:
            job.channel.close()

    def _sweep(self):
        now = time.monotonic()
        for sid, session in list(self.sessions.items()):
            if session.job:
                self._refresh(session.job)
                if session.job.finished and now - session.job.finished >= RESULT_TTL:
                    self._remove_job(session)
            if not session.job and now - session.seen > 3600:
                del self.sessions[sid]
        self.rates = {key: value for key, value in self.rates.items() if value[0] > now}

    def session(self, sid, create=False):
        with self.lock:
            self._sweep()
            session = self.sessions.get(sid)
            if not session and create:
                if len(self.sessions) >= MAX_SESSIONS:
                    raise Busy('当前试用人数较多，请稍后再来。')
                sid = secrets.token_urlsafe(32); session = Session(); self.sessions[sid] = session
            if session:
                session.seen = time.monotonic()
            return sid, session

    def start(self, session, ip, demo=False, **credentials):
        with self.lock:
            self._sweep()
            if session.job and not session.job.finished:
                raise Busy('已经在整理，请等待完成，或先停止当前任务。')
            jobs = [s.job for s in self.sessions.values() if s.job]
            active = sum(not job.finished for job in jobs)
            used = sum(path.stat().st_size for path in self.root.rglob('*') if path.is_file())
            if active >= MAX_ACTIVE or len(jobs) >= MAX_JOBS or used + (active + 1) * 80 * 1024 * 1024 > MAX_RETAINED_BYTES:
                raise Busy('大家都在整理发票，请稍等一会儿再试。')
            hashed = hmac.new(self.salt, ip.encode(), hashlib.sha256).hexdigest()
            now = time.monotonic()
            keys = [('ip:' + hashed, 8), ('all', 60)]
            for key, limit in keys:
                until, count = self.rates.get(key, (now + 3600, 0))
                if count >= limit:
                    raise Busy('本小时试用次数已用完，请稍后再试。')
            for key, _ in keys:
                until, count = self.rates.get(key, (now + 3600, 0))
                self.rates[key] = (until, count + 1)
            self._remove_job(session)
            directory = Path(tempfile.mkdtemp(dir=self.root))
            parent, child = self.context.Pipe(duplex=False)
            process = self.context.Process(target=self.target, args=(child, str(directory), demo,
                credentials.get('email_address', ''), credentials.get('authorization_code', ''),
                credentials.get('start', ''), credentials.get('end', '')), daemon=True)
            try:
                process.start()
            except Exception:
                parent.close(); child.close(); shutil.rmtree(directory, ignore_errors=True)
                raise Busy('暂时无法开始整理，请稍后重试。') from None
            child.close()
            session.job = Job(directory, process, parent, demo)

    def snapshot(self, session):
        with self.lock:
            self._sweep()
            if not session.job:
                return {'state': 'idle'}
            job = session.job
            result = dict(job.result)
            result['demo'] = job.demo
            if job.finished:
                result['expires_in'] = max(0, int(RESULT_TTL - (time.monotonic() - job.finished)))
            return result

    def clear(self, session):
        with self.lock:
            self._remove_job(session)

    def download(self, session):
        # Open under the same lock as deletion; an ongoing download keeps its FD.
        with self.lock:
            self._sweep()
            job = session.job
            if not job or job.result.get('state') != 'success' or not job.result.get('has_download'):
                return None
            path = job.root / 'invoices.zip'
            return path.open('rb'), path.stat().st_size

    def close(self):
        self.stop.set()
        with self.lock:
            for session in self.sessions.values():
                self._remove_job(session)
            self.sessions.clear()
            shutil.rmtree(self.root, ignore_errors=True)
