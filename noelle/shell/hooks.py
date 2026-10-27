"""生成 fish / bash / zsh 的集成脚本。

设计要点：
  · hook 只做一件事：回车时判断该行是不是自然语言，是就调 noelle
  · 判断走 noelle --shell-classify（独立进程，<60ms），避免污染 shell 状态
  · 每个 shell 的函数前缀用 __noelle_，不会与其它工具冲突
"""

from __future__ import annotations

FISH = r"""# noelle shell 集成 · 移植自 miyu 终端 UI v0.7.0
# 由 `noelle fish-init` 生成，勿手改；重新生成请再次运行该命令

# PATH 上没有 noelle 时去常见安装目录里找(Homebrew、~/.local、cargo)。
if not type -q noelle
    for __noelle_dir in /opt/homebrew/bin /usr/local/bin /home/linuxbrew/.linuxbrew/bin ~/.local/bin ~/.cargo/bin
        if test -x $__noelle_dir/noelle
            set -g __noelle_bin $__noelle_dir/noelle
            function noelle
                $__noelle_bin $argv
            end
            break
        end
    end
    set -e __noelle_dir
end

complete -c noelle -n __fish_use_subcommand -f -a ask -d '向助手发送一条消息'
complete -c noelle -n __fish_use_subcommand -f -a init -d '创建默认配置和状态文件；Shell 集成请使用对应的 <shell>-init 命令'
complete -c noelle -n __fish_use_subcommand -f -a paths -d '显示应用配置、数据和缓存路径'
complete -c noelle -n __fish_use_subcommand -f -a config -d '打开或管理配置'
complete -c noelle -n __fish_use_subcommand -f -a reload -d '在运行中的 Miyu daemon 内重新加载配置'
complete -c noelle -n __fish_use_subcommand -f -a models -d '列出或切换模型'
complete -c noelle -n __fish_use_subcommand -f -a fish-init -d '集成到 fish，集成后可在终端直接使用自然语言交流。'
complete -c noelle -n __fish_use_subcommand -f -a bash-init -d '集成到 bash，集成后可在终端直接使用自然语言交流。'
complete -c noelle -n __fish_use_subcommand -f -a zsh-init -d '集成到 zsh，集成后可在终端直接使用自然语言交流。'
complete -c noelle -n __fish_use_subcommand -f -a remove-shell-hook -d '安全删除已安装的 Miyu shell hook'
complete -c noelle -n __fish_use_subcommand -f -a history -d '显示会话历史'
complete -c noelle -n __fish_use_subcommand -f -a kb -d '管理本地知识库'
complete -c noelle -n __fish_use_subcommand -f -a update-default-kb -d '更新 Miyu 默认知识库'
complete -c noelle -n __fish_use_subcommand -f -a memory -d '查看或编辑助手记忆'
complete -c noelle -n __fish_use_subcommand -f -a skills -d '管理助手 skills'
complete -c noelle -n __fish_use_subcommand -f -a reset -d '清空当前会话历史'

function __noelle_paste
    set -l output (noelle --clipboard-paste 2>/dev/null)
    if test $status -eq 0; and test -n "$output"
        if not set -q __noelle_image_counter
            set -g __noelle_image_counter 0
        end
        set __noelle_image_counter (math $__noelle_image_counter + 1)
        # 视频的占位符标签是 Video,只替 Image 的话第二个视频起序号永远是 1,
        # 解析端会把它们都当成第一个附件(08-28)。
        set output (string replace -r '^\[(Image|Video) 1' "[\$1 $__noelle_image_counter" -- $output)
        commandline -i -- $output
        commandline -f repaint
    else
        fish_clipboard_paste
    end
end

bind \cv __noelle_paste

function __noelle_insert_newline
    commandline -f expand-abbr
    commandline -i \n
end

bind ctrl-j __noelle_insert_newline
bind \cj __noelle_insert_newline
bind -M insert ctrl-j __noelle_insert_newline
bind -M insert \cj __noelle_insert_newline

function __noelle_wrap_fish_prompt
    functions -q __noelle_original_fish_prompt; and return
    functions -q fish_prompt; or fish_prompt >/dev/null 2>/dev/null
    functions -q fish_prompt; or return

    functions -c fish_prompt __noelle_original_fish_prompt
    function fish_prompt
        if set -q __noelle_pending_buffer
            printf '\e[?25l'
        end
        __noelle_original_fish_prompt
    end
end

function __noelle_replay_buffer
    set -l buffer $argv[1]
    set -l lines (string split \n -- "$buffer")
    if test (count $lines) -gt 0
        set -l prompt (fish_prompt | string collect -N)
        set -l prompt_lines (string split \n -- "$prompt")
        set -l prompt_col (math (string length --visible -- "$prompt_lines[-1]") + 1)
        printf '\e[?25l'
        printf '\e[1A\e[%sG' $prompt_col
        if not set -q fish_color_error; or not set_color $fish_color_error 2>/dev/null
            set_color red
        end
        printf '%s\n' "$lines[1]"
        for line in $lines[2..-1]
            printf '  %s\n' "$line"
        end
        set_color normal
    end
end

function __noelle_restore_cursor
    printf '\e[?25h'
    set -e __noelle_cursor_hidden
end

function __noelle_on_prompt --on-event fish_prompt
    set -q __noelle_pending_buffer; or return

    set -l buffer $__noelle_pending_buffer
    set -e __noelle_pending_buffer
    set -e __noelle_image_counter

    trap __noelle_restore_cursor INT TERM EXIT
    __noelle_replay_buffer "$buffer"
    printf '\n'
    printf '%s' "$buffer" | noelle --shell-intercept --shell fish --stdin
    set -l noelle_status $status
    trap - INT TERM EXIT
    __noelle_restore_cursor
    return $noelle_status
end

function __noelle_execute_or_continue
    commandline --is-valid
    set -l valid_status $status
    if test $valid_status -eq 2
        commandline -i \n
        commandline -f repaint
    else
        set -e __noelle_image_counter
        commandline -f execute
    end
end

function __noelle_buffer_is_multiline
    test (string split \n -- "$argv[1]" | count) -gt 1
end

function __noelle_first_command
    set -l tokens (commandline --input="$argv[1]" --tokens-expanded 2>/dev/null)
    while test (count $tokens) -gt 0
        set -l token $tokens[1]
        if string match -qr '^[A-Za-z_][A-Za-z0-9_]*=' -- "$token"
            set -e tokens[1]
            continue
        end
        printf '%s' "$token"
        return 0
    end
    return 1
end

# 只看首词长什么样,所以取未展开的原文:--tokens-expanded 会真的跑命令替换,
# 放在每次回车上按不得。老版本 fish 不认 --tokens-raw,拿不到就当判不出来。
function __noelle_first_token_raw
    set -l tokens (commandline --input="$argv[1]" --tokens-raw 2>/dev/null)
    while test (count $tokens) -gt 0
        set -l token $tokens[1]
        if string match -qr '^[A-Za-z_][A-Za-z0-9_]*=' -- "$token"
            set -e tokens[1]
            continue
        end
        printf '%s' "$token"
        return 0
    end
    return 1
end

# 首词是不是一个「展开后还是它自己」的普通词。$ ( ) ~ { } % 引号反斜杠这些会把
# 首词换成别的东西,判不出来就交回 fish 自己展开,这里不猜;; & | < > # ^ ! 和空白
# 同理,出现了说明这行有 fish 语法结构。
# 通配符 * ? [ ] 故意不在名单里:命令位上出现通配符,本来就说明这不是个命令名。
# 不能用 \w 白名单——fish 的正则里 \w 只认 ASCII,中文会被当成元字符。
function __noelle_head_is_plain_word
    test -n "$argv[1]"; or return 1
    string match -qr '[\x27"$()~{}%;&|<>#^!\x5c\s]' -- "$argv[1]"; and return 1
    return 0
end

function __noelle_hand_to_ai
    set -e __noelle_image_counter
    __noelle_wrap_fish_prompt
    set -g __noelle_cursor_hidden 1
    history append -- "$argv[1]"
    set -g __noelle_pending_buffer "$argv[1]"
    commandline -b -- ""
    printf '\e[?25l'
    commandline -f execute
end

function __noelle_accept_line
    status is-interactive; or return

    commandline -f expand-abbr
    set -l buffer (commandline -b | string collect)
    set -l trimmed (string trim -- "$buffer")
    if test -z "$trimmed"
        __noelle_execute_or_continue
        return
    end

    if not __noelle_buffer_is_multiline "$buffer"
        # 单行本来靠 fish_command_not_found 兜底,但 fish 是先展开再找命令:
        # 自然语言里带个没匹配上的通配符(「输出这段命令 …/core.*.zst」),
        # fish 在展开阶段就报「未找到通配符的匹配项」,命令根本没开始找,
        # 兜底函数也就永远不触发。首词是普通词又不是任何命令时提前接管。
        set -l head (__noelle_first_token_raw "$buffer")
        if not __noelle_head_is_plain_word "$head"; or type -q -- "$head"
            __noelle_execute_or_continue
            return
        end
        __noelle_hand_to_ai "$buffer"
        return
    end

    set -l first_command (__noelle_first_command "$buffer")
    if test -n "$first_command"; and not contains -- "$first_command" time test date which type command history; and type -q -- "$first_command"
        __noelle_execute_or_continue
        return
    end

    printf '%s' "$buffer" | noelle --shell-classify --shell fish --stdin 2>/dev/null
    set -l classify_status $status
    if test $classify_status -eq 0
        __noelle_execute_or_continue
        return
    else if test $classify_status -ne 1
        __noelle_execute_or_continue
        return
    end

    __noelle_hand_to_ai "$buffer"
end

bind enter __noelle_accept_line
bind \r __noelle_accept_line
bind -M insert enter __noelle_accept_line
bind -M insert \r __noelle_accept_line

function fish_command_not_found
    status is-interactive; or return 127

    set -e __noelle_image_counter

    set -l current_line (status current-commandline 2>/dev/null | string collect)
    if test -n "$current_line"; and not string match -qr '[\n\r]' -- "$current_line"
        set -l top_command (__noelle_first_command "$current_line")
        if test -z "$top_command"; or not type -q -- "$top_command"
            printf '\n'
            printf '%s' "$current_line" | noelle --shell-intercept --shell fish --stdin 2>/dev/null
            return 127
        end
    end

    set -l command $argv
    if test (count $command) -eq 0
        return 127
    end

    set -l text (string join ' ' -- $command)
    string match -qr '[\n\r]' -- $text; and return 127

    noelle --shell-intercept --shell fish -- $command 2>/dev/null
    return 127
end
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
