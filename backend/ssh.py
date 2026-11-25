'''
SSH command lines for reaching machines.

Orca builds every SSH option itself instead of relying on
~/.ssh/config, so it behaves the same on every operator's laptop. ~/.ssh/config
still applies to anything not set here (keys, for example).

Jumps use ProxyCommand rather than ProxyJump: ProxyJump starts the jump
connection with only the config file's options, so BatchMode wouldn't apply
and a missing key would leave it waiting for a password nobody can type.
'''

from __future__ import annotations

import asyncio
import os
import shlex

from .inventory import Route


SSH_OPTIONS = [
    '-o', 'BatchMode=yes',
    '-o', 'ConnectTimeout=5',
    # Notice a dead link within ~9 s instead of waiting for TCP to time out.
    '-o', 'ServerAliveInterval=3',
    '-o', 'ServerAliveCountMax=3',
]

AGENT_REMOTE_PATH = '"$HOME/.orca/agent.py"'

# Runs in the remote user's login shell (zsh on macOS, bash on Ubuntu).
ATTACH_COMMAND = (
    'test -f ' + AGENT_REMOTE_PATH
    + ' || { echo \'{"event": "agent-missing"}\'; exit 0; }; '
    + 'exec python3 -u ' + AGENT_REMOTE_PATH + ' attach')

DEPLOY_COMMAND = (
    'mkdir -p "$HOME/.orca" && '
    'cat > "$HOME/.orca/agent.py.new" && '
    'mv "$HOME/.orca/agent.py.new" ' + AGENT_REMOTE_PATH)


def sshBaseCommand(route: Route, jumpRouteFor) -> list:
    '''ssh and its options for a route, without the target or command.
    jumpRouteFor(machine_id) gives the route to jump through for `via`
    (normally the one that machine is connected on right now).'''
    command = ['ssh', '-T'] + SSH_OPTIONS
    if route.port:
        command += ['-p', str(route.port)]
    if route.host_key_alias:
        command += ['-o', 'HostKeyAlias=' + route.host_key_alias]
    if route.via:
        jump_route = jumpRouteFor(route.via)
        if jump_route is None:
            raise OSError('no route to %s yet' % route.via)
        jump_command = (sshBaseCommand(jump_route, jumpRouteFor)
                        + ['-W', '%h:%p', jump_route.target()])
        command += ['-o', 'ProxyCommand=' + shlex.join(jump_command)]
    return command


def sshCommand(route: Route, jumpRouteFor, remote_command: str) -> list:
    return (sshBaseCommand(route, jumpRouteFor)
            + [route.target(), remote_command])


def keyscanCommand(route: Route, jumpRouteFor) -> list:
    '''Read a route's host keys. For a jump route, ssh-keyscan runs on the
    machine jumped through, since only it can reach the address.'''
    keyscan = ['ssh-keyscan', '-T', '5']
    if route.port:
        keyscan += ['-p', str(route.port)]
    keyscan.append(route.host)
    if not route.via:
        return keyscan
    jump_route = jumpRouteFor(route.via)
    if jump_route is None:
        raise OSError('no route to %s yet' % route.via)
    return sshCommand(jump_route, jumpRouteFor, shlex.join(keyscan))


def knownHostsName(route: Route) -> str:
    '''The name a route's key is checked under in known_hosts.'''
    name = route.host_key_alias or route.host
    if route.port and route.port != 22:
        return '[%s]:%d' % (name, route.port)
    return name


# --- Host keys ---------------------------------------------------------------


KNOWN_HOSTS_PATH = os.path.expanduser('~/.ssh/known_hosts')


async def scanHostKeys(route: Route, jumpRouteFor) -> dict:
    """A route's host keys and their fingerprints, for a person to compare
    before trusting them. Raises ValueError if nothing answers."""
    process = await asyncio.create_subprocess_exec(
        *keyscanCommand(route, jumpRouteFor),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await asyncio.wait_for(process.communicate(), 20.0)
    keys = [line for line in output.decode().splitlines()
            if line and not line.startswith('#')]
    if not keys:
        raise ValueError('could not read host keys from ' + route.host)
    fingerprint = await asyncio.create_subprocess_exec(
        'ssh-keygen', '-l', '-f', '-', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE)
    fingerprints, _ = await fingerprint.communicate(
        '\n'.join(keys).encode() + b'\n')
    return {'route': route.describe(), 'name': knownHostsName(route),
            'keys': keys, 'fingerprints': fingerprints.decode().splitlines()}


def keyData(key_line: str) -> str:
    """The key itself from a keyscan or known_hosts line."""
    return key_line.split()[-1]


def trustHostKeys(name: str, keys: list) -> None:
    """Add scanned keys to ~/.ssh/known_hosts under `name`."""
    lines = []
    for key in keys:
        _, key_type, key_data = key.split(None, 2)
        lines.append('%s %s %s\n' % (name, key_type, key_data))
    os.makedirs(os.path.dirname(KNOWN_HOSTS_PATH), mode=0o700, exist_ok=True)
    with open(KNOWN_HOSTS_PATH, 'a') as file:
        file.writelines(lines)


async def knownKeys(name: str) -> set:
    """Key data trusted under a known_hosts name (hashed entries too)."""
    process = await asyncio.create_subprocess_exec(
        'ssh-keygen', '-F', name, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    output, _ = await process.communicate()
    return {keyData(line) for line in output.decode().splitlines()
            if line and not line.startswith('#')}
