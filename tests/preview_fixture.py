"""LOCAL QA ONLY: synthetic invoices, no IMAP/network. Never used by Docker."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from web.app import create_app
import web.app as backend

def synthetic_scan(*args):
    time.sleep(0.6)
    pdf1 = b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n'
    pdf2 = b'%PDF-1.4\n% synthetic fixture two\n%%EOF\n'
    result = {
        'items': [
            {'id':'1','name':'演示-办公用品.pdf','size':len(pdf1),'subject':'[测试数据] 办公用品电子发票','sender':'fixture@example.invalid','date':'2026-10-28'},
            {'id':'2','name':'演示-交通.pdf','size':len(pdf2),'subject':'[测试数据] 交通电子发票 <script>alert(1)</script>','sender':'fixture@example.invalid','date':'2026-10-27'}
        ],
        'scanned':12,'matched':3,'skipped':1,'link_only':1,
        'warning':'测试夹具：以上均为合成数据，未连接任何邮箱。'
    }
    return result, {'1':pdf1,'2':pdf2}

backend.scan_mailbox = synthetic_scan
app = create_app({'TESTING':True,'APP_ACCESS_CODE':'fixture-only-access-2026'})

if __name__ == '__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8080);a=p.parse_args()
    app.run(host=a.host,port=a.port,debug=False)
