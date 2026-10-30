"""Read-only QQ/163 invoice scanning. Credentials and results stay in memory."""
from __future__ import annotations

import hashlib
import http.client
import imaplib
import io
import ipaddress
import re
import socket
import ssl
import time
import unicodedata
import urllib.parse
import zipfile
from datetime import date, timedelta
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Callable

PROVIDERS = {
    "qq": ("QQ", "imap.qq.com", {"qq.com", "vip.qq.com", "foxmail.com"}),
    "163": ("163", "imap.163.com", {"163.com"}),
}
MAX_MESSAGES = 500
MAX_MESSAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILES = 100
MAX_LINK_BYTES = 10 * 1024 * 1024
MAX_LINKS = 30
MAX_SECONDS = 180
MAX_BODY_BYTES = 1024 * 1024
MAX_LINK_CANDIDATES = 1000
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# IMAP ID (RFC 2971) is required by NetEase for third-party clients.
imaplib.Commands.setdefault("ID", ("AUTH",))


class MailboxError(Exception):
    """Safe, actionable messages for users. Never embed provider responses."""


class _LinkError(Exception):
    pass


class _Stopped(Exception):
    pass


def _imap_date(value: date) -> str:
    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year:04d}"


def _text(raw: bytes, charset: str | None = None) -> str:
    for encoding in (charset, "utf-8", "gb18030", "big5"):
        if encoding:
            try:
                return raw.decode(encoding)
            except (UnicodeError, LookupError):
                pass
    return raw.decode("utf-8", "replace")


def _header(value: object) -> str:
    try:
        fragments = decode_header(str(value or ""))
        return "".join(_text(v, enc) if isinstance(v, bytes) else v for v, enc in fragments).strip()
    except (ValueError, TypeError):
        return str(value or "")[:500]


def _filename(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\\", "/").split("/")[-1])
    value = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", value).strip(" .") or "发票.pdf"
    if not value.lower().endswith(".pdf"):
        value += ".pdf"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\.", value, re.I):
        value = "_" + value
    return value[:-4][:120].rstrip(" .") + ".pdf"


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.links = []
        self.href = None
        self.anchor = []
        self.base = ""
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        values = dict(attrs)
        if tag == "base":
            self.base = values.get("href") or self.base
        if tag == "a":
            self.href = values.get("href")
            self.anchor = [values.get("title") or ""]

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        if self.ignored:
            return
        if tag == "a" and self.href:
            if len(self.links) < MAX_LINK_CANDIDATES:
                self.links.append((self.href, " ".join(self.anchor)))
            self.href = None

    def handle_data(self, data):
        if not self.ignored:
            self.text.append(data)
            if self.href:
                self.anchor.append(data)


def _body(message: Message, warning: Callable | None = None) -> tuple[str, list[str]]:
    texts, candidates = [], []
    budget = MAX_BODY_BYTES
    truncated = False
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            continue
        if len(data) > budget:
            truncated = True
        data = data[:budget]
        budget -= len(data)
        if not data:
            continue
        content = _text(data, part.get_content_charset())
        if part.get_content_type() == "text/html":
            parser = _HTML()
            parser.feed(content)
            texts.append(" ".join(parser.text))
            base = parser.base or str(part.get("Content-Base") or part.get("Content-Location") or "")
            for url, label in parser.links:
                if len(candidates) >= MAX_LINK_CANDIDATES:
                    truncated = True
                    break
                candidates.append((urllib.parse.urljoin(base, url), label))
            if len(parser.links) >= MAX_LINK_CANDIDATES:
                truncated = True
        else:
            texts.append(content)
            for match in re.finditer(r'https?://[^\s<>"\']+', content):
                if len(candidates) >= MAX_LINK_CANDIDATES:
                    truncated = True
                    break
                candidates.append((match[0].rstrip('.,;，。；)）>'), ""))
    if truncated and warning:
        warning("部分邮件正文过大或链接过多，只检查前 1 MB 正文及 1000 个链接候选，可能遗漏发票；附件仍正常检查。")
    body = "\n".join(texts)
    context = (body + " " + _header(message.get("Subject")) + " " + _header(message.get("From"))).lower()
    urls, seen = [], set()
    for url, label in candidates:
        if len(url) > 4096:
            continue
        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                continue
        except ValueError:
            continue
        combined = urllib.parse.unquote(url + " " + label).lower()
        if re.search(r"\.(?:xml|ofd)(?:$|[?#])", combined):
            continue
        if "pdf" in combined or "zip" in combined or (("下载" in combined or "download" in combined) and ("发票" in context or "invoice" in context or "发票" in combined or "invoice" in combined or "fapiao" in combined)):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return body, urls


def _public_target(url: str) -> tuple[urllib.parse.SplitResult, str, str, int]:
    """Resolve once; every answer must be public. Caller connects to that IP only."""
    try:
        if re.search(r"[\x00-\x20\x7f]", url):
            raise _LinkError("下载链接格式无效")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise _LinkError("只支持不含登录信息的 HTTP/HTTPS 下载链接")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port != (443 if parsed.scheme == "https" else 80):
            raise _LinkError("此链接使用特殊端口，请在原邮件中手动下载")
        hostname = parsed.hostname.encode("idna").decode("ascii")
        if "%" in hostname:
            raise _LinkError("下载地址无效")
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise _LinkError("无法解析下载地址")
        for answer in addresses:
            ip = ipaddress.ip_address(answer[4][0])
            if not ip.is_global or ip.is_multicast or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global):
                raise _LinkError("已阻止本机、内网或保留地址，请检查原邮件")
        return parsed, hostname, addresses[0][4][0], port
    except (ValueError, UnicodeError, OSError) as exc:
        raise _LinkError("无法解析下载地址，请从原邮件手动下载") from exc


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, hostname, ip, port, timeout, secure):
        super().__init__(hostname, port, timeout=timeout)
        self.pinned_ip = ip
        self.secure = secure

    def connect(self):
        self.sock = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        if self.secure:
            try:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
            except BaseException:
                self.sock.close()
                raise


def _fetch_link(url: str, deadline: float, cancelled: Callable) -> tuple[bytes, Message, str]:
    deadline = min(deadline, time.monotonic() + 20)
    current = url
    try:
        for _ in range(6):
            if cancelled() or time.monotonic() >= deadline:
                raise _LinkError("下载超时或已停止，请从原邮件手动下载")
            parsed, hostname, ip, port = _public_target(current)
            connection = _PinnedHTTP(hostname, ip, port, max(0.1, min(10, deadline - time.monotonic())), parsed.scheme == "https")
            try:
                path = urllib.parse.quote(parsed.path, safe="%/:@!$&'()*+,;=-._~") or "/"
                if parsed.query:
                    path += "?" + urllib.parse.quote(parsed.query, safe="%/?@!$&'()*+,;=:-._~")
                connection.request("GET", path, headers={"User-Agent": "InvoicePocket/2.0", "Accept": "application/pdf,application/zip", "Accept-Encoding": "identity"})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise _LinkError("下载链接跳转异常")
                    current = urllib.parse.urljoin(current, location)
                    continue
                if response.status != 200:
                    raise _LinkError("链接无法直接下载，可能需要登录或已过期")
                length = response.getheader("Content-Length")
                if length and int(length) > MAX_LINK_BYTES:
                    raise _LinkError("链接文件超过 10 MB，请手动下载")
                if response.getheader("Content-Encoding", "identity") not in {"identity", ""}:
                    raise _LinkError("链接返回了不支持的压缩响应，请手动下载")
                chunks, size = [], 0
                while True:
                    if cancelled() or time.monotonic() >= deadline:
                        raise _LinkError("下载超时或已停止，请从原邮件手动下载")
                    block = response.read1(min(65536, MAX_LINK_BYTES + 1 - size))
                    if not block:
                        break
                    chunks.append(block)
                    size += len(block)
                    if size > MAX_LINK_BYTES:
                        raise _LinkError("链接文件超过 10 MB，请手动下载")
                return b"".join(chunks), response.headers, current
            finally:
                connection.close()
        raise _LinkError("下载链接跳转过多，请手动下载")
    except _LinkError:
        raise
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise _LinkError("自动下载失败，请从原邮件手动下载") from exc


def _received_date(fetch_data) -> str:
    # SEARCH SINCE/BEFORE uses INTERNALDATE. Use that same date in returned metadata.
    for item in fetch_data or []:
        data = item[0] if isinstance(item, tuple) else item
        if isinstance(data, bytes):
            match = re.search(rb'INTERNALDATE "\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})', data)
            if match:
                try:
                    return date(int(match[3]), MONTHS.index(match[2].decode().title()) + 1, int(match[1])).isoformat()
                except (ValueError, IndexError):
                    pass
    return "日期未知"


def scan_mailbox(provider: str, email: str, auth_code: str, start_date: date, end_date: date,
                 on_progress: Callable, cancelled: Callable) -> dict:
    """Scan received dates inclusively, newest first. No disk writes or IMAP mutations."""
    if provider not in PROVIDERS:
        raise MailboxError("请选择 QQ 或 163 邮箱。")
    label, host, domains = PROVIDERS[provider]
    address = email.strip()
    if not address.isascii() or not re.fullmatch(r"[^@\s\x00-\x1f]{1,64}@[^@\s]+", address) or address.rsplit("@", 1)[-1].lower() not in domains:
        raise MailboxError(f"请输入与所选服务一致的 {label} 邮箱地址。")
    if not auth_code or len(auth_code) > 256 or any(ord(c) < 32 for c in auth_code):
        raise MailboxError("请输入有效的邮箱授权码，不要填写登录密码。")
    if start_date > end_date:
        raise MailboxError("开始日期不能晚于结束日期。")
    try:
        until = end_date + timedelta(days=1)
    except (OverflowError, TypeError) as exc:
        raise MailboxError("结束日期无效。") from exc
    result = {"files": [], "manual_links": [], "scanned": 0, "matched": 0, "warnings": []}
    deadline = time.monotonic() + MAX_SECONDS
    seen_hashes, used_names, seen_links = set(), set(), set()
    total_bytes = 0
    duplicates = 0
    link_attempts = 0
    mailbox = None
    authenticated = False

    def warning(message):
        if message not in result["warnings"] and len(result["warnings"]) < 20:
            result["warnings"].append(message)

    def checkpoint():
        if cancelled():
            raise _Stopped()
        if time.monotonic() >= deadline:
            warning("本次扫描达到 3 分钟上限，已保留找到的文件；请缩小日期范围后继续。")
            raise _Stopped()
        if len(result["files"]) >= MAX_FILES or total_bytes >= MAX_TOTAL_BYTES:
            warning("本次已达到 100 个 PDF 或 50 MB 上限，请缩小日期范围分批下载。")
            raise _Stopped()

    def progress(message, percent=None):
        value = {"message": message, "scanned": result["scanned"], "matched": result["matched"]}
        if percent is not None:
            value["percent"] = percent
        on_progress(value)

    def add_pdf(data, name, metadata, source):
        nonlocal total_bytes, duplicates
        if not data.startswith(b"%PDF-"):
            warning("部分标为 PDF 的附件内容无效，已跳过。")
            return
        digest = hashlib.sha256(data).digest()
        if digest in seen_hashes:
            duplicates += 1
            return
        if len(result["files"]) >= MAX_FILES or total_bytes + len(data) > MAX_TOTAL_BYTES:
            warning("部分 PDF 超出本次 100 个文件或 50 MB 限制，已跳过；请分批下载。")
            return
        filename = _filename(name)
        stem, suffix = filename[:-4], 2
        while filename.casefold() in used_names:
            filename = f"{stem}_{suffix}.pdf"
            suffix += 1
        used_names.add(filename.casefold())
        seen_hashes.add(digest)
        total_bytes += len(data)
        result["files"].append({"name": filename, "data": data, **metadata, "source": source})

    def process_message(raw, received):
        nonlocal link_attempts
        message = BytesParser(policy=policy.default).parsebytes(raw)
        subject = _header(message.get("Subject"))[:500] or "（无主题）"
        sender = _header(message.get("From"))[:500]
        display, address = parseaddr(sender)
        sender = display or address or sender or "未知发件人"
        body, urls = _body(message, warning)
        attachment_names = " ".join(_header(part.get_filename()) for part in message.walk())
        searchable = (subject + "\n" + sender + "\n" + body + "\n" + attachment_names).lower()
        if "发票" not in searchable and "invoice" not in searchable:
            return
        result["matched"] += 1
        metadata = {"subject": subject, "sender": sender, "date": received}
        for part in message.walk():
            checkpoint()
            if part.is_multipart():
                continue
            name = _header(part.get_filename())
            if part.get_content_type() == "application/pdf" or name.lower().endswith(".pdf"):
                data = part.get_payload(decode=True)
                if isinstance(data, bytes):
                    add_pdf(data, name or "发票.pdf", metadata, "附件")
        for url in urls:
            checkpoint()
            if url in seen_links:
                continue
            seen_links.add(url)
            if len(result["manual_links"]) >= 100:
                warning("待手动下载链接较多，只展示前 100 条，请缩小日期范围。")
                break
            reason = ""
            if link_attempts >= MAX_LINKS:
                reason = "本次自动处理链接已达 30 条，请手动下载或分批扫描"
            else:
                link_attempts += 1
                try:
                    data, headers, final_url = _fetch_link(url, deadline, cancelled)
                    if data.startswith(b"%PDF-"):
                        name = headers.get_filename() or urllib.parse.unquote(urllib.parse.urlsplit(final_url).path.split("/")[-1]) or "链接发票.pdf"
                        add_pdf(data, name, metadata, "链接")
                    elif data.startswith(b"PK\x03\x04"):
                        found = False
                        with zipfile.ZipFile(io.BytesIO(data)) as archive:
                            entries = archive.infolist()
                            if len(entries) > 1000:
                                raise _LinkError("压缩包内文件太多，请手动下载")
                            expanded = 0
                            for entry in entries:
                                checkpoint()
                                if entry.is_dir() or not entry.filename.lower().endswith(".pdf"):
                                    continue
                                if entry.file_size > MAX_MESSAGE_BYTES or expanded + entry.file_size > MAX_TOTAL_BYTES:
                                    raise _LinkError("压缩包解压后超过大小限制，请手动下载")
                                expanded += entry.file_size
                                with archive.open(entry) as member:
                                    pdf = member.read(min(MAX_MESSAGE_BYTES, MAX_TOTAL_BYTES - total_bytes) + 1)
                                if len(pdf) > MAX_MESSAGE_BYTES or len(pdf) > MAX_TOTAL_BYTES - total_bytes:
                                    raise _LinkError("压缩包内 PDF 超出本次容量，请分批下载")
                                if pdf.startswith(b"%PDF-"):
                                    found = True
                                    add_pdf(pdf, entry.filename, metadata, "链接")
                        if not found:
                            reason = "压缩包中没有可识别的 PDF 文件"
                    else:
                        reason = "链接返回了网页或其他格式，可能需要登录、验证码或手动操作"
                except _LinkError as exc:
                    reason = str(exc)
                except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
                    reason = "压缩包无法自动打开，请手动下载"
            if reason:
                result["manual_links"].append({"url": url, "subject": subject, "reason": reason, "date": received})

    try:
        checkpoint()
        progress(f"正在安全连接 {label} 邮箱…", 2)
        mailbox = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=15)
        try:
            mailbox.login(address, auth_code)
            authenticated = True
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"{label} 邮箱登录失败。请确认已开启 IMAP 服务，并使用新生成的授权码；异地登录限制需在邮箱官网解除。") from None
        # Do not retain the authorization code after authentication.
        auth_code = ""
        if provider == "163":
            status, _ = mailbox._simple_command("ID", '("name" "InvoicePocket" "version" "2.0" "vendor" "InvoicePocket")')
            if status != "OK":
                raise MailboxError("163 邮箱未接受客户端身份验证，请在邮箱官网确认 IMAP 权限后重试。")
        checkpoint()
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise MailboxError(f"无法读取 {label} 收件箱。请检查邮箱官网是否有安全验证提示，并确认 IMAP 已开启。")
        status, data = mailbox.uid("search", None, "SINCE", _imap_date(start_date), "BEFORE", _imap_date(until))
        if status != "OK":
            raise MailboxError("邮箱暂时无法搜索邮件，请稍后重试。")
        ids = data[0].split() if data and data[0] else []
        if len(ids) > MAX_MESSAGES:
            warning("日期范围内邮件超过 500 封，本次仅扫描最新 500 封；请缩小日期范围以免遗漏。")
        ids = ids[-MAX_MESSAGES:][::-1]
        progress(f"找到 {len(ids)} 封邮件，正在查找发票…", 5)
        for index, uid in enumerate(ids):
            checkpoint()
            result["scanned"] += 1
            try:
                status, size_data = mailbox.uid("fetch", uid, "(RFC822.SIZE INTERNALDATE)")
                metadata_bytes = b" ".join(item[0] if isinstance(item, tuple) else item for item in (size_data or []) if isinstance(item, (bytes, tuple)))
                size_match = re.search(rb"RFC822.SIZE\s+(\d+)", metadata_bytes, re.I)
                if status != "OK" or not size_match:
                    warning("部分邮件无法读取大小，已跳过；请在邮箱中检查。")
                    continue
                size = int(size_match[1])
                if size > MAX_MESSAGE_BYTES:
                    warning("部分邮件超过 20 MB，已跳过，请到原邮箱下载这些邮件的附件。")
                    continue
                checkpoint()
                status, fetched = mailbox.uid("fetch", uid, f"(BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)")
                if status != "OK":
                    warning("部分邮件读取失败，已继续处理其他邮件。")
                    continue
                raw = next((item[1] for item in (fetched or []) if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)), None)
                if raw is None or len(raw) > MAX_MESSAGE_BYTES or len(raw) != size:
                    warning("部分邮件内容不完整或超出限制，已跳过。")
                    continue
                process_message(raw, _received_date(size_data))
            except (ValueError, TypeError, UnicodeError):
                warning("部分邮件格式无法识别，已继续处理其他邮件。")
            progress(f"已检查 {result['scanned']} / {len(ids)} 封邮件，找到 {len(result['files'])} 个 PDF", min(97, 5 + int(92 * (index + 1) / len(ids))))
        if duplicates:
            warning(f"已自动去重 {duplicates} 个内容相同的 PDF。")
        progress(f"扫描完成，找到 {len(result['files'])} 个 PDF", 100)
        return result
    except _Stopped:
        return result
    except MailboxError:
        raise
    except (OSError, ssl.SSLError, TimeoutError) as exc:
        if result["scanned"]:
            warning("邮箱连接超时，本次结果可能不完整；已保留找到的文件，请缩小日期范围后重试。")
            return result
        raise MailboxError(f"暂时无法连接 {label} 邮箱，可能是网络超时或邮箱安全限制。请稍后重试，并检查邮箱官网的安全提醒。") from None
    except imaplib.IMAP4.error as exc:
        if result["scanned"]:
            warning("邮箱连接中断，本次结果可能不完整；已保留找到的文件，请稍后重试。")
            return result
        raise MailboxError(f"{label} 邮箱连接中断或拒绝访问。请检查 IMAP 权限、授权码和异地登录安全提醒后重试。") from None
    finally:
        auth_code = ""
        if mailbox is not None:
            try:
                if authenticated:
                    mailbox.logout()
                else:
                    mailbox.shutdown()
            except (OSError, imaplib.IMAP4.error):
                pass
