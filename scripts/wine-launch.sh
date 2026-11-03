#!/bin/sh
# 用 Wine 启动日文游戏:日文 locale,加大声音缓冲(CPU 忙时耳机里不噼啪响);
# 退出后重新应用 GNOME 夜灯(Wine 退出时会把显示器颜色曲线重置为原色)
# 用法: wine-launch.sh <游戏目录> <exe 文件名>
GAME_DIR=$1
EXE=$2
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine}" LANG=ja_JP.UTF-8 WINEDEBUG=-all PULSE_LATENCY_MSEC=60

cd "$GAME_DIR" || exit 1
wine "$GAME_DIR/$EXE"
wineserver -w

K=org.gnome.settings-daemon.plugins.color
if [ "$(gsettings get $K night-light-enabled)" = "true" ]; then
    gsettings set $K night-light-enabled false
    sleep 2
    gsettings set $K night-light-enabled true
fi
