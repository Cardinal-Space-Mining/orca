#!/usr/bin/python3 -I
'''
orchestrator-share: share this machine's internet connection with devices on
one of its Ethernet ports (NAT + DHCP), for the orchestrator.

The orchestrator installs it as /usr/local/sbin/orchestrator-share, owned by
root, with a sudoers rule that lets the user run it without a password. It
takes only checked arguments and reads no file a user can write, so running
it without a password can't be used to do anything else as root.

Everything it changes is runtime state, gone after a reboot:
  - the port's address, and NetworkManager told to leave the port alone
    (`nmcli device set <port> managed no`, not saved)
  - IP forwarding (put back when the last share stops)
  - iptables rules tagged "orchestrator-share:<name>": NAT for the shared
    subnet, forwarding to and from the port, DHCP and traffic from the
    subnet to this machine. Stopping removes every rule with the tag.
  - Energy Efficient Ethernet switched off on the port (USB adapters with
    RTL8153 chips drop packets with it on), put back on stop
  - its own dnsmasq, answering DHCP only (no DNS), with its files in
    /run/orchestrator-share/<name>/. Other dnsmasq instances are left alone.
DHCP leases are kept in /var/lib/orchestrator-share/<name>.leases, so a device
usually gets the same address next time.

Usage:
    orchestrator-share up --name pits --downstream enx00e04c2646b5
        [--upstream wlp5s0] [--subnet 10.11.11.0/24] [--dns 8.8.8.8,1.1.1.1]
        [--lease 12h] [--keep-eee] [--allow-default-route]
    orchestrator-share down --name pits
    orchestrator-share status

Each command prints one JSON object: {"ok": true, ...} or {"ok": false,
"error": "..."} (with exit status 1). status works without root.
'''

import argparse
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time


STATE_ROOT = '/run/orchestrator-share'
LEASE_ROOT = '/var/lib/orchestrator-share'
TAG = 'orchestrator-share:'
SAFE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]{0,19}$')
INTERFACE_PATTERN = re.compile(r'^[A-Za-z0-9_.-]{1,15}$')
LEASE_PATTERN = re.compile(r'^[0-9]{1,4}[mhd]$')
DEFAULT_SUBNET = '10.11.11.0/24'
DEFAULT_DNS = '8.8.8.8,1.1.1.1'
DEFAULT_LEASE = '12h'
FIREWALL_CHAINS = (('nat', 'POSTROUTING'), ('filter', 'FORWARD'),
                   ('filter', 'INPUT'))


class ShareError(Exception):
    pass


def run(argv, check=True):
    result = subprocess.run(argv, capture_output=True, text=True,
                            env={'PATH': SAFE_PATH, 'LC_ALL': 'C'})
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise ShareError('%s failed: %s' % (argv[0], message))
    return result


def readText(path):
    try:
        with open(path) as file:
            return file.read().strip()
    except OSError:
        return None


def writeText(path, text):
    with open(path, 'w') as file:
        file.write(text)



# --- Checks -------------------------------------------------------------------



def checkName(name):
    if not NAME_PATTERN.match(name or ''):
        raise ShareError('a share name is lowercase letters, digits and '
                         'dashes (up to 20)')
    return name


def checkInterface(name, role):
    if not INTERFACE_PATTERN.match(name or '') \
            or not os.path.isdir('/sys/class/net/' + name):
        raise ShareError('no network interface "%s" (%s)' % (name, role))
    return name


def isWireless(name):
    return os.path.exists('/sys/class/net/%s/wireless' % name) \
        or os.path.exists('/sys/class/net/%s/phy80211' % name)


def defaultRouteInterfaces():
    output = run(['ip', '-j', 'route', 'show', 'default'], check=False).stdout
    try:
        return {route['dev'] for route in json.loads(output or '[]')
                if route.get('dev')}
    except ValueError:
        return set()


def interfaceNetworks():
    '''[(interface, IPv4Interface)] for every address on this machine.'''
    output = run(['ip', '-j', 'addr', 'show'], check=False).stdout
    found = []
    for entry in json.loads(output or '[]'):
        for address in entry.get('addr_info', []):
            if address.get('family') == 'inet':
                found.append((entry['ifname'], ipaddress.ip_interface(
                    '%s/%d' % (address['local'], address['prefixlen']))))
    return found


def checkSubnet(text, downstream):
    try:
        network = ipaddress.ip_network(text, strict=True)
    except ValueError:
        raise ShareError('"%s" is not a subnet like 10.11.11.0/24' % text)
    if network.version != 4 or not network.is_private \
            or not 16 <= network.prefixlen <= 29:
        raise ShareError('the shared subnet must be a private IPv4 network '
                         'between /16 and /29')
    for interface, address in interfaceNetworks():
        if interface != downstream and address.network.overlaps(network):
            raise ShareError(
                '%s overlaps %s on %s: pick another subnet'
                % (network, address.network, interface))
    return network


def checkDns(text):
    servers = []
    for item in (text or '').split(','):
        try:
            servers.append(str(ipaddress.IPv4Address(item.strip())))
        except ValueError:
            raise ShareError('"%s" is not an IPv4 DNS server' % item.strip())
    if not servers:
        raise ShareError('give at least one DNS server')
    return servers


def checkLease(text):
    if not LEASE_PATTERN.match(text or ''):
        raise ShareError('a lease time is like 30m, 12h or 1d')
    return text



# --- State --------------------------------------------------------------------



def stateDirectory(name):
    return os.path.join(STATE_ROOT, name)


def loadState(name):
    text = readText(os.path.join(stateDirectory(name), 'state.json'))
    return json.loads(text) if text else None


def saveState(state):
    directory = stateDirectory(state['name'])
    os.makedirs(directory, mode=0o755, exist_ok=True)
    writeText(os.path.join(directory, 'state.json'),
              json.dumps(state, indent=2) + '\n')


def activeNames():
    try:
        names = sorted(os.listdir(STATE_ROOT))
    except OSError:
        return []
    return [name for name in names if NAME_PATTERN.match(name)
            and os.path.isfile(os.path.join(STATE_ROOT, name, 'state.json'))]


def processAlive(pid, conf_path):
    '''True if pid is our dnsmasq (not a reused pid).'''
    command = readText('/proc/%d/cmdline' % pid) if pid else None
    return bool(command) and conf_path in command



# --- Pieces -------------------------------------------------------------------



def networkManagerRunning():
    if not shutil.which('nmcli', path=SAFE_PATH):
        return False
    output = run(['nmcli', '-t', '-f', 'RUNNING', 'general'],
                 check=False).stdout
    return output.strip() == 'running'


def isManaged(interface):
    output = run(['nmcli', '-t', '-f', 'GENERAL.NM-MANAGED', 'device', 'show',
                  interface], check=False).stdout
    return output.strip().endswith(':yes')


def eeeEnabled(interface):
    if not shutil.which('ethtool', path=SAFE_PATH):
        return None
    output = run(['ethtool', '--show-eee', interface], check=False).stdout
    match = re.search(r'EEE status:\s*(\w+)', output)
    if not match or match.group(1) == 'not':  # "not supported"
        return None
    return match.group(1) == 'enabled'


def firewallRules(name, downstream, upstream, subnet):
    comment = ['-m', 'comment', '--comment', TAG + name]
    out = ['-o', upstream] if upstream else []
    return [
        ('nat', 'POSTROUTING', ['-s', subnet, '!', '-d', subnet] + out
         + comment + ['-j', 'MASQUERADE']),
        ('filter', 'FORWARD', ['-i', downstream, '-s', subnet] + comment
         + ['-j', 'ACCEPT']),
        ('filter', 'FORWARD', ['-o', downstream, '-d', subnet, '-m',
                               'conntrack', '--ctstate',
                               'RELATED,ESTABLISHED'] + comment
         + ['-j', 'ACCEPT']),
        ('filter', 'INPUT', ['-i', downstream, '-p', 'udp', '--dport', '67']
         + comment + ['-j', 'ACCEPT']),
        ('filter', 'INPUT', ['-i', downstream, '-s', subnet] + comment
         + ['-j', 'ACCEPT']),
    ]


def removeFirewallRules(name):
    '''Delete every rule tagged for this share, whatever created it.'''
    tag = TAG + name
    for table, chain in FIREWALL_CHAINS:
        listing = run(['iptables', '-w', '-t', table, '-S', chain],
                      check=False).stdout
        for line in listing.splitlines():
            parts = shlex.split(line)
            if line.startswith('-A ') and tag in parts:
                parts[0] = '-D'
                run(['iptables', '-w', '-t', table] + parts, check=False)


def dhcpRange(network):
    hosts = network.num_addresses - 2
    server = network.network_address + 1
    first = server + (29 if hosts > 64 else 1)
    last = network.broadcast_address - 1
    return server, first, last


def startDnsmasq(state):
    binary = shutil.which('dnsmasq', path=SAFE_PATH)
    if not binary:
        raise ShareError('dnsmasq is not installed '
                         '(sudo apt install dnsmasq-base)')
    directory = stateDirectory(state['name'])
    conf_path = os.path.join(directory, 'dnsmasq.conf')
    os.makedirs(LEASE_ROOT, mode=0o755, exist_ok=True)
    network = ipaddress.ip_network(state['subnet'])
    writeText(conf_path, '\n'.join([
        '# orchestrator-share ' + state['name'],
        'port=0',                       # DHCP only: no DNS server
        'interface=' + state['downstream'],
        'except-interface=lo',
        'bind-dynamic',
        'dhcp-authoritative',
        'dhcp-range=%s,%s,%s,%s' % (state['dhcp_first'], state['dhcp_last'],
                                    network.netmask, state['lease']),
        'dhcp-option=option:router,' + state['server'],
        'dhcp-option=option:dns-server,' + ','.join(state['dns']),
        'dhcp-leasefile=%s/%s.leases' % (LEASE_ROOT, state['name']),
        'pid-file=' + os.path.join(directory, 'dnsmasq.pid'),
        'log-facility=' + os.path.join(directory, 'dnsmasq.log'),
        'log-dhcp',
        '']))
    run([binary, '--conf-file=' + conf_path])
    for _ in range(20):
        pid = readText(os.path.join(directory, 'dnsmasq.pid'))
        if pid and pid.isdigit():
            return int(pid), conf_path
        time.sleep(0.1)
    raise ShareError('dnsmasq started but wrote no pid file')


def stopDnsmasq(state):
    pid = state.get('dnsmasq_pid')
    conf_path = os.path.join(stateDirectory(state['name']), 'dnsmasq.conf')
    if not processAlive(pid, conf_path):
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not processAlive(pid, conf_path):
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)



# --- Commands -----------------------------------------------------------------



def commandUp(arguments):
    name = checkName(arguments.name)
    downstream = checkInterface(arguments.downstream, 'downstream')
    upstream = checkInterface(arguments.upstream, 'upstream') \
        if arguments.upstream else None
    if upstream == downstream:
        raise ShareError('upstream and downstream are the same port')
    if isWireless(downstream):
        raise ShareError(
            '%s is Wi-Fi: devices can only join a Wi-Fi adapter that runs as '
            'an access point. Share over an Ethernet port.' % downstream)
    if downstream in defaultRouteInterfaces() \
            and not arguments.allow_default_route:
        raise ShareError(
            "%s is this machine's own internet connection right now: sharing "
            'over it would cut this machine off' % downstream)
    for other in activeNames():
        state = loadState(other)
        if other != name and state and state['downstream'] == downstream:
            raise ShareError('%s is already shared as "%s"' % (downstream,
                                                               other))
    network = checkSubnet(arguments.subnet, downstream)
    dns = checkDns(arguments.dns)
    lease = checkLease(arguments.lease)

    if loadState(name):
        commandDown(argparse.Namespace(name=name), quiet=True)
    removeFirewallRules(name)  # leftovers from a crash

    server, first, last = dhcpRange(network)
    warnings = []
    state = {
        'name': name, 'downstream': downstream, 'upstream': upstream,
        'subnet': str(network), 'server': str(server),
        'dhcp_first': str(first), 'dhcp_last': str(last),
        'dns': dns, 'lease': lease, 'started_s': time.time(),
        'ip_forward_was': readText('/proc/sys/net/ipv4/ip_forward'),
        'nm_managed_was': None, 'eee_was': None, 'dnsmasq_pid': None,
    }
    saveState(state)  # so a failed start can still be stopped
    try:
        if networkManagerRunning():
            state['nm_managed_was'] = isManaged(downstream)
            run(['nmcli', 'device', 'set', downstream, 'managed', 'no'])
            time.sleep(0.5)  # let NetworkManager let go of the port
        run(['ip', 'addr', 'flush', 'dev', downstream])
        run(['ip', 'addr', 'add', '%s/%d' % (server, network.prefixlen),
             'dev', downstream])
        run(['ip', 'link', 'set', downstream, 'up'])
        if not arguments.keep_eee:
            state['eee_was'] = eeeEnabled(downstream)
            if state['eee_was']:
                run(['ethtool', '--set-eee', downstream, 'eee', 'off'],
                    check=False)
        writeText('/proc/sys/net/ipv4/ip_forward', '1\n')
        for table, chain, rule in firewallRules(name, downstream, upstream,
                                                str(network)):
            run(['iptables', '-w', '-t', table, '-I', chain, '1'] + rule)
        if not defaultRouteInterfaces() - {downstream}:
            warnings.append('this machine has no internet connection right '
                            'now: devices get addresses but no internet')
        state['dnsmasq_pid'], _ = startDnsmasq(state)
        saveState(state)
    except Exception:
        saveState(state)
        commandDown(argparse.Namespace(name=name), quiet=True)
        raise
    return {'session': state, 'warnings': warnings}


def commandDown(arguments, quiet=False):
    name = checkName(arguments.name)
    state = loadState(name)
    if state is None:
        if quiet:
            return {}
        raise ShareError('"%s" is not being shared' % name)
    stopDnsmasq(state)
    removeFirewallRules(name)
    downstream = state['downstream']
    if os.path.isdir('/sys/class/net/' + downstream):
        network = ipaddress.ip_network(state['subnet'])
        run(['ip', 'addr', 'del', '%s/%d' % (state['server'],
                                             network.prefixlen),
             'dev', downstream], check=False)
        if state.get('eee_was'):
            run(['ethtool', '--set-eee', downstream, 'eee', 'on'],
                check=False)
        if state.get('nm_managed_was'):
            run(['nmcli', 'device', 'set', downstream, 'managed', 'yes'],
                check=False)
    if state.get('ip_forward_was') == '0' \
            and not [other for other in activeNames() if other != name]:
        writeText('/proc/sys/net/ipv4/ip_forward', '0\n')
    shutil.rmtree(stateDirectory(name), ignore_errors=True)
    return {'stopped': name}


def leases(name):
    found = []
    text = readText(os.path.join(LEASE_ROOT, name + '.leases')) or ''
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            found.append({'expires_s': int(parts[0]), 'mac': parts[1],
                          'address': parts[2],
                          'hostname': None if parts[3] == '*' else parts[3]})
    return found


def commandStatus(arguments):
    sessions = []
    for name in activeNames():
        state = loadState(name)
        conf_path = os.path.join(stateDirectory(name), 'dnsmasq.conf')
        state['dnsmasq_running'] = processAlive(state.get('dnsmasq_pid'),
                                                conf_path)
        state['carrier'] = readText('/sys/class/net/%s/carrier'
                                    % state['downstream']) == '1'
        state['leases'] = leases(name)
        sessions.append(state)
    return {'sessions': sessions}


def main():
    parser = argparse.ArgumentParser(
        prog='orchestrator-share',
        description="Share this machine's internet over an Ethernet port.")
    commands = parser.add_subparsers(dest='command', required=True)
    up = commands.add_parser('up')
    up.add_argument('--name', required=True)
    up.add_argument('--downstream', required=True)
    up.add_argument('--upstream')
    up.add_argument('--subnet', default=DEFAULT_SUBNET)
    up.add_argument('--dns', default=DEFAULT_DNS)
    up.add_argument('--lease', default=DEFAULT_LEASE)
    up.add_argument('--keep-eee', action='store_true')
    up.add_argument('--allow-default-route', action='store_true')
    down = commands.add_parser('down')
    down.add_argument('--name', required=True)
    commands.add_parser('status')
    arguments = parser.parse_args()

    handlers = {'up': commandUp, 'down': commandDown,
                'status': commandStatus}
    os.umask(0o022)  # state and leases stay readable for status
    try:
        if arguments.command != 'status' and os.geteuid() != 0:
            raise ShareError('needs root: run it with sudo')
        result = handlers[arguments.command](arguments)
    except (ShareError, OSError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        return 1
    print(json.dumps(dict(result, ok=True)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
