#!/bin/sh
# 外挂翻译窗;清掉 ROS 等的 PYTHONPATH,免得混进别的包。VNTRANS_PY 指向装好依赖的 python
# readlink -f:从 ~/.local/bin 的软链接调用时也能找到仓库里的 vn_translate.py
HERE=$(dirname "$(readlink -f "$0")")
# flock:已经开着就直接退出。从 dock 点两下时不会开出两份,抢显存、一次按键翻译两遍;程序退出后锁自动释放
exec flock -n "${XDG_RUNTIME_DIR:-/tmp}/vn-translate.lock" env -u PYTHONPATH "${VNTRANS_PY:-/mnt/data4tb/envs/vntrans/bin/python}" "$HERE/../vn_translate.py"
