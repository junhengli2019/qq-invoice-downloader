"""Read-only, fixed-destination IMAP retrieval. Never follows email hyperlinks."""
import email.policy
import hashlib
import imaplib
import re
import ssl
import time
from datetime import date, timedelta
from email.parser import BytesParser

PROVIDERS = {
    'qq': {'name': 'QQ 邮箱', 'host': 'imap.qq.com', 'domains': ['qq.com', 'vip.qq.com', 'foxmail.com']},
    '163': {'name': '163 邮箱', 'host': 'imap.163.com', 'domains': ['163.com']},
}
MAX_MESSAGES = 300
MAX_MESSAGE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_FILES = 100
MAX_SECONDS = 150
MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()

class MailboxError(Exception):
    pass


def validate_input(data):
    provider = data.get('provider')
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise MailboxError('请选择 QQ 或 163 邮箱。')
    address = str(data.get('email', '')).strip().lower()
    if not re.fullmatch(r'[a-z0-9.!#$%&\'*+/=?^_`{|}~-]{1,64}@[a-z0-9.-]+', address) or address.rsplit('@', 1)[1] not in PROVIDERS[provider]['domains']:
        raise MailboxError('邮箱地址与所选服务商不匹配。')
    code = data.get('auth_code', '')
    if not isinstance(code, str) or not 8 <= len(code) <= 128 or any(ord(c) < 32 or ord(c) > 126 for c in code):
        raise MailboxError('请输入有效的 IMAP 授权码，不是邮箱登录密码。')
    try:
        start, end = date.fromisoformat(data.get('start', '')), date.fromisoformat(data.get('end', ''))
    except (ValueError, TypeError):
        raise MailboxError('请选择有效的开始与结束日期。') from None
    if start > end or (end - start).days > 365 or end >= date.max:
        raise MailboxError('日期顺序不正确，或范围超过 366 天。')
    keyword = str(data.get('keyword', '发票')).strip()
    if not 1 <= len(keyword) <= 40 or any(ord(c) < 32 for c in keyword):
        raise MailboxError('关键词应为 1–40 个字符。')
    if data.get('consent') is not True:
        raise MailboxError('请先确认授权本次只读访问。')
    return provider, address, code, start, end, keyword


def imap_date(day):
    return f'{day.day:02d}-{MONTHS[day.month-1]}-{day.year}'


def safe_filename(name):
    # PDF names are flattened; no filesystem writes or nested ZIP members.
    name = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', str(name or '发票.pdf'))
    name = name.strip(' .')[:100]
    if not name.lower().endswith('.pdf'):
        name += '.pdf'
    if not name or name.lower() in {'.pdf', '..pdf'}:
        name = '发票.pdf'
    return name


def scan_mailbox(provider, address, auth_code, start, end, keyword, imap_factory=None):
    factory = imap_factory or imaplib.IMAP4_SSL
    client = None
    started = time.monotonic()
    items, blobs, hashes = [], {}, set()
    scanned = matched = skipped = link_only = 0
    total = 0
    warning = ''
    try:
        client = factory(PROVIDERS[provider]['host'], 993, ssl_context=ssl.create_default_context(), timeout=18)
        client.login(address, auth_code)
        # NetEase may require IMAP ID; identify the app without embedding credentials.
        if provider == '163' and b'ID' in {c.upper() if isinstance(c, bytes) else c.upper().encode() for c in client.capabilities}:
            imaplib.Commands.setdefault('ID', ('AUTH', 'SELECTED'))
            typ, response = client._simple_command('ID', '("name" "InvoicePocket" "version" "1.0")')
            client._untagged_response(typ, response, 'ID')
        typ, _ = client.select('INBOX', readonly=True)
        if typ != 'OK':
            raise MailboxError('无法只读打开收件箱，请检查 IMAP 服务是否启用。')
        typ, result = client.uid('search', None, 'SINCE', imap_date(start), 'BEFORE', imap_date(end + timedelta(days=1)))
        if typ != 'OK':
            raise MailboxError('邮箱检索失败，请稍后重试。')
        ids = result[0].split() if result and result[0] else []
        if len(ids) > MAX_MESSAGES:
            warning = f'日期范围内超过 {MAX_MESSAGES} 封邮件，本次仅检查最新 {MAX_MESSAGES} 封，请缩小日期范围。'
        for uid in reversed(ids[-MAX_MESSAGES:]):
            if time.monotonic() - started > MAX_SECONDS or len(items) >= MAX_FILES:
                warning = '本次扫描已达时间或文件数量上限，请缩小日期范围继续。'
                break
            typ, meta = client.uid('fetch', uid, '(RFC822.SIZE)')
            raw_meta = b' '.join(x for x in (meta or []) if isinstance(x, bytes))
            size = re.search(rb'RFC822.SIZE\s+(\d+)', raw_meta, re.I)
            if typ != 'OK' or not size or int(size.group(1)) > MAX_MESSAGE_BYTES:
                skipped += 1
                continue
            # BODY.PEEK avoids marking unread messages as read.
            typ, body = client.uid('fetch', uid, '(BODY.PEEK[])')
            chunks = [x[1] for x in (body or []) if isinstance(x, tuple) and isinstance(x[1], bytes)]
            if typ != 'OK' or not chunks or sum(map(len, chunks)) > MAX_MESSAGE_BYTES:
                skipped += 1
                continue
            scanned += 1
            message = BytesParser(policy=email.policy.default).parsebytes(b''.join(chunks))
            subject = str(message.get('Subject', ''))[:300]
            sender = str(message.get('From', ''))[:200]
            mail_date = str(message.get('Date', ''))[:100]
            texts = []
            for part in message.walk():
                if part.get_content_type() in ('text/plain', 'text/html'):
                    try:
                        text = part.get_content()
                        if isinstance(text, str): texts.append(text[:200000])
                    except (LookupError, UnicodeError):
                        continue
            searchable = '\n'.join([subject, sender] + texts)
            if keyword.casefold() not in searchable.casefold():
                continue
            matched += 1
            has_pdf = False
            for part in message.walk():
                filename = part.get_filename()
                if not filename and part.get_content_type() != 'application/pdf':
                    continue
                if filename and not filename.lower().endswith('.pdf'):
                    continue
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes) or not payload.lstrip().startswith(b'%PDF-'):
                    skipped += 1
                    continue
                if total + len(payload) > MAX_TOTAL_BYTES or len(items) >= MAX_FILES:
                    warning = '已达到本次文件大小或数量上限，请缩小日期范围。'
                    break
                has_pdf = True
                digest = hashlib.sha256(payload).hexdigest()
                if digest in hashes:
                    continue
                hashes.add(digest)
                item_id = str(len(items) + 1)
                blobs[item_id] = payload
                total += len(payload)
                items.append({'id': item_id, 'name': safe_filename(filename), 'size': len(payload), 'subject': subject, 'sender': sender, 'date': mail_date})
            if not has_pdf and re.search(r'https?://', searchable, re.I):
                link_only += 1
        return {'items': items, 'scanned': scanned, 'matched': matched, 'skipped': skipped, 'link_only': link_only, 'warning': warning}, blobs
    except MailboxError:
        raise
    except imaplib.IMAP4.error:
        raise MailboxError('邮箱连接或认证被拒绝。请确认 IMAP 已开启、授权码正确；163 可能需要在网页端确认客户端登录。') from None
    except (OSError, TimeoutError, EOFError):
        raise MailboxError('暂时无法连接邮箱服务器，请稍后重试或缩小日期范围。') from None
    finally:
        if client:
            try: client.logout()
            except Exception: pass
