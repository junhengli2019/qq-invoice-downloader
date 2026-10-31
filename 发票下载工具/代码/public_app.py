"""The public website. The original invoice_web_app remains local-only."""
import hmac
import os
import re
import ssl
import imaplib
import threading
from datetime import date, timedelta
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from public_jobs import Busy, Jobs

COOKIE = 'invoice_visitor'


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('请重新填写邮箱和日期。')
    email = payload.get('email', '')
    code = payload.get('authorization_code', '')
    if not isinstance(email, str) or not re.fullmatch(r'[^@\s\x00-\x1f]{1,64}@(qq\.com|vip\.qq\.com|foxmail\.com)', email.strip(), re.I):
        raise ValueError('请填写完整的 QQ 邮箱，例如 123456@qq.com。')
    if not isinstance(code, str):
        raise ValueError('请填写 QQ 邮箱生成的 16 位授权码。')
    code = re.sub(r'\s', '', code)
    if not re.fullmatch(r'[A-Za-z]{16}', code):
        raise ValueError('授权码应是 16 位字母，请不要填写 QQ 密码。')
    if payload.get('consent') is not True:
        raise ValueError('请先勾选同意本次临时读取邮箱。')
    try:
        start = date.fromisoformat(payload['start_date'])
        end = date.fromisoformat(payload['end_date'])
        if start > end or (end - start).days > 30 or end > date.today() + timedelta(days=1):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('请选择最多 31 天的有效日期范围。') from None
    return dict(email_address=email.strip(), authorization_code=code, start=start.isoformat(), end=end.isoformat())


def create_app(manager=None, origin=None, probe=True):
    app = Flask(__name__, template_folder='web', static_folder='web/static')
    domain = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
    origin = origin or os.environ.get('PUBLIC_ORIGIN') or ('https://' + domain if domain else 'http://127.0.0.1:8766')
    secure = urlsplit(origin).scheme == 'https'
    app.config.update(MAX_CONTENT_LENGTH=8192, TRUSTED_HOSTS=[urlsplit(origin).hostname], JSON_SORT_KEYS=False)
    if domain:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    jobs = manager or Jobs()
    app.extensions['jobs'] = jobs
    connectivity = {'qq_connection': 'checking'}

    def probe_qq():
        mailbox = None
        try:
            mailbox = imaplib.IMAP4_SSL('imap.qq.com', 993, ssl_context=ssl.create_default_context(), timeout=12)
            connectivity['qq_connection'] = 'reachable'
        except Exception:
            connectivity['qq_connection'] = 'unavailable'
        finally:
            if mailbox:
                try: mailbox.shutdown()
                except Exception: pass
    if probe:
        threading.Thread(target=probe_qq, daemon=True).start()

    @app.before_request
    def protect():
        if request.path in {'/healthz', '/favicon.ico', '/robots.txt'} or request.path.startswith('/static/'):
            return
        create = request.method == 'GET' and request.path == '/'
        sid, session = jobs.session(request.cookies.get(COOKIE), create=create)
        if not session:
            return jsonify(error='页面已过期，请刷新后重新开始。'), 401
        g.sid = sid; g.visitor = session
        if request.method not in {'GET', 'HEAD'}:
            if request.headers.get('Origin') != origin or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return jsonify(error='请从票夹网页内操作。'), 403
            token = request.headers.get('X-CSRF-Token', '')
            if not hmac.compare_digest(token, session.csrf):
                return jsonify(error='页面已过期，请刷新后重新开始。'), 403

    @app.after_request
    def headers(response):
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'",
        })
        if secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.path == '/' and getattr(g, 'sid', None):
            response.set_cookie(COOKIE, g.sid, httponly=True, secure=secure, samesite='Lax', max_age=3600)
        return response

    @app.errorhandler(Busy)
    def busy(error):
        return jsonify(error=str(error)), 429, {'Retry-After': '60'}

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {400: '提交内容无法识别，请重新填写。', 413: '提交内容过大，请重新填写。',
                    404: '页面或文件不存在，可能已经清除。', 405: '请从网页内操作。'}
        return jsonify(error=messages.get(error.code, '这次操作没有完成，请刷新后重试。')), error.code

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/healthz')
    def health():
        return jsonify(status='ok', version='0.2.0-public', **connectivity)

    @app.get('/api/session')
    def session():
        return jsonify(csrf=g.visitor.csrf, job=jobs.snapshot(g.visitor))

    @app.get('/api/status')
    def status():
        return jsonify(jobs.snapshot(g.visitor))

    @app.post('/api/start')
    def start():
        if request.mimetype != 'application/json':
            return jsonify(error='请从网页内填写信息。'), 415
        try:
            values = validate(request.get_json())
        except ValueError as error:
            return jsonify(error=str(error)), 400
        jobs.start(g.visitor, request.remote_addr or 'unknown', **values)
        return jsonify(jobs.snapshot(g.visitor)), 202

    @app.post('/api/demo')
    def demo():
        jobs.start(g.visitor, request.remote_addr or 'unknown', demo=True)
        return jsonify(jobs.snapshot(g.visitor)), 202

    @app.post('/api/clear')
    def clear():
        jobs.clear(g.visitor)
        return jsonify(state='idle', message='本次任务和文件已清除。')

    @app.get('/api/download')
    def download():
        result = jobs.download(g.visitor)
        if not result:
            return jsonify(error='下载文件不存在或已到期，请重新整理。'), 404
        file, length = result
        response = send_file(file, mimetype='application/zip', as_attachment=True, download_name='发票整理.zip', conditional=False)
        response.content_length = length
        response.call_on_close(file.close)
        return response

    @app.get('/favicon.ico')
    def favicon():
        return '', 204

    @app.get('/robots.txt')
    def robots():
        return 'User-agent: *\nDisallow: /\n', 200, {'Content-Type': 'text/plain'}

    return app


# Gunicorn calls the factory; importing this module never opens a mailbox or spawns a janitor.
if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=8766, debug=False)
