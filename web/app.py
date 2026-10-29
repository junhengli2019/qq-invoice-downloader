"""Private, ephemeral web service. Run one process with bounded threaded workers."""
import hmac
import io
import os
import secrets
import threading
import time
import zipfile
from urllib.parse import urlsplit
from flask import Flask, jsonify, make_response, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from .mailbox import MailboxError, PROVIDERS, MAX_MESSAGES, MAX_FILES, MAX_TOTAL_BYTES, validate_input, scan_mailbox

TTL = 900
MAX_SESSIONS = 200
ANONYMOUS_TTL = 300
MAX_MEMORY = 96 * 1024 * 1024


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=8192, APP_ACCESS_CODE=os.environ.get('APP_ACCESS_CODE', ''), TESTING=False)
    if config: app.config.update(config)
    if os.environ.get('TRUST_PROXY') == '1':
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_for=1)
    sessions = {}
    attempts = {}
    lock = threading.RLock()
    scan_gate = threading.BoundedSemaphore(2)
    app.extensions['invoice_sessions'] = sessions

    def cleanup():
        now = time.monotonic()
        with lock:
            for key, value in list(sessions.items()):
                if value['expires'] <= now:
                    sessions.pop(key, None)
                elif value.get('job') and value['job']['expires'] <= now:
                    value['job'] = None
            for key, value in list(attempts.items()):
                if value[1] <= now: attempts.pop(key, None)

    def janitor():
        while True:
            time.sleep(30)
            cleanup()
    if not app.config['TESTING']:
        threading.Thread(target=janitor, daemon=True, name='expiry-cleanup').start()

    def error(message, status=400):
        return jsonify(error=message), status

    def current():
        return sessions.get(request.cookies.get('invoice_sid', ''))

    @app.before_request
    def guard():
        cleanup()
        if request.path == '/healthz': return None
        # Do not accept mailbox credentials on a public plaintext connection.
        local = request.host.split(':', 1)[0] in ('127.0.0.1', 'localhost', '[::1]')
        if not request.is_secure and not local and not app.config['TESTING']:
            return error('请使用 HTTPS 安全地址访问此服务。', 403)
        if request.method in ('POST', 'DELETE', 'PUT', 'PATCH'):
            origin = request.headers.get('Origin')
            if origin and origin != request.host_url.rstrip('/'):
                return error('请求来源校验失败，请刷新页面。', 403)
            state = current()
            token = request.headers.get('X-CSRF-Token', '')
            if not state or not hmac.compare_digest(token.encode(), state['csrf'].encode()):
                return error('页面会话已过期，请刷新后重试。', 403)
        if request.path.startswith('/api/') and request.path not in ('/api/session', '/api/login'):
            state = current()
            if not state or not state['authenticated']:
                return error('请先输入网站访问口令。', 401)

    @app.after_request
    def secure_headers(response):
        response.headers.update({
            'Cache-Control': 'no-store, max-age=0', 'Pragma': 'no-cache',
            'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()'
        })
        if request.is_secure: response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.errorhandler(413)
    def too_large(e): return error('请求过大，请缩短输入。', 413)

    @app.errorhandler(400)
    def malformed(e): return error('请求格式不正确，请刷新重试。', 400)

    @app.errorhandler(500)
    def failure(e): return error('处理失败，请稍后再试。', 500)

    @app.get('/healthz')
    def health(): return jsonify(status='ok')

    @app.get('/')
    def index(): return render_template('index.html')

    @app.get('/api/session')
    def session_info():
        with lock:
            state = current()
            sid = request.cookies.get('invoice_sid', '')
            if state is None:
                if len(sessions) >= MAX_SESSIONS:
                    # Anonymous probes must not lock out legitimate users for an hour.
                    # Evict only anonymous sessions; never discard authenticated results.
                    anonymous = [(key, value) for key, value in sessions.items() if not value['authenticated']]
                    if not anonymous: return error('服务繁忙，请稍后再试。', 503)
                    oldest = min(anonymous, key=lambda pair: pair[1]['expires'])[0]
                    sessions.pop(oldest, None)
                sid = secrets.token_urlsafe(32)
                state = {'csrf': secrets.token_urlsafe(32), 'expires': time.monotonic() + ANONYMOUS_TTL, 'authenticated': False, 'job': None, 'busy': False}
                sessions[sid] = state
            response = jsonify(csrf=state['csrf'], authenticated=state['authenticated'], configured=len(app.config['APP_ACCESS_CODE']) >= 16,
                providers=[dict(id=k, name=v['name'], domains=v['domains']) for k, v in PROVIDERS.items()],
                limits={'messages': MAX_MESSAGES, 'files': MAX_FILES, 'bytes': MAX_TOTAL_BYTES, 'expires_in': TTL})
            response.set_cookie('invoice_sid', sid, secure=request.is_secure, httponly=True, samesite='Strict', max_age=3600)
            return response

    @app.post('/api/login')
    def login():
        expected = app.config['APP_ACCESS_CODE']
        if len(expected) < 16: return error('管理员尚未配置网站访问口令，邮箱连接功能未启用。', 503)
        data = request.get_json(silent=True)
        if not isinstance(data, dict): return error('请求格式不正确。')
        supplied = data.get('access_code', '')
        ip = request.remote_addr or 'unknown'
        with lock:
            count, expires = attempts.get(ip, (0, time.monotonic() + 600))
            if count >= 10: return error('尝试过于频繁，请 10 分钟后再试。', 429)
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), expected.encode()):
                if len(attempts) < 1000 or ip in attempts: attempts[ip] = (count + 1, expires)
                return error('网站访问口令不正确。', 401)
            attempts.pop(ip, None)
            # Rotate session ID on authentication to prevent fixation.
            state = current()
            if state is None: return error('页面会话已过期，请刷新。', 401)
            sessions.pop(request.cookies.get('invoice_sid', ''), None)
            sid = secrets.token_urlsafe(32)
            state['authenticated'] = True
            state['expires'] = time.monotonic() + 3600
            sessions[sid] = state
            response = jsonify(authenticated=True)
            response.set_cookie('invoice_sid', sid, secure=request.is_secure, httponly=True, samesite='Strict', max_age=3600)
            return response

    @app.post('/api/search')
    def search():
        data = request.get_json(silent=True)
        if not isinstance(data, dict): return error('请求格式不正确。')
        try: args = validate_input(data)
        except MailboxError as exc: return error(str(exc))
        with lock:
            state = current()
            if state is None or not state['authenticated']: return error('页面会话已过期，请刷新。', 401)
            if state['busy']: return error('已有扫描正在进行，请稍候。', 409)
            if not scan_gate.acquire(blocking=False): return error('服务正在处理其他扫描，请稍后重试。', 429)
            state['busy'] = True
            state['job'] = None
            sid = request.cookies.get('invoice_sid')
        try:
            result, blobs = scan_mailbox(*args)
            # Do not persist credential or mailbox address in application session.
            del args, data
            with lock:
                if sessions.get(sid) is not state:
                    return error('会话已经退出，本次结果已丢弃。', 401)
                used = sum(sum(map(len, s['job']['blobs'].values())) for s in sessions.values() if s.get('job'))
                if used + sum(map(len, blobs.values())) > MAX_MEMORY:
                    return error('临时存储已满，请稍后重试。', 503)
                job_id = secrets.token_urlsafe(24)
                state['job'] = {'id': job_id, 'blobs': blobs, 'items': result['items'], 'expires': time.monotonic() + TTL}
                return jsonify(**result, job_id=job_id, expires_in=TTL)
        except MailboxError as exc: return error(str(exc), 422)
        except Exception:
            # Do not log raw exception: IMAP/provider errors can contain secrets.
            return error('扫描未完成，请缩小范围或检查邮箱设置后重试。', 502)
        finally:
            with lock: state['busy'] = False
            scan_gate.release()

    @app.post('/api/download')
    def download():
        data = request.get_json(silent=True)
        if not isinstance(data, dict): return error('请求格式不正确。')
        with lock:
            state = current()
            if state is None: return error('页面会话已过期，请刷新。', 401)
            job = state.get('job')
            if not job or data.get('job_id') != job['id']: return error('文件已过期或已清除，请重新扫描。', 410)
            ids = data.get('item_ids', [])
            if not isinstance(ids, list) or not ids or len(ids) > MAX_FILES or any(not isinstance(i, str) or i not in job['blobs'] for i in ids):
                return error('请选择当前扫描结果中的文件。')
            selected = [(item, job['blobs'][item['id']]) for item in job['items'] if item['id'] in set(ids)]
        output = io.BytesIO()
        # STORE prevents a PDF compression CPU spike, and preserves original PDF bytes.
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
            for index, (item, blob) in enumerate(selected, 1): archive.writestr(f'{index:03d}_{item["name"]}', blob)
        output.seek(0)
        return send_file(output, mimetype='application/zip', as_attachment=True, download_name='invoices.zip', max_age=0)

    @app.delete('/api/results/<job_id>')
    def clear(job_id):
        with lock:
            state = current()
            if state and state.get('job') and state['job']['id'] == job_id: state['job'] = None
        return jsonify(cleared=True)

    @app.post('/api/logout')
    def logout():
        with lock: sessions.pop(request.cookies.get('invoice_sid', ''), None)
        response = jsonify(logged_out=True)
        response.delete_cookie('invoice_sid')
        return response

    return app

app = create_app()
