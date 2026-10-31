import os

bind = '0.0.0.0:' + os.environ.get('PORT', '8766')
# Jobs and visitor capabilities live in one process, never in a shared directory.
workers = 1
worker_class = 'gthread'
threads = 8
timeout = 60
graceful_timeout = 15
accesslog = None
errorlog = '-'
capture_output = False
limit_request_line = 4094
limit_request_field_size = 4094
chdir = '发票下载工具/代码'


def on_starting(server):
    # Render's default GUNICORN_CMD_ARGS enables --preload. Threads created in
    # that parent do not survive in the serving child: reject that deployment.
    if server.cfg.preload_app or server.cfg.workers != 1:
        raise RuntimeError('Invoice jobs require one worker and no preload. Set GUNICORN_CMD_ARGS to an empty value.')
