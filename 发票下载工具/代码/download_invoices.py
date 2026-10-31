"""从 QQ 邮箱下载日期范围内的发票 PDF。仅使用 Python 标准库。"""

from __future__ import annotations

import imaplib
import time
from contextvars import ContextVar
from safe_download import fetch_public, resolve_public, UnsafeDownload
import io
import re
import socket
import ssl
import sys
import unicodedata
import urllib.parse
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple


IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
DOWNLOAD_TIMEOUT_SECONDS = 25
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_UNZIPPED_PDF_BYTES = 100 * 1024 * 1024
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *("COM%d" % number for number in range(1, 10)),
    *("LPT%d" % number for number in range(1, 10)),
}
IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class DownloadError(Exception):
    """可以安全展示给普通用户的下载错误。"""


@dataclass(frozen=True)
class DownloadLimits:
    max_messages: int = 300
    max_message_bytes: int = 12 * 1024 * 1024
    max_output_bytes: int = 40 * 1024 * 1024
    max_files: int = 100
    max_links: int = 80
    max_seconds: int = 180


@dataclass
class _Budget:
    limits: DownloadLimits
    started: float = field(default_factory=time.monotonic)
    output_bytes: int = 0
    links: int = 0

    def check(self):
        if time.monotonic() - self.started > self.limits.max_seconds:
            raise DownloadError("整理超过 3 分钟，请缩小日期范围后重试。")


_budget = ContextVar("invoice_budget", default=None)


class LinkDownloadError(Exception):
    """单个正文链接无法自动下载。"""


@dataclass
class ManualLink:
    mail_date: str
    sender: str
    subject: str
    url: str
    reason: str


@dataclass
class DownloadResult:
    scanned_messages: int = 0
    matched_messages: int = 0
    saved_pdfs: int = 0
    skipped_existing: int = 0
    skipped_non_pdf_attachments: int = 0
    manual_links: int = 0
    failed_items: int = 0
    logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class _Reporter:
    def __init__(
        self,
        result: DownloadResult,
        callback: Optional[Callable[[str], None]],
        secrets: Sequence[str],
    ) -> None:
        self.result = result
        self.callback = callback
        self.secrets = tuple(secret for secret in secrets if secret)

    def log(self, message: str) -> None:
        safe = str(message).replace("\r", " ").replace("\n", " ")
        for secret in self.secrets:
            safe = safe.replace(secret, "******")
        safe = safe[:1000]
        self.result.logs.append(safe)
        del self.result.logs[:-400]
        if self.callback:
            self.callback(safe)


class _InvoiceHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[Tuple[str, str]] = []
        self.visible_text: List[str] = []
        self.base_href = ""
        self._href: Optional[str] = None
        self._anchor_text: List[str] = []
        self._anchor_title = ""
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "base" and not self.base_href:
            self.base_href = values.get("href", "").strip()
        if tag.lower() == "a":
            self._href = values.get("href", "").strip()
            self._anchor_text = []
            self._anchor_title = values.get("title", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag.lower() == "a" and self._href is not None:
            text = " ".join(self._anchor_text + [self._anchor_title]).strip()
            self.links.append((self._href, text))
            self._href = None
            self._anchor_text = []
            self._anchor_title = ""

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = data.strip()
        if not text:
            return
        self.visible_text.append(text)
        if self._href is not None:
            self._anchor_text.append(text)



def _decode_bytes(raw: bytes, declared_charset: Optional[str] = None) -> str:
    charsets = [declared_charset, "utf-8", "gb18030", "big5"]
    for charset in charsets:
        if not charset:
            continue
        try:
            return raw.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def decode_mime_header(value: Optional[str]) -> str:
    if not value:
        return ""
    decoded: List[str] = []
    try:
        fragments = decode_header(str(value))
    except (ValueError, TypeError):
        return str(value)
    for fragment, charset in fragments:
        if isinstance(fragment, bytes):
            decoded.append(_decode_bytes(fragment, charset))
        else:
            decoded.append(fragment)
    return "".join(decoded).strip()


def _decode_text_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return _decode_bytes(payload, part.get_content_charset())
    raw = part.get_payload()
    return raw if isinstance(raw, str) else ""


def _message_date(message: Message) -> Optional[date]:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw)).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _body_parts(message: Message) -> Tuple[str, List[Tuple[str, str]]]:
    text_parts: List[str] = []
    html_parts: List[Tuple[str, str]] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        text = _decode_text_part(part)
        if content_type == "text/plain":
            text_parts.append(text)
        else:
            base = str(part.get("Content-Base") or part.get("Content-Location") or "")
            html_parts.append((text, base))
            parser = _InvoiceHTMLParser()
            try:
                parser.feed(text)
                text_parts.append(" ".join(parser.visible_text))
            except Exception:
                text_parts.append(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(text_parts), html_parts


def _sender_label(sender_header: str) -> str:
    decoded = decode_mime_header(sender_header)
    display_name, address = parseaddr(decoded)
    return (display_name or address or decoded or "未知发件人").strip()


def _short_text(value: str, length: int = 80) -> str:
    return re.sub(r"\s+", " ", value).strip()[:length] or "（无）"


def clean_windows_component(value: str, default: str = "发票") -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = default
    stem = value.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        value = "_" + value
    return value[:180].rstrip(" .") or default


def build_pdf_filename(mail_date: str, sender: str, original_name: str) -> str:
    original_name = original_name.replace("\\", "/").rsplit("/", 1)[-1]
    original_name = clean_windows_component(original_name, "发票.pdf")
    if not original_name.lower().endswith(".pdf"):
        original_name = original_name.rsplit(".", 1)[0] + ".pdf"
    prefix = "%s_%s_" % (
        clean_windows_component(mail_date, "日期未知")[:16],
        clean_windows_component(sender, "未知发件人")[:70],
    )
    available = max(1, 180 - len(prefix) - 4)
    stem = original_name[:-4][:available].rstrip(" .") or "发票"
    return clean_windows_component(prefix + stem + ".pdf", "发票.pdf")


def _imap_date(value: date) -> str:
    return "%02d-%s-%04d" % (value.day, IMAP_MONTHS[value.month - 1], value.year)


def _fetch_message_bytes(fetch_data: object) -> Optional[bytes]:
    if not isinstance(fetch_data, list):
        return None
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def _is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def _save_pdf(
    data: bytes,
    project_root: Path,
    filename: str,
    result: DownloadResult,
    reporter: _Reporter,
) -> bool:
    if not _is_pdf(data):
        raise ValueError("内容不是有效的 PDF 文件")
    download_dir = project_root / "downloads"
    destination = download_dir / filename
    if destination.exists():
        result.skipped_existing += 1
        reporter.log("已存在，跳过：%s" % filename)
        return False
    budget = _budget.get()
    if budget:
        budget.check()
        if result.saved_pdfs >= budget.limits.max_files or budget.output_bytes + len(data) > budget.limits.max_output_bytes:
            raise DownloadError("本次发票超过 100 份或 40 MB，请缩小日期范围后重试。")
        budget.output_bytes += len(data)
    download_dir.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with destination.open("xb") as output:
            created = True
            output.write(data)
    except FileExistsError:
        result.skipped_existing += 1
        reporter.log("已存在，跳过：%s" % filename)
        return False
    except OSError:
        if created:
            try:
                destination.unlink()
            except OSError:
                pass
        raise
    result.saved_pdfs += 1
    reporter.log("已保存：%s" % filename)
    return True


def _is_excluded_format(text: str) -> bool:
    decoded = urllib.parse.unquote(text).lower()
    return bool(
        re.search(r"(?:\.|[?&#_/=-])(xml|ofd)(?:$|[?&#_/=-])", decoded)
        or re.search(r"\b(xml|ofd)\b", decoded)
    )


def _is_invoice_link(href: str, anchor_text: str, document_text: str) -> bool:
    combined = urllib.parse.unquote("%s %s" % (href, anchor_text)).lower()
    if _is_excluded_format(combined):
        return False
    has_download = "下载" in combined or "download" in combined
    has_pdf = "pdf" in combined
    has_invoice = "发票" in combined or "invoice" in combined or "fapiao" in combined
    document_is_invoice = "发票" in document_text
    return has_pdf or (has_invoice and has_download) or (has_download and document_is_invoice)


def extract_invoice_links(html: str, content_base: str = "") -> List[Tuple[str, str]]:
    parser = _InvoiceHTMLParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    base = parser.base_href or content_base
    document_text = " ".join(parser.visible_text)
    links: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for href, anchor_text in parser.links:
        if not href or not _is_invoice_link(href, anchor_text, document_text):
            continue
        resolved = urllib.parse.urljoin(base, href) if base else href
        scheme = urllib.parse.urlsplit(resolved).scheme.lower()
        if scheme and scheme not in {"http", "https"}:
            continue
        if resolved not in seen:
            links.append((resolved, anchor_text))
            seen.add(resolved)
    return links


def _validate_remote_url(url: str) -> None:
    try:
        resolve_public(url)
    except UnsafeDownload as exc:
        raise LinkDownloadError(str(exc)) from None


def _fetch_link(url: str) -> Tuple[bytes, Message, str]:
    budget = _budget.get()
    limit = budget.limits.max_message_bytes if budget else MAX_DOWNLOAD_BYTES
    if budget:
        budget.check()
        budget.links += 1
        if budget.links > budget.limits.max_links:
            raise DownloadError("下载链接较多，请缩小日期范围后重试。")
    try:
        return fetch_public(url, limit, DOWNLOAD_TIMEOUT_SECONDS)
    except UnsafeDownload as exc:
        raise LinkDownloadError(str(exc)) from None


def _response_filename(headers: Message, final_url: str, fallback: str) -> str:
    disposition = headers.get("Content-Disposition")
    if disposition:
        message = Message()
        message["Content-Disposition"] = disposition
        filename = message.get_filename()
        if filename:
            return decode_mime_header(filename)
    url_name = urllib.parse.unquote(Path(urllib.parse.urlsplit(final_url).path).name)
    return url_name or fallback


def _html_failure_reason(data: bytes, content_type: str) -> str:
    text = _decode_bytes(data[:200000]).lower()
    checks = (
        (("扫码", "二维码", "qr code"), "链接打开后需要扫码"),
        (("短信", "验证码", "verification code"), "链接打开后需要短信或验证码"),
        (("登录", "login", "sign in"), "链接打开后需要登录"),
        (("过期", "expired"), "下载链接可能已经过期"),
    )
    for words, reason in checks:
        if any(word in text for word in words):
            return reason
    if "html" in content_type or "<html" in text or "<!doctype" in text:
        return "链接返回了网页，无法直接取得 PDF"
    return "返回内容不是可识别的 PDF 或 ZIP"


def _save_zip_pdfs(
    data: bytes,
    mail_date: str,
    sender: str,
    project_root: Path,
    result: DownloadResult,
    reporter: _Reporter,
) -> int:
    saved_before = result.saved_pdfs
    found_pdf = False
    total_unzipped = 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile):
        raise LinkDownloadError("服务器返回的 ZIP 文件已损坏")
    with archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                continue
            found_pdf = True
            total_unzipped += info.file_size
            if info.file_size > MAX_DOWNLOAD_BYTES or total_unzipped > MAX_UNZIPPED_PDF_BYTES:
                result.failed_items += 1
                reporter.log("ZIP 中的 PDF 过大，已跳过：%s" % _short_text(info.filename))
                continue
            budget = _budget.get()
            if budget:
                budget.check()
                if info.file_size > budget.limits.max_message_bytes or total_unzipped > budget.limits.max_output_bytes:
                    raise DownloadError("压缩包内的发票过大，请在原邮件中手动下载。")
            try:
                with archive.open(info) as entry:
                    pdf_data = entry.read((budget.limits.max_message_bytes if budget else MAX_DOWNLOAD_BYTES) + 1)
                if len(pdf_data) > (budget.limits.max_message_bytes if budget else MAX_DOWNLOAD_BYTES):
                    raise DownloadError("压缩包内的发票过大，请在原邮件中手动下载。")
                if not _is_pdf(pdf_data):
                    raise ValueError("文件内容不是 PDF")
                filename = build_pdf_filename(mail_date, sender, info.filename)
                _save_pdf(pdf_data, project_root, filename, result, reporter)
            except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
                result.failed_items += 1
                reporter.log("ZIP 中的 PDF 处理失败：%s（%s）" % (_short_text(info.filename), type(exc).__name__))
    if not found_pdf:
        raise LinkDownloadError("ZIP 中没有 PDF 文件")
    return result.saved_pdfs - saved_before


def _manual_key(record: ManualLink) -> Tuple[str, str, str, str]:
    return record.mail_date, record.sender, record.subject, record.url


def _single_line(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value).strip()


def _write_manual_links(project_root: Path, records: List[ManualLink]) -> None:
    if not records:
        return
    destination = project_root / "需要手动下载的发票链接.txt"
    existing = ""
    if destination.exists():
        try:
            existing = destination.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    new_records = [record for record in records if ("下载链接：%s" % record.url) not in existing]
    if not new_records:
        return
    try:
        with destination.open("a", encoding="utf-8", newline="\n") as output:
            if not existing:
                output.write("需要手动下载的发票链接\n")
                output.write("=" * 30 + "\n")
            elif not existing.endswith("\n"):
                output.write("\n")
            for record in new_records:
                output.write("\n邮件日期：%s\n" % _single_line(record.mail_date))
                output.write("发件人：%s\n" % _single_line(record.sender))
                output.write("邮件主题：%s\n" % _single_line(record.subject))
                output.write("下载链接：%s\n" % _single_line(record.url))
                output.write("失败原因：%s\n" % _single_line(record.reason))
                output.write("-" * 30 + "\n")
    except OSError as exc:
        raise DownloadError(
            "无法写入“需要手动下载的发票链接.txt”，请检查文件是否被占用或目录是否可写。"
        ) from exc


def _process_link(
    url: str,
    mail_date: str,
    sender: str,
    subject: str,
    project_root: Path,
    result: DownloadResult,
    reporter: _Reporter,
) -> Optional[str]:
    try:
        data, headers, final_url = _fetch_link(url)
        content_type = str(headers.get("Content-Type") or "").lower()
        if _is_pdf(data):
            original_name = _response_filename(headers, final_url, "下载发票.pdf")
            filename = build_pdf_filename(mail_date, sender, original_name)
            _save_pdf(data, project_root, filename, result, reporter)
            return None
        if data.startswith(b"PK\x03\x04") or zipfile.is_zipfile(io.BytesIO(data)):
            _save_zip_pdfs(data, mail_date, sender, project_root, result, reporter)
            return None
        return _html_failure_reason(data, content_type)
    except LinkDownloadError as exc:
        return str(exc)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        result.failed_items += 1
        return "自动处理失败（%s）" % type(exc).__name__


def _process_message(
    message: Message,
    start_date: date,
    end_date: date,
    project_root: Path,
    result: DownloadResult,
    reporter: _Reporter,
    manual_records: List[ManualLink],
    manual_seen: Set[Tuple[str, str, str, str]],
) -> None:
    subject = decode_mime_header(message.get("Subject")) or "（无主题）"
    sender_header = str(message.get("From") or "")
    sender = _sender_label(sender_header)
    mail_date_value = _message_date(message)
    mail_date = mail_date_value.isoformat() if mail_date_value else "日期未知"
    if mail_date_value and not start_date <= mail_date_value <= end_date:
        reporter.log("邮件头日期不在范围内，跳过：%s" % _short_text(subject))
        return

    body_text, html_parts = _body_parts(message)
    if "发票" not in "\n".join((subject, decode_mime_header(sender_header), body_text)):
        return
    result.matched_messages += 1
    reporter.log("处理发票邮件：%s" % _short_text(subject))

    for part in message.walk():
        if part.is_multipart():
            continue
        filename = decode_mime_header(part.get_filename())
        disposition = part.get_content_disposition()
        is_attachment = (
            disposition == "attachment"
            or bool(filename)
            or part.get_content_type().lower() == "application/pdf"
        )
        if not is_attachment:
            continue
        content_type = part.get_content_type().lower()
        is_pdf_candidate = content_type == "application/pdf" or filename.lower().endswith(".pdf")
        if not is_pdf_candidate:
            result.skipped_non_pdf_attachments += 1
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or not _is_pdf(data):
            result.failed_items += 1
            reporter.log("PDF 附件内容无效，已跳过：%s" % _short_text(filename or "未命名附件"))
            continue
        try:
            output_name = build_pdf_filename(mail_date, sender, filename or "发票.pdf")
            _save_pdf(data, project_root, output_name, result, reporter)
        except OSError as exc:
            result.failed_items += 1
            reporter.log("PDF 附件保存失败：%s（%s）" % (_short_text(filename), type(exc).__name__))

    links: List[Tuple[str, str]] = []
    seen_urls: Set[str] = set()
    for html, base in html_parts:
        for url, anchor_text in extract_invoice_links(html, base):
            if url not in seen_urls:
                seen_urls.add(url)
                links.append((url, anchor_text))
    for url, _anchor_text in links:
        reporter.log("尝试正文下载链接：%s" % _safe_url_for_log(url))
        reason = _process_link(url, mail_date, sender, subject, project_root, result, reporter)
        if reason:
            record = ManualLink(mail_date, sender, subject, url, reason)
            key = _manual_key(record)
            if key not in manual_seen:
                manual_seen.add(key)
                manual_records.append(record)
                result.manual_links += 1
            reporter.log("需要手动下载：%s" % reason)


def _safe_url_for_log(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return "相对链接（已隐藏参数）"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:300]
    except ValueError:
        return "无法解析的链接"


def download_invoices(
    email_address: str,
    authorization_code: str,
    start_date: date,
    end_date: date,
    project_root: Path,
    progress_callback: Optional[Callable[[str], None]] = None,
    limits: Optional[DownloadLimits] = None,
) -> DownloadResult:
    """下载发票并返回统计结果；授权码不会进入日志或磁盘。"""
    if not re.fullmatch(r"[^@\s]{1,64}@(qq\.com|vip\.qq\.com|foxmail\.com)", email_address, re.IGNORECASE):
        raise DownloadError("QQ 邮箱地址格式不正确。")
    if not authorization_code:
        raise DownloadError("IMAP 授权码不能为空。")
    if start_date > end_date:
        raise DownloadError("起始日期不能晚于截止日期。")
    try:
        end_exclusive = end_date + timedelta(days=1)
    except OverflowError as exc:
        raise DownloadError("截止日期无效。") from exc

    project_root = Path(project_root).resolve()
    result = DownloadResult()
    reporter = _Reporter(result, progress_callback, (authorization_code,))
    manual_records: List[ManualLink] = []
    manual_seen: Set[Tuple[str, str, str, str]] = set()
    mailbox = None
    logged_in = False

    budget = _Budget(limits) if limits else None
    token = _budget.set(budget)
    reporter.log("正在连接 QQ 邮箱 IMAP 服务……")
    try:
        try:
            mailbox = imaplib.IMAP4_SSL(
                IMAP_HOST,
                IMAP_PORT,
                ssl_context=ssl.create_default_context(),
                timeout=30,
            )
        except TypeError:  # 兼容较早的 Python 3
            mailbox = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=ssl.create_default_context())
        try:
            mailbox.login(email_address, authorization_code)
            logged_in = True
        except imaplib.IMAP4.error as exc:
            raise DownloadError("QQ 邮箱登录失败。请检查邮箱、IMAP 授权码，并确认已开启 IMAP 服务。") from exc

        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise DownloadError("无法打开 QQ 邮箱的收件箱（INBOX）。")
        status, search_data = mailbox.search(None, "SINCE", _imap_date(start_date), "BEFORE", _imap_date(end_exclusive))
        if status != "OK":
            raise DownloadError("QQ 邮箱搜索邮件失败，请稍后重试。")
        message_ids = search_data[0].split() if search_data and search_data[0] else []
        reporter.log("日期范围内找到 %d 封邮件，开始检查是否包含“发票”。" % len(message_ids))

        if limits and len(message_ids) > limits.max_messages:
            raise DownloadError("日期范围内邮件超过 300 封，请缩小日期范围后重试。")
        for message_id in message_ids:
            if budget:
                budget.check()
            reporter.log("正在检查第 %d/%d 封邮件。" % (result.scanned_messages + 1, len(message_ids)))
            result.scanned_messages += 1
            try:
                if limits:
                    size_status, size_data = mailbox.fetch(message_id, "(RFC822.SIZE)")
                    size_bytes = b" ".join(part for part in (size_data or []) if isinstance(part, bytes))
                    size_match = re.search(rb"RFC822.SIZE\s+(\d+)", size_bytes)
                    if size_status != "OK" or not size_match:
                        raise ValueError("无法确认邮件大小")
                    if int(size_match.group(1)) > limits.max_message_bytes:
                        raise DownloadError("有邮件超过 12 MB，请缩小日期范围或在原邮箱下载。")
                    query = "(BODY.PEEK[]<0.%d>)" % (limits.max_message_bytes + 1)
                else:
                    query = "(BODY.PEEK[])"
                status, fetch_data = mailbox.fetch(message_id, query)
                raw_message = _fetch_message_bytes(fetch_data)
                if status != "OK" or raw_message is None:
                    raise ValueError("邮件内容为空")
                if limits and len(raw_message) > limits.max_message_bytes:
                    raise DownloadError("有邮件超过 12 MB，请在原邮箱下载。")
                message = BytesParser(policy=policy.default).parsebytes(raw_message)
                _process_message(
                    message,
                    start_date,
                    end_date,
                    project_root,
                    result,
                    reporter,
                    manual_records,
                    manual_seen,
                )
            except DownloadError:
                raise
            except Exception as exc:
                result.failed_items += 1
                reporter.log("一封邮件处理失败，已继续下一封（%s）。" % type(exc).__name__)

        _write_manual_links(project_root, manual_records)
        reporter.log(
            "处理完成：保存 %d 个 PDF，跳过已存在 %d 个，需要手动下载 %d 个。"
            % (result.saved_pdfs, result.skipped_existing, result.manual_links)
        )
        return result
    except DownloadError:
        raise
    except (socket.gaierror, ssl.SSLError, TimeoutError, OSError) as exc:
        raise DownloadError("无法连接 QQ 邮箱。请检查网络、防火墙和电脑系统时间后重试。") from exc
    except imaplib.IMAP4.error as exc:
        raise DownloadError("QQ 邮箱返回错误，请稍后重试。") from exc
    finally:
        _budget.reset(token)
        if mailbox is not None:
            try:
                if logged_in:
                    mailbox.logout()
                else:
                    mailbox.shutdown()
            except Exception:
                pass


def _self_check() -> None:
    assert clean_windows_component('a<b>c?.pdf') == "a_b_c_.pdf"
    links = extract_invoice_links('<p>电子发票</p><a href="https://example.com/a.pdf">下载</a>')
    assert links == [("https://example.com/a.pdf", "下载")]
    print("自检通过。")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_check()
    else:
        print("请返回项目根目录，双击“双击此文件开始下载.bat”使用本工具。")
