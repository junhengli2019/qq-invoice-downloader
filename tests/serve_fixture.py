import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'发票下载工具/代码'))
from public_app import create_app
from public_jobs import Jobs
from fixtures import run_fixture

if __name__=='__main__':
    manager=Jobs(root=Path(tempfile.mkdtemp(prefix='invoice-test-')),target=run_fixture)
    create_app(manager,'http://127.0.0.1:8767',probe=False).run(host='127.0.0.1',port=8767,debug=False)
