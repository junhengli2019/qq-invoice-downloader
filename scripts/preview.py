import argparse, os
p=argparse.ArgumentParser();p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',default='4173');p.add_argument('--strictPort',action='store_true');a=p.parse_args()
os.execv('.venv/bin/gunicorn',['gunicorn','--bind',a.host+':'+a.port,'--workers','1','--threads','4','web.app:app'])
