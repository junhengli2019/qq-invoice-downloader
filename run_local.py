"""Local launcher: isolated dependencies, loopback only, no background service."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent
ENV = ROOT / '.venv-local'
REQUIREMENTS = ROOT / 'requirements-local.txt'


def environment_python():
    return ENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def dependencies_ready(python):
    if not python.is_file():
        return False
    check = ('from importlib.metadata import version; '
             'assert version("Flask") == "3.1.2"; '
             'assert version("waitress") == "3.0.2"')
    return subprocess.run([str(python), '-c', check], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, cwd=ROOT).returncode == 0


def install():
    print('正在准备独立运行环境，只安装到项目的 .venv-local 文件夹。', flush=True)
    if not environment_python().is_file():
        venv.EnvBuilder(with_pip=True).create(ENV)
    subprocess.run([str(environment_python()), '-m', 'pip', '--isolated', 'install',
                    '--disable-pip-version-check', '--index-url', 'https://pypi.org/simple',
                    '-r', str(REQUIREMENTS)], check=True, cwd=ROOT)


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('端口必须是 1024–65535 的整数。') from None
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError('端口必须在 1024–65535 之间。')
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description='启动收票夹，只允许本机访问。按 Ctrl+C 停止。')
    parser.add_argument('--install', action='store_true', help='安装或修复项目依赖，然后退出')
    parser.add_argument('--port', type=port_number, default=8765, help='本机端口，默认 8765')
    parser.add_argument('--no-browser', action='store_true', help='启动后不自动打开浏览器')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        print('需要 Python 3.12 或更新版本。请从 https://www.python.org/downloads/ 安装。')
        return 1
    try:
        if args.install:
            install()
            print('安装完成。再次运行本启动器即可打开网站。')
            return 0
        if not dependencies_ready(environment_python()):
            print('首次启动需要从官方 PyPI 下载 Flask 和 Waitress 等依赖。')
            print('只安装到本项目，不修改系统 Python，不开放局域网或公网访问。')
            if not sys.stdin.isatty():
                print('请先运行：python run_local.py --install')
                return 1
            if input('现在安装？输入 y 确认，其他输入退出：').strip().lower() != 'y':
                return 0
            install()
        command = [str(environment_python()), '-m', 'webapp.local_server', '--port', str(args.port)]
        if args.no_browser:
            command.append('--no-browser')
        env = os.environ.copy()
        env['INVOICE_LOCAL_ONLY'] = '1'
        return subprocess.run(command, cwd=ROOT, env=env).returncode
    except subprocess.CalledProcessError:
        print('依赖安装未完成。请检查网络后重试 --install；不要把邮箱授权码填入终端。')
        return 1
    except (OSError, EOFError) as exc:
        print('启动未完成，请查看 docs/本机使用.md。错误类型：' + type(exc).__name__)
        return 1
    except KeyboardInterrupt:
        print('\n已请求停止。浏览器关闭不会停止服务器，下次仍需运行启动器。')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
