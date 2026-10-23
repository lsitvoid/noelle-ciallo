#!/usr/bin/env bash
# ============================================================
# noelle 卸载脚本
# 只删部署，不碰你的数据（除非加 --purge）
# ============================================================
set -uo pipefail

TARGET="$HOME/.local/bin/noelle"

echo "将删除："
echo "  · $TARGET"
echo "  · shell hook (若存在)"
[ "${1:-}" = "--purge" ] && echo "  · ~/.noelle （全部配置与数据！）"
echo
read -rp "确认？[y/N] " ans
case "$ans" in y|Y) ;; *) echo "已取消"; exit 0 ;; esac

[ -f "$TARGET" ] && rm -f "$TARGET" && echo "✓ 已删除启动器"

for f in "$HOME/.config/fish/conf.d/noelle.fish"; do
    if [ -f "$f" ] && grep -q noelle "$f" 2>/dev/null; then
        rm -f "$f" && echo "✓ 已移除 $f"
    fi
done

command -v noelle >/dev/null 2>&1 && noelle daemon stop 2>/dev/null

if [ "${1:-}" = "--purge" ]; then
    rm -rf "$HOME/.noelle" && echo "✓ 已删除 ~/.noelle"
fi

echo "卸载完成。"
