# SSH Skill Command Reference

Load this file only when exact command syntax, transfer modes, tunnel behavior, config format, or troubleshooting details are needed.

## Shell Setup

Use a script root variable instead of quoted `~` paths:

```bash
SSH_SKILL_DIR="${SSH_SKILL_DIR:-$HOME/.claude/skills/ssh-skill}"
SCRIPT_ROOT="$SSH_SKILL_DIR/scripts"
```

Examples use `python3`. If unavailable, use the environment's Python 3 command.

## Config And Discovery

```bash
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" list-servers
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" list-servers --environment production
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" find "<keyword>"
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" create --alias <alias> --host <ip-or-hostname> --user <user> --key <key-file> --environment <env>
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" update <alias> --description "new description"
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" update <alias> --environment production --location "new location"
python3 "$SCRIPT_ROOT/ssh_config_manager_v3.py" delete <alias>
```

Render list/find results as a Markdown table with these columns when available: index, alias, description, tags, location, auth, user.

## Execute Remote Commands

```bash
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>"
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>" --timeout 300
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>" --no-daemon
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>" --stream
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>" --stream --timeout 300
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>" --sudo
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "<command>" --sudo --stream
```

`ssh_execute.py` automatically uses the daemon long connection when available and starts it when needed.
Normal mode returns a JSON result after the remote command exits.

Use `--stream` for blocking commands, long-running programs, and live log watching:

```bash
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "cd /opt/app && ./run.sh" --stream
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "tail -f /var/log/app.log" --stream
```

Stream mode writes remote stdout and stderr directly to local stdout and stderr, so it does not emit JSON. When `--timeout` is omitted, it keeps running until the remote command exits or the local process is interrupted. It still executes one command session and does not preserve remote shell state across calls; put `cd`, `export`, and other setup in the same command.

Use `--sudo` when the remote command needs sudo privileges and the host stores a `password` metadata comment in `~/.ssh/config`. The implementation wraps the command with `sudo -S -p '' sh -lc ...` and sends the password over SSH stdin, not in the remote command line. `--sudo` bypasses daemon mode because sudo password input must be sent through the current SSH channel.

```bash
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "whoami && id -u" --sudo
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "apt update" --sudo --stream
```

For related read-only checks, combine commands:

```bash
python3 "$SCRIPT_ROOT/ssh_execute.py" <alias> "hostname && uptime && df -h && free -m"
```

Keep state-changing commands separate when later steps depend on earlier results.

## Upload And Download

Git Bash/MSYS requires `MSYS_NO_PATHCONV=1`.

```bash
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_upload.py" <alias> "<local-path>" "<remote-path>"
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_upload.py" <alias> "<local-dir>" "<remote-dir>" --recursive
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_upload.py" <alias> "<local-path>" "<remote-path>" --resume

MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_download.py" <alias> "<remote-path>" "<local-path>"
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_download.py" <alias> "<remote-dir>" "<local-dir>" --recursive
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_download.py" <alias> "<remote-path>" "<local-path>" --resume
```

Use `--no-progress` only when progress output would make logs noisy or break downstream parsing.

## Server-To-Server Transfer

```bash
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_server_transfer.py" <source-alias> "<source-path>" <dest-alias> "<dest-path>"
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_server_transfer.py" <source-alias> "<source-path>" <dest-alias> "<dest-path>" --mode direct
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_server_transfer.py" <source-alias> "<source-path>" <dest-alias> "<dest-path>" --mode stream
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_server_transfer.py" <source-alias> "<source-path>" <dest-alias> "<dest-path>" --mode hybrid
MSYS_NO_PATHCONV=1 python3 "$SCRIPT_ROOT/ssh_server_transfer.py" <source-alias> "<source-path>" <dest-alias> "<dest-path>" --use-rsync
```

| Mode | Use when | Data flow |
| --- | --- | --- |
| `auto` | Default; let the tool choose | Adaptive |
| `direct` | Large files and source can reach destination | Source -> destination |
| `stream` | Servers cannot reach each other or file is small | Source -> local stream -> destination |
| `hybrid` | Environment is uncertain | Direct first, then stream fallback |

Options: `--mode <auto|direct|stream|hybrid>`, `--use-rsync`, `--no-progress`, `--size-threshold <MB>`, `--timeout <seconds>`.

## Batch And Cluster

```bash
python3 "$SCRIPT_ROOT/ssh_cluster.py" "<command>" --parallel
python3 "$SCRIPT_ROOT/ssh_cluster.py" "<command>" --hosts "DEV-002,DEV-003" --parallel
python3 "$SCRIPT_ROOT/ssh_cluster.py" "<command>" --environment production --parallel
python3 "$SCRIPT_ROOT/ssh_cluster.py" "<command>" --tags "web,nginx" --parallel
python3 "$SCRIPT_ROOT/ssh_cluster.py" "<command>" --parallel --max-workers 10 --timeout 300
python3 "$SCRIPT_ROOT/ssh_cluster.py" "<health-command>" --parallel --health-check
```

Use cluster operations only when the user clearly requests a batch scope or the selected hosts are unambiguous.

## SSH Tunnels

```bash
python3 "$SCRIPT_ROOT/ssh_tunnel.py" start <alias> --remote-port <port>
python3 "$SCRIPT_ROOT/ssh_tunnel.py" start <alias> --local-port <local-port> --remote-port <remote-port>
python3 "$SCRIPT_ROOT/ssh_tunnel.py" start <alias> --remote-host <remote-host> --remote-port <remote-port>
python3 "$SCRIPT_ROOT/ssh_tunnel.py" list
python3 "$SCRIPT_ROOT/ssh_tunnel.py" status <tunnel-id>
python3 "$SCRIPT_ROOT/ssh_tunnel.py" stop <tunnel-id>
python3 "$SCRIPT_ROOT/ssh_tunnel.py" stop-all <alias>
```

Use tunnels for remote databases, internal web services, internal APIs, and services reachable only from the remote network. Tunnels listen on localhost and run as daemons with heartbeat and idle-timeout behavior.

## Daemon Management

Manual daemon control is usually unnecessary because `ssh_execute.py` starts and reuses the daemon automatically.

```bash
python3 "$SCRIPT_ROOT/ssh_daemon.py" start <alias>
python3 "$SCRIPT_ROOT/ssh_daemon.py" status <alias>
python3 "$SCRIPT_ROOT/ssh_daemon.py" stop <alias>
```

Use `ssh_execute.py --no-daemon` to bypass daemon mode for one command.

## Key Management

```bash
python3 "$SCRIPT_ROOT/ssh_key_manager.py" add --host <alias> --key ~/.ssh/id_ed25519.pub
python3 "$SCRIPT_ROOT/ssh_key_manager.py" add --hosts "host-a,host-b" --key ~/.ssh/id_ed25519.pub
python3 "$SCRIPT_ROOT/ssh_key_manager.py" add --all --key ~/.ssh/id_ed25519.pub
python3 "$SCRIPT_ROOT/ssh_key_manager.py" verify --host <alias> --key ~/.ssh/id_ed25519.pub
python3 "$SCRIPT_ROOT/ssh_key_manager.py" rollback --host <alias>
```

## SSH Config Format

Servers live in `~/.ssh/config` as standard OpenSSH Host blocks with comment metadata:

```ssh-config
# ===== prod-web-01 =====
# description: production web server
# environment: production
# tags: web,nginx,production
# location: beijing
# created_at: 2026-03-01 12:00:00
# updated_at: 2026-03-01 12:00:00
Host prod-web-01
    HostName 192.168.1.100
    User root
    IdentityFile ~/.ssh/id_rsa
    Port 22
```

Jump hosts use standard `ProxyJump`:

```ssh-config
Host bastion
    HostName bastion.example.com
    User jumpuser
    IdentityFile ~/.ssh/jump_key

Host internal-server
    HostName 10.0.1.100
    User appuser
    IdentityFile ~/.ssh/id_rsa
    ProxyJump bastion
```

Prefer key authentication. If password metadata exists in config comments, avoid printing it.

## Troubleshooting

- Connection timeout: verify network, host status, firewall, jump host reachability; retry long commands with `--timeout 300`.
- Alias not found: run `find` with the user's keyword or list all servers.
- Daemon stuck: run `ssh_daemon.py stop <alias>`, then retry; use `--no-daemon` for direct mode.
- Sudo asks for a password: use `--sudo`; if it still fails, confirm the host has password metadata in `~/.ssh/config` and that the SSH password is also valid for sudo.
- Transfer path mangled on Windows: ensure `MSYS_NO_PATHCONV=1` is set for upload/download/server-transfer commands.
- Passphrase key on Windows: confirm Windows OpenSSH Authentication Agent is running and the key is loaded.
