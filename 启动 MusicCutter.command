#!/bin/zsh
cd "${0:A:h}" || exit 1
task_python=""
for candidate in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 /opt/homebrew/bin/python3; do
  if [[ -x "$candidate" ]] && "$candidate" -c 'import tkinter' >/dev/null 2>&1; then
    task_python="$candidate"
    break
  fi
done
if [[ -z "$task_python" ]]; then
  task_python="$(command -v python3)"
fi
if [[ -z "$task_python" ]]; then
  print '未找到 Python 3。请安装带 Tkinter 的 Python。'
  read -k 1 '?按任意键关闭…'
  exit 1
fi
"$task_python" music_cutter_gui.py
task_status=$?
if (( task_status != 0 )); then
  print '启动失败。请查看上方错误或 userdata/logs/musiccutter.log。'
  read -k 1 '?按任意键关闭…'
fi
exit "$task_status"
