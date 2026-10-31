#!/bin/bash
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo '请先从 https://www.python.org/downloads/ 安装 Python 3.12 或更新版本。'
  read -r -p '按回车退出。'
  exit 1
fi
python3 run_local.py
status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then read -r -p '按回车关闭此窗口。'; fi
exit "$status"
