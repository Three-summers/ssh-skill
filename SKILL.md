---
name: ssh-skill
description: Use when performing SSH or remote server operations (SSH, remote server, user@host, 服务器, 远程, 登录, 上传, 下载, 部署), including command execution, file transfers, server-to-server transfer, jump hosts, tunnels, and remote diagnostics. Do not use for localhost or local workspace commands.
allowed-tools: Bash Read Glob
metadata:
  version: "3.3.0"
---

# SSH Skill

高性能 SSH 操作技能。用本 skill 的 Python 脚本处理远程命令、文件传输、批量执行、跳板机、服务器间传输和 SSH 隧道，避免直接手写 `ssh`/`scp` 导致配置绕过、路径转换错误或连接无法复用。

## Core Rules

- 对支持的远程操作，使用 `scripts/` 下的工具；不要直接运行裸 `ssh`、`scp` 或 `rsync` 命令。
- 使用 `~/.ssh/config` 中的 Host alias 标识服务器；不知道别名时先 list/find。
- 不用于本地命令、当前工作区操作、`localhost` 或 `127.0.0.1`。
- 上传、下载、服务器间传输在 Git Bash/MSYS 环境必须加 `MSYS_NO_PATHCONV=1`，防止 `/tmp/file` 被转换为 Windows 路径。
- 对同一服务器的多个只读状态查询，优先合并成一次远程命令；状态变更、依赖步骤或需要独立错误处理时分开执行。
- 对 `gdb`、`pdb`、`lldb` 等需要持续 stdin/PTY 状态的调试器，使用 `ssh_interactive.py`。不要把普通 `ssh_execute.py` 命令发送到 interactive session；普通命令和调试 PTY 必须隔离。
- interactive PTY 当前要求 direct Paramiko-supported alias；该 interactive backend 暂不支持 ProxyJump/jump-host alias。
- 脚本通常输出 JSON；先检查 `success`、`exit_code`、`stdout`、`stderr`，再向用户汇总结果。

## Script Location

默认安装路径：

```bash
SSH_SKILL_DIR="${SSH_SKILL_DIR:-$HOME/.claude/skills/ssh-skill}"
SCRIPT_ROOT="$SSH_SKILL_DIR/scripts"
```

如果正在本 skill 仓库内工作，可把 `SSH_SKILL_DIR` 设为当前目录。示例命令使用 `python3`；如果目标环境没有 `python3`，改用可用的 Python 3 解释器。

不要写 `python "~/.claude/..."`，引号里的 `~` 不会被 shell 展开；使用 `$HOME`、`$SSH_SKILL_DIR` 或未加引号的 `~`。

## Shortcut Requests

当用户使用 `/ssh-skill ...`：

| 请求 | 处理方式 |
| --- | --- |
| `/ssh-skill list` | 运行 `ssh_config_manager_v3.py list-servers`，把 JSON 结果整理为 Markdown 表格：序号、别名、备注、标签、位置、认证、用户名，并显示总数。 |
| `/ssh-skill find <关键词>` | 运行 `ssh_config_manager_v3.py find "<关键词>"`，输出格式同 list。 |
| `/ssh-skill help` | 简要列出核心能力、快捷命令和常用自然语言示例；不要整段复述本文件。 |
| 其他参数 | 按自然语言意图选择下方工具；需要完整语法时读取 `references/commands.md`。 |

## Operation Map

常用命令模板：

```bash
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" list-servers
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" find "<keyword>"
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>"
python3 "$SCRIPT_ROOT/ssh_interactive.py" <alias> start <session> --command "<debugger-command>"
python3 "$SCRIPT_ROOT/ssh_interactive.py" <alias> send <session> "<debugger-input>" --wait-for "<prompt-regex>" --timeout 10
python3 "$SCRIPT_ROOT/ssh_interactive.py" <alias> read <session> --since <seq> --wait-for "<prompt-regex>" --timeout 5
python3 "$SCRIPT_ROOT/ssh_interactive.py" <alias> control <session> ctrl-c
python3 "$SCRIPT_ROOT/ssh_interactive.py" <alias> stop <session>
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_upload.py" <alias> "<local-path>" "<remote-path>"
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_download.py" <alias> "<remote-path>" "<local-path>"
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_server_transfer.py" <source-alias> "<source-path>" <dest-alias> "<dest-path>"
python3 "$SCRIPT_ROOT/ssh_cluster.py" "<command>" --parallel
python3 "$SCRIPT_ROOT/ssh_tunnel.py" start <alias> --remote-port <port>
```

Read `references/commands.md` for exact options, transfer modes, tunnel commands, daemon management, config examples, and troubleshooting.

## Server Selection

1. If the user gives an alias, use it directly.
2. If the user gives a description, environment, tag, IP, hostname, or Chinese server name, run find/list first.
3. If multiple hosts match, summarize the matches and ask the user to choose unless the requested scope is clearly batch/cluster.
4. If no host matches, explain that the alias is missing and offer the create/config workflow.

## Output Standards

- For remote command output, report the target alias, command purpose, exit status, and important stdout/stderr lines.
- For transfers, report source, destination, mode if known, success/failure, and any retry/resume action.
- For list/find, use compact Markdown tables.
- Do not expose passwords, private key material, full connection strings, or unrelated environment variables.

## Troubleshooting

- Alias missing: run `ssh_config_manager_v3.py find "<keyword>"` or `list-servers`.
- Timeout: add `--timeout <seconds>` for long commands; avoid infinite waits.
- Daemon problem: stop the daemon for that alias or retry with `--no-daemon`.
- Windows path issue: confirm `MSYS_NO_PATHCONV=1` is present for upload/download/server-transfer commands.
- Passphrase key on Windows: use the Windows SSH Agent helper documented in the repo README before retrying.

## Dependencies

- Python 3.8+
- `paramiko`
- Standard OpenSSH config at `~/.ssh/config`
