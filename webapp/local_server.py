"""Foreground local-only WSGI server for Windows, macOS and Linux."""
from __future__ import annotations
import argparse
import os
import threading
import webbrowser

# Set before importing the application; never inherit hosted proxy trust locally.
os.environ['INVOICE_LOCAL_ONLY'] = '1'


def main(argv=None):
    from run_local import port_number
    parser = argparse.ArgumentParser(description='收票夹本机服务')
    parser.add_argument('--port', type=port_number, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    from waitress import create_server
    from webapp.app import app, LOCK, JOBS, POOL
    url = f'http://127.0.0.1:{args.port}'
    try:
        server = create_server(app, host='127.0.0.1', port=args.port, threads=8,
                               max_request_body_size=8192, channel_timeout=240,
                               inbuf_overflow=16384, outbuf_overflow=64 * 1024 * 1024,
                               outbuf_high_watermark=1024 * 1024, expose_tracebacks=False,
                               clear_untrusted_proxy_headers=True)
    except OSError:
        print(f'无法使用端口 {args.port}，可能已有程序运行。不会关闭其他程序。', flush=True)
        print('可关闭之前的收票夹窗口，或使用 --port 8766 换一个端口。', flush=True)
        return 1
    print('\n收票夹已启动：' + url, flush=True)
    print('仅这台电脑可用；此地址不能发给手机或其他人访问。', flush=True)
    print('请保留此窗口。停止：按 Ctrl+C；关闭网页不会停止服务器。\n', flush=True)
    opener = None
    if not args.no_browser:
        opener = threading.Timer(0.5, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if opener:
            opener.cancel()
        with LOCK:
            for job in JOBS.values():
                job['cancel'].set()
                job['files'].clear()
            JOBS.clear()
        server.close()
        POOL.shutdown(wait=False, cancel_futures=True)
        print('本机网页服务已停止，临时结果已清除。进行中的连接将在下个检查点结束。', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
