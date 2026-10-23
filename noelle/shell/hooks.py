"""生成 fish / bash / zsh 的集成脚本。

设计要点：
  · hook 只做一件事：回车时判断该行是不是自然语言，是就调 noelle
  · 判断走 noelle --shell-classify（独立进程，<60ms），避免污染 shell 状态
  · 每个 shell 的函数前缀用 __noelle_，不会与其它工具冲突
"""

from __future__ import annotations

FISH = r"""# noelle shell 集成 · fish
# 由 `noelle fish-init` 生成。重新生成请再次运行该命令。

function __noelle_bin
    command -v noelle
end

function __noelle_classify
    set -l line (commandline)
    test -z "$line"; and return 1
    set -l bin (__noelle_bin)
    test -z "$bin"; and return 1
    set -l verdict (printf '%s' "$line" | $bin --shell-classify --shell fish --stdin 2>/dev/null)
    test "$verdict" = "natural"
end

function __noelle_accept_line
    if __noelle_classify
        set -l line (commandline)
        set -l bin (__noelle_bin)
        commandline -f repaint
        echo
        $bin --shell-intercept --shell fish -- $line
        commandline -f repaint
        commandline -r ""
    else
        commandline -f execute
    end
end

function noelle-ask
    set -l bin (__noelle_bin)
    $bin ask $argv
end

bind \r __noelle_accept_line
bind -M insert \r __noelle_accept_line
"""

BASH = r"""# noelle shell 集成 · bash
# 由 `noelle bash-init` 生成

__noelle_bin() { command -v noelle; }

__noelle_accept_line() {
    local line="$READLINE_LINE"
    [ -z "$line" ] && { READLINE_LINE=""; return; }
    local verdict
    verdict=$(printf '%s' "$line" | "$(__noelle_bin)" --shell-classify --shell bash --stdin 2>/dev/null)
    if [ "$verdict" = "natural" ]; then
        READLINE_LINE=""
        READLINE_POINT=0
        printf '\n'
        "$(__noelle_bin)" --shell-intercept --shell bash -- "$line"
    else
        eval "$line"
        READLINE_LINE=""
        READLINE_POINT=0
    fi
}

noelle-ask() { "$(__noelle_bin)" ask "$@"; }

bind -x '"\C-m": __noelle_accept_line'
"""

ZSH = r"""# noelle shell 集成 · zsh
# 由 `noelle zsh-init` 生成

__noelle_bin() { command -v noelle; }

__noelle_accept_line() {
    local line="$BUFFER"
    if [ -z "$line" ]; then
        zle accept-line
        return
    fi
    local verdict
    verdict=$(printf '%s' "$line" | "$(__noelle_bin)" --shell-classify --shell zsh --stdin 2>/dev/null)
    if [ "$verdict" = "natural" ]; then
        BUFFER=""
        CURSOR=0
        zle -I
        print
        "$(__noelle_bin)" --shell-intercept --shell zsh -- "$line"
    else
        zle accept-line
    fi
}

noelle-ask() { "$(__noelle_bin)" ask "$@" }

autoload -Uz add-zle-hook-widget 2>/dev/null || true
zle -N __noelle_accept_line 2>/dev/null || true
bindkey '^M' __noelle_accept_line
"""

TEMPLATES = {"fish": FISH, "bash": BASH, "zsh": ZSH}


def generate(shell: str) -> str:
    return TEMPLATES.get(shell, "")


def hooks_path(shell: str) -> str:
    if shell == "fish":
        return "~/.config/fish/conf.d/noelle.fish"
    if shell == "bash":
        return "~/.bashrc"
    if shell == "zsh":
        return "~/.zshrc"
    return ""
