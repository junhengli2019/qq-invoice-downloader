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
