#!/usr/bin/env bash
# ============================================================
# noelle 安装脚本 (Linux / macOS)
#
# 用法:
#   git clone <repo> ~/noelle-ciallo
#   cd ~/noelle-ciallo && ./install.sh
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
TARGET="$BIN_DIR/noelle"

info() { printf '\033[1;36m[noelle]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[警告]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 1. Python ----------
PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        ver=$("$c" -c 'import sys;print("%d%02d"%sys.version_info[:2])' 2>/dev/null || echo 0)
        if [ "$ver" -ge 309 ] 2>/dev/null; then PY="$c"; break; fi
    fi
done
[ -n "$PY" ] || die "需要 Python 3.9+，未找到。Arch: sudo pacman -S python"
info "Python $("$PY" -c 'import sys;print(sys.version.split()[0])') ($PY)"

# ---------- 2. 部署启动器 ----------
mkdir -p "$BIN_DIR"
cat > "$TARGET" <<EOF
#!/usr/bin/env bash
exec "$PY" "$ROOT/run.py" "\$@"
EOF
chmod +x "$TARGET"
chmod +x "$ROOT/run.py"
info "启动器 → $TARGET"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR 不在 PATH，请加到 shell 配置里" ;;
esac

# ---------- 3. 初始化配置 ----------
if [ ! -f "$HOME/.noelle/config/config.jsonc" ]; then
    "$TARGET" init >/dev/null 2>&1 && info "已初始化配置: ~/.noelle"
else
    info "配置已存在，保留"
fi

# ---------- 3.5 Rust 可视化 TUI（可选） ----------
if command -v cargo >/dev/null 2>&1 && [ -f tui/Cargo.toml ]; then
    if (cd tui && cargo build --release -q 2>/dev/null); then
        install -m 755 tui/target/release/noelle-tui "$BIN_DIR/noelle-tui"
        info "已安装 noelle-tui（noelle tui 启动）"
    else
        warn "noelle-tui 编译失败，跳过（不影响主程序）"
    fi
else
    warn "未检测到 cargo，跳过 Rust TUI（之后可 cd tui && cargo build --release）"
fi

# ---------- 4. shell 集成（可选） ----------
if [ "${1:-}" = "--shell" ] && command -v fish >/dev/null 2>&1; then
    "$TARGET" fish-init >/dev/null && info "fish hook 已安装"
fi

# ---------- 5. 完成 ----------
cat <<'MSG'

────────────────────────────────────────────────────────
 安装完成

   首次配置：  noelle oobe          # 选模型 + 填 key + 写人格
   普通对话：  noelle               # 或 noelle normal
   开发模式：  noelle dev           # 无人格
   单次提问：  noelle ask "问题"
   查看工具：  noelle tools
   路径信息：  noelle paths

   数据目录：  ~/.noelle
   可视化设置：noelle tui            # Rust TUI：设置 + 插件
   终端集成：  noelle fish-init     # fish
               noelle bash-init     # bash
               noelle zsh-init      # zsh
────────────────────────────────────────────────────────
MSG
