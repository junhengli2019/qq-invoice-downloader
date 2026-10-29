"""Bounded, ephemeral, per-browser invoice jobs. Run one Gunicorn worker."""
from __future__ import annotations

import hashlib
import io
import os
import secrets
import threading
import time
import zipfile
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file, send_from_directory, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from webapp.mailbox import MailboxError, scan_mailbox

app = Flask(__name__, static_folder=None)
app.config.update(SECRET_KEY=secrets.token_hex(32), MAX_CONTENT_LENGTH=8192,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                  SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=2))
if os.environ.get('RENDER'):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
STATIC = Path(__file__).parent / 'static'
TTL = 1800
MAX_JOBS = 3  # <=150 MiB retained PDFs, bounded scanning overhead on 512 MiB instance
LOCK = threading.RLock()
JOBS = {}
ATTEMPTS = defaultdict(deque)
ACTIVE_SCANS = 0
DOWNLOAD_SLOTS = threading.BoundedSemaphore(1)
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mailbox')


def fail(message, status=400):
    return jsonify(error=message), status


def cleanup():
    now = time.time()
    with LOCK:
        for key, job in list(JOBS.items()):
            if job['expires_at'] <= now:
                job['cancel'].set()
                job['files'].clear()
                del JOBS[key]
        for key, entries in list(ATTEMPTS.items()):
            while entries and entries[0] < now - 600:
                entries.popleft()
            if not entries:
                del ATTEMPTS[key]


def reaper():
    while True:
        time.sleep(30)
        cleanup()


threading.Thread(target=reaper, daemon=True, name='expiry').start()


@app.before_request
def protect():
    if request.path.startswith('/api/'):
        cleanup()
        if request.method in {'POST', 'DELETE', 'PUT', 'PATCH'}:
            expected = session.get('csrf')
            supplied = request.headers.get('X-CSRF-Token', '')
            if not expected or not secrets.compare_digest(expected, supplied):
                return fail('页面已过期，请刷新后重新操作。', 403)
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return fail('请求来源不正确，请在网站页面内操作。', 403)
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return fail('不允许跨站请求。', 403)


@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    if os.environ.get('RENDER'):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.errorhandler(Exception)
def error(exc):
    if isinstance(exc, HTTPException):
        messages = {413: '提交内容过大，请减少选择的文件后重试。', 404: '页面或任务不存在。', 405: '不支持此操作。', 400: '提交内容格式不正确，请刷新重试。'}
        return fail(messages.get(exc.code, '请求未能完成，请重试。'), exc.code)
    # Never log request data, exception repr, credentials, or message content.
    app.logger.error('Request failed: %s', type(exc).__name__)
    return fail('服务暂时无法完成操作，请稍后重试。', 500)


@app.get('/')
def index():
    return send_from_directory(STATIC, 'index.html')


@app.get('/<path:name>')
def assets(name):
    if name not in {'app.js', 'styles.css', 'favicon.svg'}:
        return fail('页面不存在。', 404)
    return send_from_directory(STATIC, name)


@app.get('/healthz')
def health():
    return jsonify(status='ok', service='invoice-pocket')


@app.get('/api/session')
def browser_session():
    if 'sid' not in session:
        session['sid'] = secrets.token_urlsafe(32)
        session['csrf'] = secrets.token_urlsafe(32)
        session.permanent = True
    with LOCK:
        active = next((j['id'] for j in JOBS.values() if j['owner'] == session['sid']), None)
    return jsonify(csrf_token=session['csrf'], active_job_id=active)


def json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('提交内容格式不正确。')
    return data


def new_job(demo=False):
    global ACTIVE_SCANS
    with LOCK:
        owner = session['sid']
        if any(j['owner'] == owner for j in JOBS.values()):
            return None, fail('请先清除上次结果，再开始新的查找。', 409)
        key = hashlib.sha256((request.remote_addr or 'unknown').encode()).hexdigest()
        if len(ATTEMPTS.get(key, ())) >= 8:
            return None, fail('操作较频繁，请 10 分钟后再试。', 429)
        if not demo and ACTIVE_SCANS >= 2:
            return None, fail('当前已有查找任务进行中，请稍后再试。', 503)
        if len(JOBS) >= MAX_JOBS:
            return None, fail('当前使用人数较多，请稍后再试。已有结果会在 30 分钟后清除。', 503)
        # Bound unauthenticated rate-limit bookkeeping as well.
        if key not in ATTEMPTS and len(ATTEMPTS) >= 1000:
            return None, fail('当前使用人数较多，请稍后重试。', 503)
        ATTEMPTS[key].append(time.time())
        ident = secrets.token_urlsafe(24)
        job = dict(id=ident, owner=owner, status='queued', progress={'message': '正在准备连接…', 'scanned': 0, 'matched': 0},
                   files=[], manual_links=[], warnings=[], error=None, demo=demo,
                   expires_at=time.time()+TTL, cancel=threading.Event())
        JOBS[ident] = job
        if not demo:
            ACTIVE_SCANS += 1
        return job, None


def public(job):
    return {key: ([{k: v for k, v in f.items() if k != 'data'} for f in job[key]] if key == 'files' else job[key])
            for key in ('id','status','progress','files','manual_links','warnings','error','demo','expires_at')}


def execute(job, provider, email, auth_code, start, end):
    global ACTIVE_SCANS
    def progress(value):
        with LOCK:
            if not job['cancel'].is_set():
                job['progress'] = value
    try:
        with LOCK:
            job['status'] = 'running'
        result = scan_mailbox(provider, email, auth_code, start, end, progress, job['cancel'].is_set)
        with LOCK:
            if job['cancel'].is_set():
                return
            files = result.get('files', [])
            if len(files) > 100 or sum(len(f['data']) for f in files) > 50 * 1024 * 1024:
                raise MailboxError('本次结果过大，请缩短日期范围后重试。')
            job['files'] = [dict(f, id=secrets.token_urlsafe(12), size=len(f['data'])) for f in files]
            job['manual_links'] = result.get('manual_links', [])
            job['warnings'] = result.get('warnings', [])
            job['progress'] = {'message': '查找完成', 'scanned': result.get('scanned', 0), 'matched': result.get('matched', 0)}
            job['status'] = 'done'
            job['expires_at'] = time.time()+TTL
    except MailboxError as exc:
        with LOCK:
            job['error'] = str(exc).replace(auth_code, '******') if auth_code else str(exc)
            job['status'] = 'error'
    except Exception as exc:
        with LOCK:
            job['error'] = '连接中断或处理失败。请稍后重试，或缩短日期范围。'
            job['status'] = 'error'
        app.logger.warning('Scan failed: %s', type(exc).__name__)
    finally:
        auth_code = None
        email = None
        with LOCK:
            ACTIVE_SCANS -= 1


@app.post('/api/jobs')
def create_job():
    try:
        data = json_object()
        provider = data.get('provider')
        email = data.get('email', '')
        code = data.get('auth_code', '')
        if not isinstance(email, str) or not isinstance(code, str):
            raise ValueError('请填写邮箱地址和授权码。')
        email = email.strip().lower()
        code = code.strip()
        allowed = {'qq': {'qq.com','foxmail.com'}, '163': {'163.com'}}
        if not isinstance(provider, str) or provider not in allowed:
            raise ValueError('目前支持 QQ 和 163 邮箱，请重新选择。')
        if len(email)>254 or email.count('@') != 1 or email.rsplit('@',1)[-1] not in allowed[provider] or not email.split('@')[0] or any(c.isspace() or ord(c)<32 for c in email):
            raise ValueError('邮箱地址与所选类型不一致，请填写完整的 QQ 或 163 邮箱地址。')
        if not 6 <= len(code) <= 128 or any(ord(c)<33 or ord(c)>126 for c in code):
            raise ValueError('请填写邮箱设置中生成的客户端授权码，不要填写登录密码。')
        if data.get('consent') is not True:
            raise ValueError('请先阅读并勾选本次临时处理说明。')
        try:
            start = date.fromisoformat(data.get('start_date', ''))
            end = date.fromisoformat(data.get('end_date', ''))
        except (ValueError, TypeError):
            raise ValueError('请选择有效的开始和结束日期。')
        if end == date.max or start > end or (end-start).days > 366:
            raise ValueError('结束日期应不早于开始日期，每次最多查找一年。')
    except (ValueError, TypeError) as exc:
        return fail(str(exc) if isinstance(exc, ValueError) else '提交内容格式不正确。')
    job, error_response = new_job()
    if error_response:
        return error_response
    POOL.submit(execute, job, provider, email, code, start, end)
    return jsonify(id=job['id']), 202


def demo_pdf(label):
    text = f'BT /F1 20 Tf 50 760 Td (DEMO - NOT A VALID INVOICE) Tj 0 -40 Td ({label}) Tj ET'.encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length '+str(len(text)).encode()+b' >>\nstream\n'+text+b'\nendstream']
    out=bytearray(b'%PDF-1.4\n'); offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(out)); out.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    xref=len(out); out.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]: out.extend(f'{offset:010d} 00000 n \n'.encode())
    out.extend(f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(out)


@app.post('/api/demo')
def demo():
    job, error_response = new_job(True)
    if error_response:
        return error_response
    today=date.today()
    with LOCK:
        for i, (sender, label) in enumerate([('出行服务（示例）','Travel sample'), ('酒店住宿（示例）','Hotel sample'), ('办公用品（示例）','Office sample')]):
            content=demo_pdf(label)
            job['files'].append(dict(id=secrets.token_urlsafe(12),name=f'示例_{i+1}_非真实发票.pdf',data=content,size=len(content),subject=sender+'电子发票',sender=sender,date=str(today-timedelta(days=i)),source='附件'))
        job.update(status='done', progress={'message':'示例已准备好','scanned':12,'matched':3}, warnings=['这是演示数据，下载的 PDF 标有 DEMO，不可用于报销。'])
    return jsonify(id=job['id']), 202


def owned_job(ident):
    job=JOBS.get(ident)
    return job if job and job['owner']==session.get('sid') else None


@app.get('/api/jobs/<ident>')
def status(ident):
    with LOCK:
        job=owned_job(ident)
        if not job: return fail('任务已过期或已清除，请重新查找。',404)
        return jsonify(public(job))


@app.delete('/api/jobs/<ident>')
def delete(ident):
    with LOCK:
        job=owned_job(ident)
        if not job: return fail('任务已过期或已清除。',404)
        job['cancel'].set()
        job['files'].clear()
        del JOBS[ident]
    return jsonify(ok=True)


@app.post('/api/jobs/<ident>/download')
def download(ident):
    try:
        data=json_object()
    except ValueError:
        return fail('请选择需要下载的文件。')
    ids=data.get('ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(x,str) for x in ids):
        return fail('请至少选择一份 PDF，最多 100 份。')
    with LOCK:
        job=owned_job(ident)
        if not job: return fail('任务已过期或已清除，请重新查找。',404)
        if job['status']!='done': return fail('请等待查找完成。',409)
        files={f['id']:f for f in job['files']}
        if any(x not in files for x in ids): return fail('选择的文件已失效，请重新查找。',400)
        selected=[files[x] for x in dict.fromkeys(ids)]
        demo_mode=job['demo']
    if not DOWNLOAD_SLOTS.acquire(blocking=False):
        return fail('其他下载正在打包，请稍后重试。', 503)
    try:
        archive=io.BytesIO()
        used=set()
        with zipfile.ZipFile(archive,'w',zipfile.ZIP_STORED) as z:
            for i,f in enumerate(selected,1):
                # Never trust incoming MIME attachment paths, even if scanner sanitizes them.
                name=str(f['name']).replace('\\','/').rsplit('/',1)[-1]
                name=''.join(c for c in name if ord(c)>=32).strip(' .')[:180] or 'invoice.pdf'
                if not name.lower().endswith('.pdf'): name+='.pdf'
                if name.casefold() in used: name=f'{i}_{name}'
                used.add(name.casefold()); z.writestr(name,f['data'])
        archive.seek(0)
        response = send_file(archive,mimetype='application/zip',as_attachment=True,download_name=('DEMO_' if demo_mode else '')+'invoices.zip')
        response.call_on_close(DOWNLOAD_SLOTS.release)
        response.direct_passthrough = False
        return response
    except BaseException:
        DOWNLOAD_SLOTS.release()
        raise

