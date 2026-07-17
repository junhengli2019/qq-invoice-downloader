"""QQ 邮箱发票下载工具的本地网页。仅使用 Python 标准库。"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import urlsplit

from download_invoices import DownloadError, download_invoices


HOST = "127.0.0.1"
PORT = 8765
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_REQUEST_BYTES = 16 * 1024
QQ_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@(qq\.com|vip\.qq\.com|foxmail\.com)$", re.IGNORECASE)


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QQ 邮箱发票下载工具</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #17213a;
      --muted: #65708a;
      --line: #dce3ef;
      --brand: #3157d5;
      --brand-dark: #2443aa;
      --panel: #fff;
      --stamp: #c9444d;
      --success: #16734a;
      --danger: #b23a48;
      --shadow: 0 22px 60px rgba(29,48,95,.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft YaHei UI", "Microsoft YaHei", system-ui, sans-serif;
      color: var(--ink);
      background:
        repeating-linear-gradient(0deg, rgba(73,99,145,.035) 0, rgba(73,99,145,.035) 1px, transparent 1px, transparent 32px),
        radial-gradient(circle at 15% 10%, rgba(110,150,255,.24), transparent 34rem),
        radial-gradient(circle at 90% 90%, rgba(91,214,190,.20), transparent 30rem),
        #f3f6fc;
    }
    main { width: min(980px, calc(100% - 32px)); margin: 0 auto; padding: 48px 0; }
    header { display: flex; align-items: center; justify-content: space-between; gap: 28px; margin-bottom: 24px; }
    .eyebrow { color: var(--brand); font-weight: 700; letter-spacing: .12em; font-size: 13px; }
    h1 { margin: 8px 0 10px; font-family: Bahnschrift, "Microsoft YaHei UI", sans-serif; font-size: clamp(30px, 5vw, 48px); letter-spacing: -.035em; }
    header p { margin: 0; color: var(--muted); font-size: 16px; }
    .local-seal {
      width: 78px;
      height: 78px;
      display: grid;
      place-items: center;
      flex: none;
      border: 3px double var(--stamp);
      border-radius: 50%;
      color: var(--stamp);
      font: 750 18px/1.15 "Microsoft YaHei UI", sans-serif;
      letter-spacing: .18em;
      text-align: center;
      transform: rotate(-8deg);
      box-shadow: inset 0 0 0 4px rgba(201,68,77,.06);
    }
    .mobile-break { display: none; }
    .layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(300px, .86fr); gap: 20px; }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
      padding: 26px;
    }
    h2 { margin: 0 0 20px; font-size: 20px; }
    label { display: block; margin: 0 0 7px; font-weight: 650; font-size: 14px; }
    .field { margin-bottom: 17px; }
    input {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 13px;
      font: inherit;
      color: var(--ink);
      background: #fbfcff;
      outline: none;
      transition: border-color .15s, box-shadow .15s;
    }
    input:focus { border-color: var(--brand); box-shadow: 0 0 0 4px rgba(49,87,213,.12); }
    .dates { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .hint { color: var(--muted); font-size: 12px; margin-top: 6px; line-height: 1.55; }
    button {
      width: 100%;
      margin-top: 4px;
      border: 0;
      border-radius: 12px;
      padding: 13px 18px;
      color: white;
      background: var(--brand);
      font-family: inherit;
      font-size: 16px;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 10px 24px rgba(49,87,213,.25);
    }
    button:hover { background: var(--brand-dark); }
    button:focus-visible { outline: 3px solid rgba(49,87,213,.28); outline-offset: 3px; }
    button:disabled { cursor: wait; opacity: .62; box-shadow: none; }
    .status-head { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #9ba6bb; flex: none; }
    .running .dot { background: #d78a18; box-shadow: 0 0 0 5px rgba(215,138,24,.13); }
    .success .dot { background: var(--success); box-shadow: 0 0 0 5px rgba(22,115,74,.12); }
    .error .dot { background: var(--danger); box-shadow: 0 0 0 5px rgba(178,58,72,.12); }
    .status-title { font-weight: 750; }
    .message { color: var(--muted); line-height: 1.65; min-height: 52px; margin-bottom: 16px; }
    .summary {
      display: none;
      margin-bottom: 16px;
      padding: 12px 14px;
      border-radius: 12px;
      background: #f1f5ff;
      color: #334365;
      line-height: 1.7;
      font-size: 13px;
    }
    .summary.visible { display: block; }
    .output-label { color: var(--muted); font-size: 12px; font-weight: 700; margin-bottom: 7px; }
    pre {
      margin: 0;
      min-height: 225px;
      max-height: 350px;
      overflow: auto;
      border-radius: 14px;
      padding: 14px;
      background: #11182a;
      color: #dce6ff;
      font: 12px/1.65 Consolas, "Microsoft YaHei UI", monospace;
      white-space: pre-wrap;
      word-break: break-word;
    }
    footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 22px; }
    @media (max-width: 760px) {
      main { padding: 28px 0; }
      header { position: relative; display: block; padding-right: 72px; }
      .local-seal { position: absolute; top: 0; right: 0; width: 64px; height: 64px; font-size: 15px; }
      .mobile-break { display: block; }
      .layout { grid-template-columns: 1fr; }
      .card { padding: 21px; border-radius: 18px; }
      .dates { grid-template-columns: 1fr; gap: 0; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <div class="eyebrow">QQ 邮箱 → 发票 PDF</div>
        <h1>QQ 邮箱发票<br class="mobile-break">下载工具</h1>
        <p>信息只在本机本次运行中使用，不会保存邮箱或授权码。</p>
      </div>
      <div class="local-seal" aria-label="本机处理">本机<br>处理</div>
    </header>
    <div class="layout">
      <section class="card" aria-labelledby="form-title">
        <h2 id="form-title">填写下载范围</h2>
        <form id="download-form">
          <div class="field">
            <label for="email">QQ 邮箱</label>
            <input id="email" name="email" type="email" placeholder="例如：123456789@qq.com" autocomplete="email" required>
          </div>
          <div class="field">
            <label for="authorization-code">IMAP 授权码</label>
            <input id="authorization-code" name="authorization_code" type="password" placeholder="不是 QQ 登录密码" autocomplete="new-password" required>
            <div class="hint">请在 QQ 邮箱设置中开启 IMAP 服务并生成授权码。</div>
          </div>
          <div class="dates">
            <div class="field">
              <label for="start-date">起始日期</label>
              <input id="start-date" name="start_date" type="date" required>
            </div>
            <div class="field">
              <label for="end-date">截止日期</label>
              <input id="end-date" name="end_date" type="date" required>
            </div>
          </div>
          <button id="start-button" type="submit">开始下载</button>
        </form>
      </section>
      <section id="status-card" class="card idle" aria-live="polite" aria-labelledby="status-title">
        <div class="status-head">
          <span class="dot" aria-hidden="true"></span>
          <div id="status-title" class="status-title">等待开始</div>
        </div>
        <div id="status-message" class="message">填写左侧信息后点击“开始下载”。下载完成后，请到项目根目录的 downloads 文件夹查看 PDF。</div>
        <div id="summary" class="summary"></div>
        <div class="output-label">运行输出</div>
        <pre id="output">尚未运行。</pre>
      </section>
    </div>
    <footer>本服务只监听 127.0.0.1。关闭黑色命令行窗口即可停止。</footer>
  </main>
  <script>
    const form = document.getElementById('download-form');
    const button = document.getElementById('start-button');
    const card = document.getElementById('status-card');
    const title = document.getElementById('status-title');
    const message = document.getElementById('status-message');
    const output = document.getElementById('output');
    const summary = document.getElementById('summary');
    const authInput = document.getElementById('authorization-code');
    let pollTimer = null;

    function localDateString(value) {
      const year = value.getFullYear();
      const month = String(value.getMonth() + 1).padStart(2, '0');
      const day = String(value.getDate()).padStart(2, '0');
      return `${year}-${month}-${day}`;
    }

    const now = new Date();
    document.getElementById('end-date').value = localDateString(now);
    document.getElementById('start-date').value = localDateString(new Date(now.getFullYear(), now.getMonth(), 1));

    function renderStatus(status) {
      card.className = `card ${status.state || 'idle'}`;
      const labels = {idle: '等待开始', running: '正在下载', success: '下载完成', error: '运行失败'};
      title.textContent = labels[status.state] || '运行状态';
      message.textContent = status.message || '';
      button.disabled = status.state === 'running';
      button.textContent = status.state === 'running' ? '正在下载…' : '开始下载';
      const lines = Array.isArray(status.logs) ? status.logs : [];
      output.textContent = lines.length ? lines.join('\n') : '尚未运行。';
      output.scrollTop = output.scrollHeight;
      if (status.summary) {
        const s = status.summary;
        summary.textContent = `扫描邮件 ${s.scanned_messages} 封 · 匹配发票 ${s.matched_messages} 封 · 新增 PDF ${s.saved_pdfs} 个 · 已存在 ${s.skipped_existing} 个 · 跳过非 PDF 附件 ${s.skipped_non_pdf_attachments} 个 · 手动链接 ${s.manual_links} 个 · 失败项目 ${s.failed_items} 个`;
        summary.classList.add('visible');
      } else {
        summary.textContent = '';
        summary.classList.remove('visible');
      }
      if (status.state === 'running') schedulePoll();
    }

    function schedulePoll() {
      clearTimeout(pollTimer);
      pollTimer = setTimeout(refreshStatus, 900);
    }

    async function refreshStatus() {
      try {
        const response = await fetch('/api/status', {cache: 'no-store'});
        if (!response.ok) throw new Error('状态接口不可用');
        renderStatus(await response.json());
      } catch (error) {
        card.className = 'card error';
        title.textContent = '无法读取状态';
        message.textContent = '本地服务可能已经停止。请确认黑色命令行窗口仍然打开。';
        button.disabled = false;
        button.textContent = '开始下载';
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const data = new FormData(form);
      const payload = Object.fromEntries(data.entries());
      if (payload.start_date > payload.end_date) {
        card.className = 'card error';
        title.textContent = '日期有误';
        message.textContent = '起始日期不能晚于截止日期。';
        return;
      }
      button.disabled = true;
      button.textContent = '正在启动…';
      try {
        const response = await fetch('/api/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
        authInput.value = '';
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || '无法启动下载');
        renderStatus(result);
      } catch (error) {
        authInput.value = '';
        card.className = 'card error';
        title.textContent = '无法开始';
        message.textContent = error.message || '请求失败';
        button.disabled = false;
        button.textContent = '开始下载';
      }
    });

    refreshStatus();
  </script>
</body>
</html>
"""


class JobManager:
    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self._lock = threading.Lock()
        self._state: Dict[str, object] = {
            "state": "idle",
            "message": "等待用户填写信息。",
            "logs": [],
            "summary": None,
        }

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "state": self._state["state"],
                "message": self._state["message"],
                "logs": list(self._state["logs"]),
                "summary": dict(self._state["summary"]) if self._state["summary"] else None,
            }

    def start(
        self,
        email_address: str,
        authorization_code: str,
        start_date: date,
        end_date: date,
    ) -> Tuple[bool, str]:
        with self._lock:
            if self._state["state"] == "running":
                return False, "已有下载任务正在运行，请等待完成。"
            self._state = {
                "state": "running",
                "message": "正在连接 QQ 邮箱，请稍候……",
                "logs": [],
                "summary": None,
            }
        worker = threading.Thread(
            target=self._run,
            args=(email_address, authorization_code, start_date, end_date),
            name="invoice-downloader",
            daemon=True,
        )
        worker.start()
        return True, ""

    def _progress(self, line: str) -> None:
        with self._lock:
            logs = self._state["logs"]
            if isinstance(logs, list):
                logs.append(line)
                if len(logs) > 400:
                    del logs[:-400]
            self._state["message"] = line

    def _run(self, email_address: str, authorization_code: str, start_date: date, end_date: date) -> None:
        try:
            result = download_invoices(
                email_address=email_address,
                authorization_code=authorization_code,
                start_date=start_date,
                end_date=end_date,
                project_root=self.project_root,
                progress_callback=self._progress,
            )
            summary = result.to_dict()
            summary.pop("logs", None)
            message = "下载完成。如有 PDF，请到项目根目录的 downloads 文件夹查看。"
            if result.manual_links:
                message += " 另有链接无法自动下载，请查看“需要手动下载的发票链接.txt”。"
            if result.failed_items:
                message += " 部分项目处理失败，详情见运行输出。"
            with self._lock:
                self._state.update(state="success", message=message, summary=summary)
        except DownloadError as exc:
            with self._lock:
                self._state.update(state="error", message=str(exc), summary=None)
        except Exception as exc:
            print("下载任务发生未预期错误：%s" % type(exc).__name__)
            with self._lock:
                self._state.update(
                    state="error",
                    message="发生未预期错误。请查看黑色命令行窗口中的错误类型后重试。",
                    summary=None,
                )
        finally:
            authorization_code = ""


class InvoiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, project_root: Path):  # type: ignore[no-untyped-def]
        super().__init__(server_address, handler_class)
        self.job_manager = JobManager(project_root)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "InvoiceTool/1.0"
    sys_version = ""

    def log_message(self, format_string: str, *args: object) -> None:
        return

    def _allowed_host(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {
            "127.0.0.1:%d" % port,
            "localhost:%d" % port,
        }

    def _allowed_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port}

    def _common_headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )

    def _send_json(self, status: int, payload: Dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._common_headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self._common_headers("text/plain; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _check_local_request(self) -> bool:
        if self._allowed_host():
            return True
        self._send_text(403, "拒绝访问：Host 不属于本地服务。")
        return False

    def do_GET(self) -> None:
        if not self._check_local_request():
            return
        path = urlsplit(self.path).path
        if path == "/":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self._common_headers("text/html; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self._send_json(200, self.server.job_manager.snapshot())
        elif path == "/favicon.ico":
            self.send_response(204)
            self._common_headers("image/x-icon", 0)
            self.end_headers()
        else:
            self._send_text(404, "页面不存在。")

    def do_POST(self) -> None:
        if not self._check_local_request() or not self._allowed_origin():
            if self._allowed_host():
                self._send_text(403, "拒绝访问：请求来源不是本地页面。")
            return
        if urlsplit(self.path).path != "/api/start":
            self._send_text(404, "接口不存在。")
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, {"error": "请求格式必须是 JSON。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(400, {"error": "提交内容为空或过大。"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "提交内容无法解析。"})
            return
        try:
            email_address, authorization_code, start_date, end_date = _validate_form(payload)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        started, error = self.server.job_manager.start(
            email_address, authorization_code, start_date, end_date
        )
        if not started:
            self._send_json(409, {"error": error})
            return
        self._send_json(202, self.server.job_manager.snapshot())


def _validate_form(payload: object) -> Tuple[str, str, date, date]:
    if not isinstance(payload, dict):
        raise ValueError("提交内容格式错误。")
    email_address = str(payload.get("email", "")).strip()
    authorization_code = str(payload.get("authorization_code", "")).strip()
    if not QQ_EMAIL_PATTERN.fullmatch(email_address):
        raise ValueError("请输入有效的 QQ 邮箱地址。")
    if not authorization_code or len(authorization_code) > 128:
        raise ValueError("请输入有效的 IMAP 授权码。")
    try:
        start_date = date.fromisoformat(str(payload.get("start_date", "")))
        end_date = date.fromisoformat(str(payload.get("end_date", "")))
    except ValueError:
        raise ValueError("请输入有效的起始日期和截止日期。")
    if start_date > end_date:
        raise ValueError("起始日期不能晚于截止日期。")
    return email_address, authorization_code, start_date, end_date


def create_server(project_root: Path = PROJECT_ROOT, host: str = HOST, port: int = PORT) -> InvoiceHTTPServer:
    return InvoiceHTTPServer((host, port), RequestHandler, project_root)


def main() -> None:
    try:
        server = create_server()
    except OSError as exc:
        print("无法启动本地网页服务：127.0.0.1:%d 可能已被占用。" % PORT)
        print("请关闭之前打开的工具窗口，然后重新双击启动文件。")
        print("错误类型：%s" % type(exc).__name__)
        raise SystemExit(1)
    url = "http://%s:%d" % (HOST, PORT)
    print("QQ 邮箱发票下载工具已启动。")
    print("浏览器地址：%s" % url)
    print("关闭此黑色命令行窗口即可停止服务。")
    browser_timer = threading.Timer(0.6, lambda: webbrowser.open(url))
    browser_timer.daemon = True
    browser_timer.start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        print("\n本地服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
