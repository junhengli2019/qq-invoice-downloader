"""HTTP download with per-hop public IP validation and a pinned socket.

Resolving once for validation and again for the connection permits DNS rebinding.
This module connects to the exact address checked, with normal TLS/SNI verification.
"""
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit


class UnsafeDownload(Exception):
    pass


def resolve_public(url):
    if len(url) > 4096 or any(ord(c) < 32 for c in url):
        raise UnsafeDownload("下载地址无效或过长")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError()
        if parsed.username is not None or parsed.password is not None:
            raise ValueError()
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if port not in {80, 443}:
            raise ValueError()
        host = parsed.hostname.encode('idna').decode('ascii')
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (ValueError, UnicodeError, OSError):
        raise UnsafeDownload("下载地址无效，或无法解析下载服务器") from None
    if not addresses:
        raise UnsafeDownload("无法解析下载服务器")
    for entry in addresses:
        address = entry[4][0]
        ip = ipaddress.ip_address(address)
        mapped = getattr(ip, 'ipv4_mapped', None)
        if not ip.is_global or ip.is_multicast or ip.is_reserved or (mapped is not None and not mapped.is_global):
            raise UnsafeDownload("为安全起见，不自动访问本机或内网地址")
    return parsed, host, port, addresses[0]


def fetch_public(url, max_bytes, timeout=20):
    for _ in range(6):
        parsed, host, port, address = resolve_public(url)
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        sock = socket.socket(address[0], socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(address[4])
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            connection.sock = sock
            host_header = '[' + host + ']' if ':' in host else host
            if port != (443 if parsed.scheme == 'https' else 80):
                host_header += ':' + str(port)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={
                'Host': host_header, 'User-Agent': 'InvoiceDownloader/2.0',
                'Accept': 'application/pdf, application/zip, */*;q=0.5',
                'Accept-Encoding': 'identity', 'Connection': 'close',
            })
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader('Location')
                if not location:
                    raise UnsafeDownload("下载服务器没有返回跳转地址")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise UnsafeDownload("下载服务器返回 HTTP %s" % response.status)
            declared = response.getheader('Content-Length')
            if declared and (not declared.isdigit() or int(declared) > max_bytes):
                raise UnsafeDownload("下载内容过大或长度无效，请在原邮件中下载")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise UnsafeDownload("下载内容过大，请在原邮件中下载")
            return data, response.headers, url
        except UnsafeDownload:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise UnsafeDownload("无法安全连接下载服务器，请在原邮件中打开链接") from None
        finally:
            connection.close()
            sock.close()
    raise UnsafeDownload("下载链接跳转次数过多")
