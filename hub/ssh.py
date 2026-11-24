'''
SSH command lines for reaching machines.

The hub builds every SSH option itself instead of relying on ~/.ssh/config, so
it behaves the same on every operator's laptop. ~/.ssh/config still applies to
anything not set here (keys, for example).

Jumps use ProxyCommand rather than ProxyJump: ProxyJump starts the jump
connection with only the config file's options, so BatchMode wouldn't apply
and a missing key would leave it waiting for a password nobody can type.
'''

from __future__ import annotations

import shlex

from .inventory import Route


SSH_OPTIONS = [
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=5',
    # Notice a dead link within ~9 s instead of waiting for TCP to time out.
    '-o', 'ServerAliveInterval=3',
    '-o', 'ServerAliveCountMax=3',
]

AGENT_REMOTE_PATH = '"$HOME/.csm-hub/agent.py"'

# Runs in the remote user's login shell (zsh on macOS, bash on Ubuntu).
ATTACH_COMMAND = (
    'test -f ' + AGENT_REMOTE_PATH
    + ' || { echo \'{"event": "agent-missing"}\'; exit 0; }; '
    + 'exec python3 -u ' + AGENT_REMOTE_PATH + ' attach')

DEPLOY_COMMAND = (
    'mkdir -p "$HOME/.csm-hub" && '
    'cat > "$HOME/.csm-hub/agent.py.new" && '
    'mv "$HOME/.csm-hub/agent.py.new" ' + AGENT_REMOTE_PATH)


def sshBaseCommand(route: Route, machines_by_id: dict) -> list:
    '''ssh and its options for a route, without the target or command.'''
    command = ['ssh', '-T'] + SSH_OPTIONS
    if route.port:
        command += ['-p', str(route.port)]
    if route.host_key_alias:
        command += ['-o', 'HostKeyAlias=' + route.host_key_alias]
    if route.via:
        jump_route = machines_by_id[route.via].routes[0]
        jump_command = (sshBaseCommand(jump_route, machines_by_id)
                        + ['-W', '%h:%p', jump_route.target()])
        command += ['-o', 'ProxyCommand=' + shlex.join(jump_command)]
    return command


def sshCommand(route: Route, machines_by_id: dict, remote_command: str) -> list:
    return (sshBaseCommand(route, machines_by_id)
            + [route.target(), remote_command])
