#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SSH命令执行CLI工具 v3.0

支持通过别名执行SSH命令，从标准 SSH config 和注释元数据中加载配置。
自动检测守护进程：有则走长连接，无则走直连。

用法：
    python ssh_execute.py <alias> <command> [--timeout TIMEOUT]
    python ssh_execute.py <alias> <command> --no-daemon
    python ssh_execute.py <alias> <command> --stream
    python ssh_execute.py <alias> <command> --sudo
    python ssh_execute.py <alias> <command> --session <name>
    python ssh_execute.py <alias> --session-status <name>
    python ssh_execute.py <alias> --session-logs <name> [--follow] [--lines N]
    python ssh_execute.py <alias> --session-stop <name>

示例：
    python ssh_execute.py prod-web-01 "whoami && hostname"
    python ssh_execute.py DEV-002 "df -h" --timeout 60
"""

import sys
import os
import json
import socket
import struct
import argparse
import subprocess
import time
import shlex
import re

# 添加lib到路径
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_script_dir, 'lib'))


SESSION_ROOT = "$HOME/.ssh-skill/sessions"


def _send_message(sock, data):
    """发送带长度前缀的 JSON 消息"""
    payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
    header = struct.pack('!I', len(payload))
    sock.sendall(header + payload)


def _recv_message(sock, timeout=None):
    """接收带长度前缀的 JSON 消息"""
    if timeout:
        sock.settimeout(timeout)

    header = b''
    while len(header) < 4:
        chunk = sock.recv(4 - len(header))
        if not chunk:
            raise ConnectionError("连接已关闭")
        header += chunk

    length = struct.unpack('!I', header)[0]
    if length > 10 * 1024 * 1024:
        raise ValueError(f"消息过大: {length} bytes")

    body = b''
    while len(body) < length:
        chunk = sock.recv(min(65536, length - len(body)))
        if not chunk:
            raise ConnectionError("连接已关闭")
        body += chunk

    return json.loads(body.decode('utf-8'))


def try_daemon_execute(alias, command, timeout):
    """尝试通过守护进程执行命令，返回 None 表示守护进程不可用"""
    from ssh_daemon import read_daemon_info

    info = read_daemon_info(alias)
    if not info:
        return None

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout + 5)
        sock.connect(('127.0.0.1', info['port']))
        _send_message(sock, {
            'action': 'execute',
            'command': command,
            'timeout': timeout
        })
        result = _recv_message(sock, timeout=timeout + 5)
        sock.close()
        return result
    except Exception:
        return None


def start_daemon_background(alias):
    """后台启动守护进程"""
    daemon_script = os.path.join(_script_dir, 'ssh_daemon.py')
    try:
        if os.name == 'nt':
            # Windows: 使用 CREATE_NO_WINDOW
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                [sys.executable, daemon_script, 'start', alias],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW
            )
        else:
            subprocess.Popen(
                [sys.executable, daemon_script, 'start', alias],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        # 等待守护进程启动
        import time
        for _ in range(10):
            time.sleep(0.3)
            from ssh_daemon import read_daemon_info
            if read_daemon_info(alias):
                return True
        return False
    except Exception:
        return False


def _sudo_wrap(command):
    """Wrap a user command for non-interactive sudo without embedding passwords."""
    return f"sudo -S -p '' sh -lc {shlex.quote(command)}"


def _send_stdin_password(stdin, password):
    """Send a password over the SSH stdin channel without logging it."""
    stdin.write(password + "\n")
    stdin.flush()
    channel = getattr(stdin, "channel", None)
    if channel is not None and hasattr(channel, "shutdown_write"):
        try:
            channel.shutdown_write()
        except Exception:
            pass


def _execute_paramiko_command(client, command, timeout=None, sudo_password=None):
    """Execute through Paramiko, optionally feeding sudo password on stdin."""
    try:
        ssh_client = client._get_connection()
        stdin, stdout, stderr = ssh_client.exec_command(
            command, timeout=timeout or client.timeout
        )
        if sudo_password is not None:
            _send_stdin_password(stdin, sudo_password)

        stdout_text = stdout.read().decode('utf-8', errors='replace')
        stderr_text = stderr.read().decode('utf-8', errors='replace')
        exit_code = stdout.channel.recv_exit_status()

        return {
            'success': exit_code == 0,
            'exit_code': exit_code,
            'stdout': stdout_text,
            'stderr': stderr_text,
        }
    except Exception as e:
        return {
            'success': False,
            'exit_code': -1,
            'stdout': '',
            'stderr': f'Execution error: {e}',
        }


def _session_safe_name(session):
    """Validate a session name before using it in remote shell paths."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", session or ""):
        raise ValueError(
            "session name must be 1-64 chars: letters, numbers, dot, dash, underscore"
        )
    return session


def _session_paths(session):
    safe = _session_safe_name(session)
    remote_dir = f"{SESSION_ROOT}/{safe}"
    return {
        "session": safe,
        "remote_dir": remote_dir,
        "pid": f"{remote_dir}/pid",
        "log": f"{remote_dir}/session.log",
        "exit_code": f"{remote_dir}/exit_code",
    }


def _remote_double_quote(value):
    """Quote trusted remote shell values while preserving $HOME expansion."""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _parse_key_values(text):
    values = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _session_start_remote_command(session, command):
    paths = _session_paths(session)
    runner = (
        "printf '[session] started at %s\\n' \"$(date -Is 2>/dev/null || date)\"; "
        f"{command}; "
        "code=$?; "
        "printf '[session] exited with code %s at %s\\n' "
        "\"$code\" \"$(date -Is 2>/dev/null || date)\"; "
        f"printf '%s\\n' \"$code\" > {_remote_double_quote(paths['exit_code'])}; "
        "exit \"$code\""
    )
    return (
        f"session_dir={_remote_double_quote(paths['remote_dir'])}; "
        'pid_file="$session_dir/pid"; '
        'log_file="$session_dir/session.log"; '
        'exit_code_file="$session_dir/exit_code"; '
        'mkdir -p "$session_dir" && '
        'chmod 755 "$session_dir" && '
        ': > "$log_file" && '
        'rm -f "$exit_code_file" && '
        "("
        f"nohup sh -lc {shlex.quote(runner)} "
        '>> "$log_file" 2>&1 < /dev/null & '
        "pid=$!; "
        'printf "%s\\n" "$pid" > "$pid_file"; '
        "printf 'pid=%s\\n' \"$pid\"; "
        'printf "remote_dir=%s\\n" "$session_dir"; '
        'printf "log=%s\\n" "$log_file"'
        ")"
    )


def _session_status_remote_command(session):
    paths = _session_paths(session)
    return (
        f"session_dir={_remote_double_quote(paths['remote_dir'])}; "
        'pid_file="$session_dir/pid"; '
        'log_file="$session_dir/session.log"; '
        'exit_code_file="$session_dir/exit_code"; '
        'if [ ! -f "$pid_file" ]; then '
        f"printf 'session=%s\\nstate=missing\\n' {shlex.quote(paths['session'])}; "
        "exit 1; fi; "
        'pid=$(cat "$pid_file"); '
        "exit_code=''; "
        'if [ -f "$exit_code_file" ]; then '
        'exit_code=$(cat "$exit_code_file"); fi; '
        'if [ -n "$exit_code" ]; then state=exited; '
        'elif kill -0 "$pid" 2>/dev/null; then state=running; '
        "else state=unknown; fi; "
        f"printf 'session=%s\\n' {shlex.quote(paths['session'])}; "
        "printf 'state=%s\\n' \"$state\"; "
        "printf 'pid=%s\\n' \"$pid\"; "
        "printf 'exit_code=%s\\n' \"$exit_code\"; "
        'printf "log=%s\\n" "$log_file"'
    )


def _session_stop_remote_command(session):
    paths = _session_paths(session)
    return (
        f"session_dir={_remote_double_quote(paths['remote_dir'])}; "
        'pid_file="$session_dir/pid"; '
        'if [ ! -f "$pid_file" ]; then '
        f"printf 'session=%s\\nstate=missing\\n' {shlex.quote(paths['session'])}; "
        "exit 1; fi; "
        'pid=$(cat "$pid_file"); '
        "if kill -0 \"$pid\" 2>/dev/null; then "
        "kill -TERM \"$pid\" 2>/dev/null || true; sleep 1; "
        "if kill -0 \"$pid\" 2>/dev/null; then state=stopping; "
        "else state=stopped; fi; "
        "else state=not_running; fi; "
        f"printf 'session=%s\\n' {shlex.quote(paths['session'])}; "
        "printf 'state=%s\\n' \"$state\"; "
        "printf 'pid=%s\\n' \"$pid\""
    )


def session_start(alias, command, session, timeout, use_sudo=False):
    result = direct_execute(
        alias,
        _session_start_remote_command(session, command),
        timeout,
        use_sudo=use_sudo,
    )
    if not result.get("success"):
        return result
    details = _parse_key_values(result.get("stdout", ""))
    return {
        "success": True,
        "session": _session_safe_name(session),
        "state": "started",
        **details,
    }


def session_status(alias, session, timeout, use_sudo=False):
    result = direct_execute(
        alias,
        _session_status_remote_command(session),
        timeout,
        use_sudo=use_sudo,
    )
    details = _parse_key_values(result.get("stdout", ""))
    return {
        "success": result.get("success", False),
        "session": _session_safe_name(session),
        **details,
        "stderr": result.get("stderr", ""),
    }


def session_stop(alias, session, timeout, use_sudo=False):
    result = direct_execute(
        alias,
        _session_stop_remote_command(session),
        timeout,
        use_sudo=use_sudo,
    )
    details = _parse_key_values(result.get("stdout", ""))
    return {
        "success": result.get("success", False),
        "session": _session_safe_name(session),
        **details,
        "stderr": result.get("stderr", ""),
    }


def session_logs(
    alias,
    session,
    lines=100,
    follow=False,
    timeout=None,
    use_sudo=False,
):
    paths = _session_paths(session)
    command = f"tail -n {int(lines)} {'-f ' if follow else ''}{paths['log']}"
    if follow:
        return stream_execute(alias, command, timeout, use_sudo=use_sudo)
    result = direct_execute(alias, command, timeout or 30, use_sudo=use_sudo)
    if result.get("stdout"):
        sys.stdout.write(result["stdout"])
        sys.stdout.flush()
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
        sys.stderr.flush()
    return 0 if result.get("success") else 1


def direct_execute(alias, command, timeout, use_sudo=False):
    """直连执行命令（智能选择客户端类型，支持降级到原生 SSH）"""
    from config_v3 import SSHConfigLoaderV3
    from native_ssh_fallback import should_use_native_ssh, execute_native_ssh, check_ssh_agent

    loader = SSHConfigLoaderV3()

    # 加载 SSH 配置
    ssh_config = loader.load_ssh_config(alias)
    metadata = {}
    try:
        metadata = loader.load_metadata(alias)
    except:
        pass

    params = loader.get_connection_params(alias)

    if use_sudo:
        sudo_password = params.get('password')
        if not sudo_password:
            return {
                'success': False,
                'exit_code': -1,
                'stdout': '',
                'stderr': 'sudo password is not available in SSH config metadata',
            }
        client = loader.from_alias(alias)
        client.timeout = timeout
        return _execute_paramiko_command(
            client,
            _sudo_wrap(command),
            timeout=timeout,
            sudo_password=sudo_password,
        )

    # 检测是否应该降级到原生 SSH
    should_fallback, reason = should_use_native_ssh(ssh_config, metadata)

    if should_fallback:
        # 检查 ssh-agent 状态（如果涉及密钥认证）
        agent_available, agent_msg = check_ssh_agent()

        # 如果原因包含 passphrase 且 ssh-agent 不可用，给出提示但仍然尝试
        if 'passphrase' in reason.lower() and not agent_available:
            import sys
            print(f"\n⚠️  警告：检测到需要 passphrase 的密钥，但 ssh-agent 未配置", file=sys.stderr)
            print(f"ssh-agent 状态: {agent_msg}", file=sys.stderr)
            print(f"\n建议配置 ssh-agent 以避免每次输入密码：", file=sys.stderr)
            print(f"1. 启动 ssh-agent: eval $(ssh-agent)", file=sys.stderr)
            print(f"2. 添加密钥: ssh-add ~/.ssh/your_key", file=sys.stderr)
            print(f"\n现在将使用原生 SSH（需要交互式输入 passphrase）...\n", file=sys.stderr)

        # 使用原生 SSH 执行
        result = execute_native_ssh(alias, command, timeout)
        result['fallback_reason'] = reason
        return result

    # 使用智能选择：密钥认证 → NativeSSHClient，密码认证 → ParamikoClient
    client = loader.from_alias(alias)

    # 设置超时
    client.timeout = timeout

    result = client.execute(command)
    return {
        'success': result.success,
        'exit_code': result.exit_code,
        'stdout': result.stdout,
        'stderr': result.stderr
    }


def _write_stream(stream, data):
    """Write SSH byte output to a local text stream immediately."""
    if not data:
        return
    stream.write(data.decode('utf-8', errors='replace'))
    stream.flush()


def _stream_paramiko_client(
    client,
    command,
    timeout=None,
    poll_interval=0.05,
    sudo_password=None
):
    """Run a command through Paramiko and stream stdout/stderr as it arrives."""
    channel = None
    try:
        ssh_client = client._get_connection()
        stdin, stdout, stderr = ssh_client.exec_command(command)
        if sudo_password is not None:
            _send_stdin_password(stdin, sudo_password)
        channel = stdout.channel
        start_time = time.monotonic()

        while True:
            made_progress = False

            while channel.recv_ready():
                data = channel.recv(65536)
                if not data:
                    break
                _write_stream(sys.stdout, data)
                made_progress = True

            while channel.recv_stderr_ready():
                data = channel.recv_stderr(65536)
                if not data:
                    break
                _write_stream(sys.stderr, data)
                made_progress = True

            if channel.exit_status_ready():
                while channel.recv_ready():
                    _write_stream(sys.stdout, channel.recv(65536))
                while channel.recv_stderr_ready():
                    _write_stream(sys.stderr, channel.recv_stderr(65536))
                return channel.recv_exit_status()

            if timeout is not None and time.monotonic() - start_time >= timeout:
                channel.close()
                print(f"Command timeout after {timeout} seconds", file=sys.stderr)
                return -1

            if not made_progress:
                time.sleep(poll_interval)

    except KeyboardInterrupt:
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        print("\nInterrupted; remote SSH channel closed", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Execution error: {e}", file=sys.stderr)
        return -1


def stream_execute(alias, command, timeout=None, use_sudo=False):
    """
    Execute a remote command with live stdout/stderr forwarding.

    Password-auth hosts use Paramiko so comment metadata passwords still work.
    Key-auth hosts use native ssh so OpenSSH config features remain available.
    """
    from config_v3 import SSHConfigLoaderV3
    from native_ssh_fallback import execute_native_ssh_stream

    loader = SSHConfigLoaderV3()
    params = loader.get_connection_params(alias)

    if use_sudo:
        sudo_password = params.get('password')
        if not sudo_password:
            print(
                'sudo password is not available in SSH config metadata',
                file=sys.stderr,
            )
            return -1
        client = loader.from_alias(alias)
        return _stream_paramiko_client(
            client,
            _sudo_wrap(command),
            timeout,
            sudo_password=sudo_password,
        )

    if not params.get('password'):
        return execute_native_ssh_stream(alias, command, timeout)

    client = loader.from_alias(alias)
    return _stream_paramiko_client(client, command, timeout)


def main():
    parser = argparse.ArgumentParser(description='SSH command execution tool v3.0')
    parser.add_argument('alias', help='SSH host alias from ~/.ssh/config')
    parser.add_argument('command', nargs='?', help='Command to execute')
    parser.add_argument('--timeout', type=int, help='Timeout in seconds')
    parser.add_argument('--no-daemon', action='store_true',
                        help='Disable daemon mode, use direct SSH connection')
    parser.add_argument('--stream', action='store_true',
                        help='Stream stdout/stderr live; disables JSON output')
    parser.add_argument('--sudo', action='store_true',
                        help='Run command through sudo using password metadata')
    parser.add_argument('--session',
                        help='Start command as a named remote background session')
    parser.add_argument('--session-status',
                        help='Show status for a named remote session')
    parser.add_argument('--session-logs',
                        help='Print logs for a named remote session')
    parser.add_argument('--session-stop',
                        help='Stop a named remote session')
    parser.add_argument('--follow', action='store_true',
                        help='Follow session logs with tail -f')
    parser.add_argument('--lines', type=int, default=100,
                        help='Number of session log lines to print')

    args = parser.parse_args()
    timeout = args.timeout if args.stream else (args.timeout or 30)

    try:
        session_actions = [
            bool(args.session),
            bool(args.session_status),
            bool(args.session_logs),
            bool(args.session_stop),
        ]
        if sum(session_actions) > 1:
            parser.error('use only one session action at a time')

        if args.session:
            if not args.command:
                parser.error('--session requires a command')
            result = session_start(
                args.alias, args.command, args.session, timeout or 30, args.sudo
            )
            print(json.dumps(result, ensure_ascii=True, indent=2))
            sys.exit(0 if result.get('success') else 1)

        if args.session_status:
            result = session_status(
                args.alias, args.session_status, timeout or 30, args.sudo
            )
            print(json.dumps(result, ensure_ascii=True, indent=2))
            sys.exit(0 if result.get('success') else 1)

        if args.session_logs:
            sys.exit(session_logs(
                args.alias,
                args.session_logs,
                lines=args.lines,
                follow=args.follow,
                timeout=args.timeout,
                use_sudo=args.sudo,
            ))

        if args.session_stop:
            result = session_stop(
                args.alias, args.session_stop, timeout or 30, args.sudo
            )
            print(json.dumps(result, ensure_ascii=True, indent=2))
            sys.exit(0 if result.get('success') else 1)

        if not args.command:
            parser.error('command is required unless a session action is used')

        if args.stream:
            sys.exit(stream_execute(
                args.alias, args.command, timeout, use_sudo=args.sudo
            ))

        result = None

        # 智能判断是否使用守护进程
        # 守护进程只对密码认证有意义（Paramiko），密钥认证使用原生 SSH 不需要守护进程
        from config_v3 import SSHConfigLoaderV3
        loader = SSHConfigLoaderV3()
        params = loader.get_connection_params(args.alias)

        has_key = params.get('key_file') is not None
        has_password = params.get('password') is not None
        use_daemon = (
            has_password and not args.no_daemon and not args.sudo
        )  # sudo needs stdin, so bypass daemon mode

        if use_daemon:
            # 密码认证：尝试通过守护进程执行
            result = try_daemon_execute(args.alias, args.command, timeout)

            # 守护进程不可用，尝试后台启动
            if result is None:
                if start_daemon_background(args.alias):
                    result = try_daemon_execute(args.alias, args.command, timeout)

        # 仍然没有结果，使用直连（密钥认证会使用 NativeSSHClient）
        if result is None:
            result = direct_execute(
                args.alias, args.command, timeout, use_sudo=args.sudo
            )

        print(json.dumps(result, ensure_ascii=True, indent=2))
        sys.exit(0 if result.get('success') else 1)

    except FileNotFoundError as e:
        print(json.dumps({
            'success': False,
            'exit_code': -1,
            'stdout': '',
            'stderr': f'Config not found: {e}'
        }, ensure_ascii=True, indent=2), file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(json.dumps({
            'success': False,
            'exit_code': -1,
            'stdout': '',
            'stderr': f'Invalid alias: {e}'
        }, ensure_ascii=True, indent=2), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(json.dumps({
            'success': False,
            'exit_code': -1,
            'stdout': '',
            'stderr': f'Execution error: {e}'
        }, ensure_ascii=True, indent=2), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
