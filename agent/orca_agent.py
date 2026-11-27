#!/usr/bin/env python3
'''
Orca agent.

Runs on every machine Orca manages: the robot's mac host, its
Ubuntu VM, client laptops, and Orca's own machine. It is one file
that uses only the standard library, so Orca can copy it over SSH
and run it with any Python 3.9+, including macOS's /usr/bin/python3.

Modes (first command-line argument):
    daemon   Long-running server. Answers requests on a unix socket and will
             own the processes Orca starts, so they outlive
             Orca's connection.
    attach   Bridges stdin/stdout to the daemon's socket, starting the daemon
             first if it isn't running. Orca runs this over SSH, so
             the agent never opens a network port and SSH keys are the only
             authentication.
    hash     Prints the hash of this file (Orca compares it with
             its copy).

Protocol: one JSON object per line, in both directions.
    request    {"id": 1, "method": "system.stats", "params": {}}
    response   {"id": 1, "result": ...}
               {"id": 1, "error": {"message": "..."}}
    event      {"event": "stats", "data": ...}
The daemon sends a "hello" event as soon as a client connects.
'''

import asyncio
import codecs
import collections
import ctypes
import fcntl
import getpass
import glob
import hashlib
import ipaddress
import json
import os
import platform
import plistlib
import pty
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time


# --- Constants ----------------------------------------------------------------



PROTOCOL_VERSION = 2

AGENT_PATH = os.path.abspath(__file__)
# ORCA_AGENT_DIR lets tests run an agent beside the real one.
AGENT_DIR = os.environ.get('ORCA_AGENT_DIR') \
    or os.path.join(os.path.expanduser('~'), '.orca')
SOCKET_PATH = os.path.join(AGENT_DIR, 'agent.sock')
# Unix socket paths are limited to ~104 bytes, so the daemon and `attach`
# both work from AGENT_DIR and use this relative name.
SOCKET_NAME = 'agent.sock'
LOCK_PATH = os.path.join(AGENT_DIR, 'daemon.lock')
LOG_PATH = os.path.join(AGENT_DIR, 'agent.log')
RUNS_DIR = os.path.join(AGENT_DIR, 'runs')
MAX_LOG_BYTES = 2 * 1024 * 1024

# --- Supervised processes ---
# Robot disks are small (the VM's is 64 GB and is backed up whole), so cap
# what one process can write to its log.
MAX_PROCESS_LOG_BYTES = 20 * 1024 * 1024
MAX_BUFFERED_LINES = 5000
MAX_FINISHED_PROCESSES = 30
MAX_LINE_CHARACTERS = 4000
OUTPUT_FLUSH_S = 0.1
# How long an exit waits for the process's last output to be read.
OUTPUT_DRAIN_TIMEOUT_S = 2.0
PARTIAL_LINE_FLUSH_S = 0.5
PTY_COLUMNS = 200
PTY_ROWS = 50
# Ctrl-C semantics: SIGINT to the whole process group first, then escalate.
DEFAULT_STOP_GRACE_S = 20.0
TERM_GRACE_S = 5.0
RUN_LOG_MAX_AGE_S = 14 * 24 * 3600

IS_MACOS = sys.platform == 'darwin'
# Non-interactive SSH sessions on macOS don't read .zprofile, so Homebrew's
# tools (qemu, socat, rsync) are missing from PATH unless we add them.
MACOS_EXTRA_PATHS = ['/opt/homebrew/bin', '/opt/homebrew/sbin', '/usr/local/bin']

DAEMON_START_TIMEOUT_S = 10.0
DAEMON_LOCK_TIMEOUT_S = 5.0
STATS_CACHE_S = 1.0
WIFI_CACHE_S = 15.0
WIFI_COMMAND_TIMEOUT_S = 20.0
COMMAND_TIMEOUT_S = 5.0


def fileHash(path):
    '''Short sha256 of a file, or None if it can't be read.'''
    try:
        with open(path, 'rb') as file:
            return hashlib.sha256(file.read()).hexdigest()[:12]
    except OSError:
        return None


# Hash of the code this process is running; the file on disk may be newer.
AGENT_HASH = fileHash(AGENT_PATH)



# --- Command helpers ----------------------------------------------------------



def runCommand(args, timeout_s=COMMAND_TIMEOUT_S):
    '''Run a command and return its stdout, or None if it fails or is
    missing.'''
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def readText(path):
    try:
        with open(path) as file:
            return file.read()
    except OSError:
        return None


def parseElapsedTime(elapsed):
    '''Convert ps's etime format, [[dd-]hh:]mm:ss, to seconds.'''
    days = 0
    if '-' in elapsed:
        day_text, elapsed = elapsed.split('-', 1)
        days = int(day_text)
    parts = [int(part) for part in elapsed.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds



# --- System information -------------------------------------------------------



def osDescription():
    if IS_MACOS:
        version = runCommand(['sw_vers', '-productVersion'])
        return 'macOS ' + version.strip() if version else 'macOS'
    os_release = readText('/etc/os-release') or ''
    match = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', os_release, re.MULTILINE)
    return match.group(1) if match else platform.system()


def memoryTotalBytes():
    if IS_MACOS:
        output = runCommand(['sysctl', '-n', 'hw.memsize'])
        return int(output) if output else None
    meminfo = readText('/proc/meminfo') or ''
    match = re.search(r'^MemTotal:\s+(\d+) kB', meminfo, re.MULTILINE)
    return int(match.group(1)) * 1024 if match else None


def systemInfo(daemon_started_s):
    return {
        'hostname': socket.gethostname(),
        'user': getpass.getuser(),
        'home': os.path.expanduser('~'),
        'platform': 'macos' if IS_MACOS else 'linux',
        'os': osDescription(),
        'kernel': platform.release(),
        'arch': platform.machine(),
        'num_cpus': os.cpu_count(),
        'memory_total_bytes': memoryTotalBytes(),
        'python': platform.python_version(),
        'agent_hash': AGENT_HASH,
        'protocol_version': PROTOCOL_VERSION,
        'daemon_pid': os.getpid(),
        'daemon_started_s': daemon_started_s,
    }



# --- CPU and memory -----------------------------------------------------------



class MacosHost:
    '''Mach host statistics through libSystem (no extra processes).'''

    HOST_CPU_LOAD_INFO = 3
    CPU_STATE_COUNT = 4

    def __init__(self):
        self.library = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
        self.library.mach_host_self.restype = ctypes.c_uint32
        # mach_host_self() hands out a port right; take it once.
        self.host_port = self.library.mach_host_self()

    def cpuTicks(self):
        ticks = (ctypes.c_uint32 * self.CPU_STATE_COUNT)()
        count = ctypes.c_uint32(self.CPU_STATE_COUNT)
        result = self.library.host_statistics(
            self.host_port, self.HOST_CPU_LOAD_INFO, ticks,
            ctypes.byref(count))
        if result != 0:
            return None
        # Order from <mach/machine.h>: user, system, idle, nice.
        user, system, idle, nice = ticks
        return (user + system + nice, user + system + idle + nice)


MACOS_HOST = None


def cpuTicks():
    '''Return (busy_ticks, total_ticks) since boot.'''
    global MACOS_HOST
    if IS_MACOS:
        if MACOS_HOST is None:
            MACOS_HOST = MacosHost()
        return MACOS_HOST.cpuTicks()
    stat = readText('/proc/stat')
    if not stat:
        return None
    # cpu  user nice system idle iowait irq softirq steal ...
    values = [int(value) for value in stat.split('\n', 1)[0].split()[1:9]]
    idle = values[3] + values[4]
    total = sum(values)
    return (total - idle, total)


def memoryUsedBytes():
    if IS_MACOS:
        output = runCommand(['vm_stat'])
        if not output:
            return None
        page_size = int(re.search(r'page size of (\d+) bytes', output).group(1))
        pages = {}
        for name, value in re.findall(r'^(.+?):\s+(\d+)\.', output, re.MULTILINE):
            pages[name] = int(value)
        # Roughly what Activity Monitor calls "Memory Used".
        used_pages = (pages.get('Pages active', 0)
                      + pages.get('Pages wired down', 0)
                      + pages.get('Pages occupied by compressor', 0))
        return used_pages * page_size
    meminfo = readText('/proc/meminfo') or ''
    total = re.search(r'^MemTotal:\s+(\d+) kB', meminfo, re.MULTILINE)
    available = re.search(r'^MemAvailable:\s+(\d+) kB', meminfo, re.MULTILINE)
    if not total or not available:
        return None
    return (int(total.group(1)) - int(available.group(1))) * 1024


def uptimeSeconds():
    if IS_MACOS:
        output = runCommand(['sysctl', '-n', 'kern.boottime'])
        match = re.search(r'sec = (\d+)', output or '')
        return time.time() - int(match.group(1)) if match else None
    uptime = readText('/proc/uptime')
    return float(uptime.split()[0]) if uptime else None


def temperatureCelsius():
    '''Hottest thermal zone on Linux. macOS needs root for sensors.'''
    if IS_MACOS:
        return None
    readings = []
    for path in glob.glob('/sys/class/thermal/thermal_zone*/temp'):
        text = readText(path)
        if text and text.strip().lstrip('-').isdigit():
            readings.append(int(text) / 1000.0)
    return max(readings) if readings else None



# --- Network interfaces -------------------------------------------------------



def linuxInterfaceKind(name, entry):
    base = '/sys/class/net/' + name
    if entry.get('link_type') == 'can':
        return 'can'
    if os.path.exists(base + '/wireless'):
        return 'wifi'
    if os.path.exists(base + '/bridge'):
        return 'bridge'
    if os.path.exists(base + '/device'):
        return 'ethernet'
    return 'virtual'


def linuxInterfaces():
    output = runCommand(['ip', '-j', 'addr', 'show'])
    if output is None:
        return []
    interfaces = []
    for entry in json.loads(output):
        name = entry.get('ifname')
        if not name or entry.get('link_type') == 'loopback':
            continue
        flags = entry.get('flags', [])
        operstate = entry.get('operstate')
        counters = linuxByteCounters(name)
        interfaces.append({
            'name': name,
            'label': None,
            'rx_bytes': counters[0] if counters else None,
            'tx_bytes': counters[1] if counters else None,
            'kind': linuxInterfaceKind(name, entry),
            'is_up': operstate == 'UP' or (
                operstate == 'UNKNOWN' and 'LOWER_UP' in flags),
            'mac': entry.get('address'),
            'ipv4': [
                address['local'] + '/' + str(address['prefixlen'])
                for address in entry.get('addr_info', [])
                if address.get('family') == 'inet'],
        })
    return interfaces


def linuxByteCounters(name):
    base = '/sys/class/net/%s/statistics/' % name
    received = readText(base + 'rx_bytes')
    sent = readText(base + 'tx_bytes')
    if received is None or sent is None:
        return None
    return int(received), int(sent)


def macosByteCounters():
    '''{interface: (received, sent)} from netstat's link-level rows. Counted
    from the right, since the Address column is sometimes empty.'''
    output = runCommand(['netstat', '-ibn']) or ''
    counters = {}
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or not fields[2].startswith('<Link#'):
            continue
        try:
            counters[fields[0]] = (int(fields[-5]), int(fields[-2]))
        except ValueError:
            continue
    return counters


def linuxWifi(interface_name):
    '''Connected network of one Wi-Fi interface, from NetworkManager.'''
    output = runCommand([
        'nmcli', '-t', '-f', 'IN-USE,SSID,CHAN,FREQ,SIGNAL',
        'device', 'wifi', 'list', 'ifname', interface_name, '--rescan', 'no'])
    if output is None:
        return None
    for line in output.splitlines():
        # Terse mode escapes colons inside values as "\:".
        fields = [field.replace('\\:', ':')
                  for field in re.split(r'(?<!\\):', line)]
        if len(fields) >= 5 and fields[0] == '*':
            return {
                'connected': True,
                'ssid': fields[1],
                'channel': int(fields[2]) if fields[2].isdigit() else None,
                'frequency_mhz': int(fields[3].split()[0])
                if fields[3] else None,
                'signal_percent': int(fields[4])
                if fields[4].isdigit() else None,
            }
    return {'connected': False}


def macosHardwarePorts():
    '''Map device name (en0) to its hardware port name (Ethernet, Wi-Fi).'''
    output = runCommand(['networksetup', '-listallhardwareports']) or ''
    ports = {}
    for port, device in re.findall(
            r'Hardware Port: (.+)\nDevice: (\S+)', output):
        ports[device] = port
    return ports


def macosInterfaceKind(name, port):
    if port and 'Wi-Fi' in port:
        return 'wifi'
    if name.startswith('bridge') or (port and 'Bridge' in port):
        return 'bridge'
    if port:
        return 'ethernet'
    return 'virtual'


def parseIfconfig(output):
    interfaces = []
    current = None
    for line in output.splitlines():
        if line and not line[0].isspace():
            flags = re.search(r'<([^>]*)>', line)
            current = {
                'name': line.split(':', 1)[0],
                'flags': flags.group(1).split(',') if flags else [],
                'ipv4': [], 'mac': None, 'status': None,
            }
            interfaces.append(current)
            continue
        if current is None:
            continue
        fields = line.split()
        if fields[:1] == ['inet'] and len(fields) >= 4:
            prefix_length = bin(int(fields[3], 16)).count('1')
            current['ipv4'].append(fields[1] + '/' + str(prefix_length))
        elif fields[:1] == ['ether']:
            current['mac'] = fields[1]
        elif fields[:1] == ['status:']:
            current['status'] = fields[1]
    return interfaces


def macosInterfaces(hardware_ports):
    output = runCommand(['ifconfig', '-a'])
    if output is None:
        return []
    counters = macosByteCounters()
    interfaces = []
    for entry in parseIfconfig(output):
        name = entry['name']
        port = hardware_ports.get(name)
        # Skip the many virtual interfaces macOS keeps (utun, awdl, anpi...)
        # unless they carry an address, like the VM's host-only bridge.
        if name == 'lo0' or (port is None and not entry['ipv4']):
            continue
        if entry['status'] is not None:
            is_up = entry['status'] == 'active'
        else:
            is_up = 'UP' in entry['flags'] and 'RUNNING' in entry['flags']
        received, sent = counters.get(name, (None, None))
        interfaces.append({
            'name': name,
            'label': port,
            'rx_bytes': received,
            'tx_bytes': sent,
            'kind': macosInterfaceKind(name, port),
            'is_up': is_up,
            'mac': entry['mac'],
            'ipv4': entry['ipv4'],
        })
    return interfaces


def macosWifi():
    '''Status of the mac's own Wi-Fi. macOS redacts the network name (even
    for root) unless the process has Location Services access, so only the
    channel, band and signal are reliable.'''
    output = runCommand(['system_profiler', 'SPAirPortDataType', '-json'],
                        timeout_s=WIFI_COMMAND_TIMEOUT_S)
    if output is None:
        return None
    try:
        data = json.loads(output)['SPAirPortDataType'][0]
        interface = data['spairport_airport_interfaces'][0]
    except (ValueError, KeyError, IndexError):
        return None
    network = interface.get('spairport_current_network_information')
    if not network:
        return {'connected': False,
                'power': interface.get('spairport_status_information')}
    channel_text = str(network.get('spairport_network_channel', ''))
    channel = re.match(r'\d+', channel_text)
    signal_noise = str(network.get('spairport_signal_noise', ''))
    signal = re.match(r'(-?\d+) dBm', signal_noise)
    ssid = network.get('_name')
    return {
        'connected': True,
        'ssid': None if ssid == '<redacted>' else ssid,
        'ssid_redacted': ssid == '<redacted>',
        'channel': int(channel.group(0)) if channel else None,
        'band': channel_text,
        'signal_dbm': int(signal.group(1)) if signal else None,
        'phy_mode': network.get('spairport_network_phymode'),
        'rate_mbps': network.get('spairport_network_rate'),
    }


def defaultRoute():
    if IS_MACOS:
        output = runCommand(['route', '-n', 'get', 'default']) or ''
        gateway = re.search(r'gateway: (\S+)', output)
        interface = re.search(r'interface: (\S+)', output)
        if not interface:
            return None
        return {'gateway': gateway.group(1) if gateway else None,
                'interface': interface.group(1)}
    output = runCommand(['ip', '-j', 'route', 'show', 'default'])
    routes = json.loads(output) if output else []
    if not routes:
        return None
    return {'gateway': routes[0].get('gateway'),
            'interface': routes[0].get('dev')}



# --- Statistics snapshot ------------------------------------------------------



class StatsSampler:
    '''Builds the periodic "stats" snapshot. Shared by all clients so CPU
    deltas stay meaningful and commands aren't run once per client.'''

    def __init__(self):
        self.previous_ticks = cpuTicks()
        self.hardware_ports = macosHardwarePorts() if IS_MACOS else {}
        # --- Wi-Fi details, refreshed in the background (system_profiler
        # can take several seconds) ---
        self.wifi_lock = threading.Lock()
        self.wifi_cache = {}  # interface name -> (sampled_s, wifi info)
        self.wifi_refreshing = set()  # interface names; guarded by wifi_lock

    def cpuPercent(self):
        current = cpuTicks()
        previous, self.previous_ticks = self.previous_ticks, current
        if current is None or previous is None:
            return None
        busy = current[0] - previous[0]
        total = current[1] - previous[1]
        return 100.0 * busy / total if total > 0 else None

    def wifiFor(self, interface_name):
        '''Last known Wi-Fi details; starts a refresh when they're stale.
        Returns None until the first refresh finishes.'''
        with self.wifi_lock:
            cached = self.wifi_cache.get(interface_name)
            is_stale = (cached is None
                        or time.time() - cached[0] >= WIFI_CACHE_S)
            if is_stale and interface_name not in self.wifi_refreshing:
                self.wifi_refreshing.add(interface_name)
                threading.Thread(target=self.refreshWifi,
                                 args=(interface_name,), daemon=True).start()
        return cached[1] if cached else None

    def refreshWifi(self, interface_name):
        wifi = macosWifi() if IS_MACOS else linuxWifi(interface_name)
        with self.wifi_lock:
            self.wifi_cache[interface_name] = (time.time(), wifi)
            self.wifi_refreshing.discard(interface_name)

    def interfaces(self):
        if IS_MACOS:
            interfaces = macosInterfaces(self.hardware_ports)
        else:
            interfaces = linuxInterfaces()
        for interface in interfaces:
            if interface['kind'] == 'wifi':
                interface['wifi'] = self.wifiFor(interface['name'])
        return interfaces

    def sample(self):
        return {
            'time_s': time.time(),
            'cpu_percent': self.cpuPercent(),
            'load_average': list(os.getloadavg()),
            'memory_used_bytes': memoryUsedBytes(),
            'memory_total_bytes': memoryTotalBytes(),
            'uptime_s': uptimeSeconds(),
            'temperature_c': temperatureCelsius(),
            'interfaces': self.interfaces(),
            'default_route': defaultRoute(),
        }



# --- Workspace ----------------------------------------------------------------



def git(repository, *args):
    output = runCommand(['git', '-C', repository] + list(args))
    return output.strip() if output is not None else None


def workspaceStatus(path):
    '''Git and build state of a colcon workspace whose repo is <path>/src.'''
    root = os.path.expanduser(path)
    repository = os.path.join(root, 'src')
    status = {'path': path, 'exists': os.path.isdir(repository),
              'ros_distros': installedRosDistros()}
    if not status['exists']:
        return status  # a first deploy creates it

    head = git(repository, 'rev-parse', 'HEAD')
    status['is_git'] = head is not None
    if head is None:
        return status
    porcelain = git(repository, 'status', '--porcelain') or ''
    last_commit = (git(repository, 'log', '-1', '--format=%s%x1f%ct')
                   or '\x1f0').split('\x1f')
    submodules = git(repository, 'submodule', 'status') or ''
    status.update({
        'head': head,
        'branch': git(repository, 'rev-parse', '--abbrev-ref', 'HEAD'),
        'num_changes': len([line for line in porcelain.splitlines() if line]),
        'subject': last_commit[0],
        'commit_time_s': int(last_commit[1] or 0),
        # "+": submodule checked out at a different commit than recorded;
        # "-": not initialised.
        'num_submodules_off': len([
            line for line in submodules.splitlines()
            if line[:1] in ('+', '-', 'U')]),
    })

    latest_build = os.path.join(root, 'log', 'latest_build')
    status['has_install'] = os.path.isdir(os.path.join(root, 'install'))
    status['last_build_s'] = (os.stat(latest_build).st_mtime
                              if os.path.exists(latest_build) else None)
    status['fingerprint'] = codeFingerprint(repository)
    status['ros_distro'] = builtRosDistro(root)
    usage = shutil.disk_usage(root)
    status['disk'] = {'free_bytes': usage.free, 'total_bytes': usage.total}
    return status


def workspaceRefs(path):
    '''Every branch, tag and remote ref of <path>/src with its commit, so
    Orca can tell whether a push would lose commits that only exist
    here.'''
    repository = os.path.join(os.path.expanduser(path), 'src')
    output = git(repository, 'for-each-ref',
                 '--format=%(objectname) %(refname)') or ''
    refs = {}
    for line in output.splitlines():
        sha, _, name = line.partition(' ')
        refs[name] = sha
    # Older stashes live only in the stash reflog.
    stashes = git(repository, 'stash', 'list', '--format=%H %gd') or ''
    for line in stashes.splitlines():
        sha, _, name = line.partition(' ')
        refs[name] = sha
    # Refs kept as files survive a push (Orca never deletes inside
    # .git); refs only in packed-refs don't, since that file gets replaced.
    git_directory = git(repository, 'rev-parse', '--absolute-git-dir') or ''
    loose = [name for name in refs if name.startswith('stash@')
             or os.path.exists(os.path.join(git_directory, name))]
    status = git(repository, 'status', '--porcelain=v1') or ''
    return {'refs': refs, 'loose': loose, 'changes': [
        line for line in status.splitlines() if line and not isNoise(line)]}


def isNoise(status_line):
    '''Files every run creates (Python caches) shouldn't make two copies of
    the same code look different.'''
    return '__pycache__' in status_line or status_line.endswith('.pyc')


def codeFingerprint(repository):
    '''Hash of the code as it is on disk: the commit, uncommitted changes
    and untracked files, in the repo and its submodules. Two workspaces with
    the same fingerprint run the same code, whatever their history.'''
    digest = hashlib.sha256()
    commands = [
        ['rev-parse', 'HEAD'],
        ['status', '--porcelain=v1', '--untracked-files=all'],
        ['diff', 'HEAD'],
        ['submodule', 'status', '--recursive'],
        ['submodule', 'foreach', '--quiet', '--recursive',
         'git diff HEAD; git status --porcelain=v1 --untracked-files=all'],
    ]
    for arguments in commands:
        output = runCommand(['git', '-C', repository] + arguments,
                            timeout_s=30) or ''
        if 'status' in arguments or 'foreach' in arguments:
            output = '\n'.join(line for line in output.splitlines()
                               if not isNoise(line))
        digest.update(output.encode())
    # Untracked files count by content, not just by name (a new file that is
    # edited after a deploy must make the copies differ).
    for directory in [repository] + submoduleDirectories(repository):
        digest.update(untrackedContentHashes(directory).encode())
    return digest.hexdigest()[:12]


def submoduleDirectories(repository):
    output = git(repository, 'submodule', 'foreach', '--quiet',
                 '--recursive', 'echo "$toplevel/$sm_path"') or ''
    return [line for line in output.splitlines() if line]


def untrackedContentHashes(directory, depth=0):
    '''"path hash" for every untracked file. A nested repository that
    isn't a submodule (git lists it as "name/") is summarized the same way
    as the main one.'''
    listing = git(directory, 'ls-files', '--others', '--exclude-standard')
    entries = [path for path in (listing or '').splitlines()
               if path and not isNoise(path)]
    nested = [path for path in entries if path.endswith('/')]
    paths = [path for path in entries if not path.endswith('/')
             and os.path.isfile(os.path.join(directory, path))]
    summaries = []
    for path in nested:
        if depth < 3:
            summaries.append('%s {%s}' % (path, nestedRepositorySummary(
                os.path.join(directory, path), depth + 1)))
    if not paths:
        return '\n'.join(summaries)
    try:
        completed = subprocess.run(
            ['git', '-C', directory, 'hash-object', '--stdin-paths'],
            input='\n'.join(paths) + '\n', capture_output=True, text=True,
            timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ''
    hashes = ['%s %s' % pair for pair in
              zip(paths, completed.stdout.splitlines())]
    return '\n'.join(summaries + hashes)


def nestedRepositorySummary(directory, depth):
    parts = [git(directory, 'rev-parse', 'HEAD') or 'no commits',
             git(directory, 'diff', 'HEAD') or '',
             untrackedContentHashes(directory, depth)]
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()[:12]


def builtRosDistro(root):
    '''The ROS distro the workspace was built against (colcon records the
    underlay in install/setup.sh).'''
    setup = readText(os.path.join(root, 'install', 'setup.sh')) or ''
    match = re.search(r'/opt/ros/([A-Za-z]+)', setup)
    return match.group(1) if match else None


def installedRosDistros():
    try:
        return sorted(os.listdir('/opt/ros'))
    except OSError:
        return []



# --- Setup checklist ------------------------------------------------------------

# One-time setup a machine needs, checked without sudo. Orca
# offers a fix for most items (a script run in a terminal, where sudo can ask
# for a password).

EEE_DRIVERS = ('r8152',)  # Realtek RTL8153/8156 USB Ethernet
EEE_UDEV_RULE = '/etc/udev/rules.d/70-orca-eee.rules'
# Installed before the rename to Orca; does the same.
OLD_EEE_UDEV_RULE = '/etc/udev/rules.d/70-orchestrator-eee.rules'
CAN_COMMANDS = ('/usr/bin/slcand', 'link set can0 up')
VM_COMMANDS = ('/opt/homebrew/bin/qemu-system-aarch64',
               '/opt/homebrew/bin/socat')
# What run-vm.sh runs as root: QEMU, its monitor (socat), force stop (kill)
# and committing an overlay root made (qemu-img).
VM_SUDO_COMMANDS = VM_COMMANDS + ('/bin/kill', '/opt/homebrew/bin/qemu-img')


def rebootCommand():
    '''The command that restarts this machine (run as root).'''
    if IS_MACOS:
        return ['/sbin/shutdown', '-r', 'now']
    return [shutil.which('systemctl') or '/usr/bin/systemctl', 'reboot']


def rebootMachine(check_only=False):
    '''Restart this machine now, without a password. {"needs_password":
    true, "command"} when sudo would ask for one (Orca then runs the
    command in a terminal instead); check_only just says which.'''
    argv = rebootCommand()
    is_root = os.geteuid() == 0
    if not is_root and not sudoAllows(argv):
        return {'ok': False, 'needs_password': True,
                'command': ['sudo'] + argv}
    if check_only:
        return {'ok': True}
    # Detached, and a moment from now, so this answer reaches Orca first.
    subprocess.Popen(['/bin/sh', '-c', 'sleep 1; exec "$@"', 'reboot']
                     + (argv if is_root else ['sudo', '-n'] + argv),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return {'ok': True}


def sudoAllows(argv):
    """True if sudo runs this command without asking for a password."""
    completed = subprocess.run(['sudo', '-n', '-l'] + list(argv),
                               capture_output=True, text=True)
    return completed.returncode == 0


def eeeInterfaces():
    """USB Ethernet adapters prone to dropping packets with EEE, and
    whether EEE is on for each."""
    found = []
    for path in sorted(glob.glob('/sys/class/net/*/device/driver')):
        name = path.split('/')[4]
        driver = os.path.basename(os.path.realpath(path))
        if driver not in EEE_DRIVERS:
            continue
        output = runCommand(['ethtool', '--show-eee', name]) or \
            runCommand(['/usr/sbin/ethtool', '--show-eee', name]) or ''
        match = re.search(r'EEE status:\s*(\w+)', output)
        found.append({'name': name, 'driver': driver,
                      'eee_on': bool(match) and match.group(1) == 'enabled'})
    return found


def setupChecks(options):
    """[{id, title, state: ok|todo|optional, detail}] for this machine.
    options: workspace, csm_vm_path, tags (can, sharing), is_local,
    share_helper_hash."""
    checks = []
    tags = set(options.get('tags') or [])

    def check(check_id, title, state, detail):
        checks.append({'id': check_id, 'title': title, 'state': state,
                       'detail': detail})

    user = getpass.getuser()
    if not options.get('is_local'):
        allowed = os.geteuid() == 0 or sudoAllows(rebootCommand())
        check('reboot-sudo', 'Restart without a password',
              'ok' if allowed else 'optional',
              'Restart on its card restarts it in one click.' if allowed
              else 'Restart on its card opens a terminal to type the '
              'sudo password in.')
    if not IS_MACOS:
        if not options.get('is_local'):
            linger = (runCommand(['loginctl', 'show-user', user, '-p',
                                  'Linger', '--value']) or '').strip()
            check('linger', 'Lingering', 'ok' if linger == 'yes' else 'todo',
                  'Processes Orca starts keep running when SSH '
                  'sessions end.' if linger == 'yes' else
                  'Off: systemd may stop processes started over SSH when the '
                  'session ends.')
        if options.get('workspace'):
            has_rsync = shutil.which('rsync') is not None
            check('rsync', 'rsync', 'ok' if has_rsync else 'todo',
                  'Deploying code uses it.' if has_rsync
                  else 'Missing: the Deploy page needs it.')
        avahi = (runCommand(['systemctl', 'is-active', 'avahi-daemon'])
                 or '').strip() == 'active'
        hostname = socket.gethostname().split('.')[0]
        check('avahi', 'mDNS name', 'ok' if avahi else 'optional',
              'Answers to %s.local.' % hostname if avahi else
              'avahi-daemon isn\'t running: %s.local doesn\'t resolve '
              '(Orca finds it anyway).' % hostname)
        adapters = eeeInterfaces()
        if adapters:
            has_rule = os.path.exists(EEE_UDEV_RULE) \
                or os.path.exists(OLD_EEE_UDEV_RULE)
            on = [adapter['name'] for adapter in adapters
                  if adapter['eee_on']]
            ok = has_rule and not on
            check('eee', 'USB Ethernet power saving',
                  'ok' if ok else 'todo',
                  'Energy Efficient Ethernet stays off on %s.' % ', '.join(
                      adapter['name'] for adapter in adapters) if ok else
                  'On for %s%s: RTL8153 adapters drop packets with it.' % (
                      ', '.join(on) or 'adapters plugged in later',
                      '' if has_rule else ', and nothing turns it off'))
        if 'can' in tags:
            ip = shutil.which('ip') or '/usr/sbin/ip'
            allowed = sudoAllows([CAN_COMMANDS[0]]) and sudoAllows(
                [ip] + CAN_COMMANDS[1].split())
            check('can-sudo', 'CAN without a password',
                  'ok' if allowed else 'todo',
                  'run.sh --canbus can bring up CAN.' if allowed else
                  'sudo asks for a password for slcand / ip link, so '
                  'run.sh --canbus fails when started from Orca.')
        if 'sharing' in tags or options.get('is_local'):
            helper_hash = fileHash(SHARE_HELPER_PATH)
            current = helper_hash and helper_hash == options.get(
                'share_helper_hash') and shareSudoAllowed(helper_hash)
            check('sharing', 'Internet sharing helper',
                  'ok' if current else ('todo' if 'sharing' in tags
                                        else 'optional'),
                  'Installed: sharing starts without a password.' if current
                  else 'Out of date.' if helper_hash else
                  'Not installed (needed to share internet over Ethernet).')
    else:
        vm_path = options.get('csm_vm_path')
        if vm_path:
            script = os.path.join(os.path.expanduser(vm_path), 'run-vm.sh')
            text = readText(script)
            if text is None:
                check('csm-vm', 'csm-vm', 'todo',
                      'No run-vm.sh in %s.' % vm_path)
            else:
                current = '--overlay' in text and 'SERIAL_SOCKET' in text
                safe_stop = 'STOP_TIMEOUT_S' in text
                check('csm-vm', 'csm-vm run-vm.sh',
                      'ok' if current and safe_stop else 'todo',
                      'Supports --overlay, the serial console and a stop '
                      'timeout.' if current and safe_stop
                      else 'An older run-vm.sh: Shut down can wait forever '
                      'for a hung VM, and Force stop can\'t kill it.'
                      if current else 'An older run-vm.sh: starting the VM '
                      'from the orca can stop at a question, and there is '
                      'no serial console.')
            tools = [path for path in VM_COMMANDS if not os.path.exists(path)]
            check('vm-tools', 'QEMU and socat', 'ok' if not tools else 'todo',
                  'Installed with Homebrew.' if not tools else
                  'Missing %s (brew install qemu socat).' % ', '.join(tools))
            allowed = all(sudoAllows([path]) for path in VM_SUDO_COMMANDS)
            check('vm-sudo', 'VM control without a password',
                  'ok' if allowed else 'todo',
                  'run-vm.sh can start, stop and force-stop the VM.'
                  if allowed else 'sudo asks for a password for QEMU, '
                  'socat, kill or qemu-img, so starting, stopping or '
                  'force-stopping the VM from Orca fails.')
    if options.get('workspace'):
        distros = installedRosDistros()
        check('ros', 'ROS 2', 'ok' if distros else 'todo',
              'Installed: %s.' % ', '.join(distros) if distros else
              'No ROS under /opt/ros: install it to build the workspace.')
        root = os.path.expanduser(options['workspace'])
        built = os.path.exists(os.path.join(root, 'install', 'setup.sh'))
        exists = os.path.isdir(os.path.join(root, 'src'))
        check('workspace', 'Workspace', 'ok' if built else 'todo',
              '%s is built.' % options['workspace'] if built else
              '%s %s: deploy and build it from the Deploy page.' % (
                  options['workspace'],
                  'is not built' if exists else 'does not exist'))
    return checks



# --- Surveying a new machine ----------------------------------------------------

# What the add-machine wizard can prefill: workspaces and csm-vm folders near
# the home directory, and VMs this machine hosts (seen on its host-only
# bridge).

def homeRelative(path):
    home = os.path.expanduser('~')
    relative = os.path.relpath(path, home)
    return '~' if relative == '.' else '~/' + relative


def findWorkspaces():
    home = os.path.expanduser('~')
    found = []
    for pattern in ('*/src/build.sh', '*/*/src/build.sh'):
        for path in sorted(glob.glob(os.path.join(home, pattern))):
            workspace = os.path.dirname(os.path.dirname(path))
            if os.path.isfile(os.path.join(workspace, 'src', 'run.sh')):
                found.append(homeRelative(workspace))
    return list(dict.fromkeys(found))


def findCsmVm():
    home = os.path.expanduser('~')
    found = []
    for pattern in ('*/run-vm.sh', '*/*/run-vm.sh'):
        for path in sorted(glob.glob(os.path.join(home, pattern))):
            text = readText(path) or ''
            found.append({'path': homeRelative(os.path.dirname(path)),
                          'has_overlay_option': '--overlay' in text})
    return found


def vmGuests(interfaces):
    """Addresses seen on this machine's bridge interfaces (e.g. the
    host-only link to a csm-vm VM), other than its own."""
    bridges = []
    own = set()
    for interface in interfaces:
        for cidr in interface.get('ipv4') or []:
            address = ipaddress.ip_interface(cidr)
            own.add(str(address.ip))
            if interface['kind'] == 'bridge':
                bridges.append((interface['name'], address.network))
    guests = []
    for address, mac in neighbourTable().items():
        ip = ipaddress.ip_address(address)
        for name, network in bridges:
            if ip in network and address not in own \
                    and ip != network.broadcast_address:
                guests.append({'address': address, 'mac': mac,
                               'interface': name})
    return guests


def machineSurvey(interfaces):
    return {
        'workspaces': findWorkspaces(),
        'ros_distros': installedRosDistros(),
        'csm_vm': findCsmVm(),
        'vm_guests': vmGuests(interfaces),
    }



# --- Internet sharing ---------------------------------------------------------

# Sharing this machine's internet over an Ethernet port goes through a small
# root-owned helper (share_helper.py, installed by Orca with a
# sudoers rule), so it works without a password but can't do anything else.
SHARE_HELPER_PATH = '/usr/local/sbin/orca-share'
SHARE_SUDO_CHECK_S = 60.0
SHARE_COMMAND_TIMEOUT_S = 40.0
share_sudo_check = {'checked_s': 0.0, 'allowed': False, 'hash': None}


def shareSudoAllowed(helper_hash):
    """True if sudo runs the helper without a password (checked once a
    minute, and again when the helper changes)."""
    check = share_sudo_check
    if check['hash'] == helper_hash \
            and time.time() - check['checked_s'] < SHARE_SUDO_CHECK_S:
        return check['allowed']
    completed = subprocess.run(['sudo', '-n', '-l', SHARE_HELPER_PATH],
                               capture_output=True, text=True)
    check.update(checked_s=time.time(), allowed=completed.returncode == 0,
                 hash=helper_hash)
    return check['allowed']


def shareNeighbours(interface):
    """Devices seen on one port: [{address, mac, state}]."""
    output = runCommand(['ip', '-j', '-4', 'neigh', 'show', 'dev',
                         interface]) or '[]'
    try:
        entries = json.loads(output)
    except ValueError:
        return []
    return [{'address': entry['dst'], 'mac': entry['lladdr'],
             'state': (entry.get('state') or [''])[0].lower()}
            for entry in entries if entry.get('lladdr')]


def shareStatus(expected_hash):
    if IS_MACOS:
        return {'supported': False,
                'reason': 'sharing is only available on Linux machines'}
    helper_hash = fileHash(SHARE_HELPER_PATH)
    sessions = []
    if helper_hash:
        output = runCommand([sys.executable, SHARE_HELPER_PATH, 'status'])
        try:
            sessions = json.loads(output or '{}').get('sessions', [])
        except ValueError:
            sessions = []
    for session in sessions:
        session['neighbours'] = shareNeighbours(session['downstream'])
    route = defaultRoute() or {}
    ports = []
    for interface in linuxInterfaces():
        if interface['kind'] not in ('ethernet', 'wifi'):
            continue
        ports.append({
            'name': interface['name'], 'kind': interface['kind'],
            'mac': interface['mac'], 'ipv4': interface['ipv4'],
            'is_up': interface['is_up'],
            'carrier': (readText('/sys/class/net/%s/carrier'
                                 % interface['name']) or '').strip() == '1',
            'is_default_route': interface['name'] == route.get('interface'),
        })
    return {
        'supported': True,
        'helper_installed': helper_hash is not None,
        'helper_hash': helper_hash,
        'helper_current': helper_hash == expected_hash,
        'sudo_ok': bool(helper_hash) and shareSudoAllowed(helper_hash),
        'dnsmasq_installed': shutil.which('dnsmasq') is not None
        or os.path.exists('/usr/sbin/dnsmasq'),
        'default_route': route,
        'ports': ports,
        'sessions': sessions,
    }


def runShareHelper(arguments):
    try:
        completed = subprocess.run(
            ['sudo', '-n', SHARE_HELPER_PATH] + arguments,
            capture_output=True, text=True, timeout=SHARE_COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError('the sharing helper took too long')
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        message = (completed.stderr or completed.stdout).strip()
        if 'password is required' in message:
            message = ('sudo asked for a password: install the sharing '
                       'helper first')
        raise RuntimeError(message or 'the sharing helper failed')
    if not result.get('ok'):
        raise RuntimeError(result.get('error') or 'sharing failed')
    return result


def shareUp(params):
    arguments = ['up', '--name', str(params['name']),
                 '--downstream', str(params['downstream'])]
    for key in ('upstream', 'subnet', 'dns', 'lease'):
        if params.get(key):
            arguments += ['--' + key, str(params[key])]
    if params.get('keep_eee'):
        arguments.append('--keep-eee')
    return runShareHelper(arguments)


def shareDown(params):
    return runShareHelper(['down', '--name', str(params['name'])])



# --- Network tools ------------------------------------------------------------



MAX_SCAN_HOSTS = 1024
SCAN_CONCURRENCY = 200
NAME_LOOKUP_TIMEOUT_S = 1.0


def ping(host, count):
    '''Round trips to a host with the system ping (ICMP needs no root
    there).'''
    wait = ['-W', '1000'] if IS_MACOS else ['-W', '1']
    try:
        completed = subprocess.run(
            ['ping', '-n', '-c', str(count)] + wait + [host],
            capture_output=True, text=True, timeout=count * 2 + 5)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'host': host, 'error': str(error)}
    output = completed.stdout + completed.stderr
    loss = re.search(r'([\d.]+)% packet loss', output)
    times = re.search(r'= ([\d.]+)/([\d.]+)/([\d.]+)', output)
    return {
        'host': host,
        'sent': count,
        'loss_percent': float(loss.group(1)) if loss else 100.0,
        'min_ms': float(times.group(1)) if times else None,
        'average_ms': float(times.group(2)) if times else None,
        'max_ms': float(times.group(3)) if times else None,
        'error': None if loss else output.strip().splitlines()[-1:],
    }


async def tcpProbe(host, port, timeout_s):
    '''"open", "closed" (refused: the host is up), or "no answer".'''
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout_s)
    except ConnectionRefusedError:
        return 'closed', (time.monotonic() - started) * 1000.0
    except (asyncio.TimeoutError, OSError):
        return 'no answer', None
    elapsed_ms = (time.monotonic() - started) * 1000.0
    writer.close()
    return 'open', elapsed_ms


def normalMac(mac):
    '''aa:b:3:dd:e:ff -> aa:0b:03:dd:0e:ff (macOS's arp drops zeros).'''
    return ':'.join(part.zfill(2) for part in mac.lower().split(':'))


def neighbourTable():
    '''{ip: mac} from the ARP / neighbour table.'''
    table = {}
    if IS_MACOS:
        output = runCommand(['arp', '-an']) or ''
        for ip, mac in re.findall(r'\(([\d.]+)\) at ([0-9a-f:]+)', output):
            table[ip] = normalMac(mac)
    else:
        output = runCommand(['ip', 'neigh', 'show']) or ''
        for ip, mac in re.findall(r'^([\d.]+) .*lladdr ([0-9a-f:]+)', output,
                                  re.MULTILINE):
            table[ip] = mac
    return table


async def lookupName(address):
    loop = asyncio.get_running_loop()
    try:
        name, _, _ = await asyncio.wait_for(
            loop.run_in_executor(None, socket.gethostbyaddr, address),
            NAME_LOOKUP_TIMEOUT_S)
        return name
    except (asyncio.TimeoutError, OSError):
        return None


async def scanNetwork(cidr, ports, timeout_s):
    '''Hosts in a network that answer on any of the ports (a refused
    connection counts: something is there).'''
    network = ipaddress.ip_network(cidr, strict=False)
    if network.num_addresses > MAX_SCAN_HOSTS + 2:
        raise ValueError('scan at most %d addresses (a /22)' % MAX_SCAN_HOSTS)
    limit = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def probe(address, port):
        async with limit:
            return address, port, await tcpProbe(address, port, timeout_s)

    started = time.time()
    results = await asyncio.gather(*(
        probe(str(address), port) for address in network.hosts()
        for port in ports))
    hosts = {}
    for address, port, (state, elapsed_ms) in results:
        if state == 'no answer':
            continue
        host = hosts.setdefault(address, {'address': address, 'ports': {},
                                          'rtt_ms': elapsed_ms})
        host['ports'][str(port)] = state
        host['rtt_ms'] = min(host['rtt_ms'], elapsed_ms)
    neighbours = await asyncio.get_running_loop().run_in_executor(
        None, neighbourTable)
    # Hosts that answered ARP but none of the ports (e.g. a firewall that
    # drops everything) were still there.
    for address, mac in neighbours.items():
        if address not in hosts and ipaddress.ip_address(address) in network:
            hosts[address] = {'address': address, 'ports': {},
                              'rtt_ms': None, 'arp_only': True}
    names = await asyncio.gather(*(lookupName(address) for address in hosts))
    for host, name in zip(hosts.values(), names):
        host['name'] = name
        host['mac'] = neighbours.get(host['address'])
    ordered = sorted(hosts.values(),
                     key=lambda host: ipaddress.ip_address(host['address']))
    return {'network': str(network), 'ports': ports, 'hosts': ordered,
            'duration_s': time.time() - started}



# --- Bags ---------------------------------------------------------------------



BAG_DIRECTORY = 'bag_recordings'


def directorySize(path):
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def bagInfo(path):
    '''Summary of one rosbag2 directory from its metadata.yaml (read with
    regular expressions: there is no YAML parser in the standard library).'''
    metadata = readText(os.path.join(path, 'metadata.yaml')) or ''

    def number(pattern):
        match = re.search(pattern, metadata, re.MULTILINE)
        return int(match.group(1)) if match else None

    topics = re.findall(r'^\s+- topic_metadata:\s*\n\s+name:\s*(\S+)',
                        metadata, re.MULTILINE)
    storage = re.search(r'^  storage_identifier:\s*(\S+)', metadata,
                        re.MULTILINE)
    start_ns = number(r'^  starting_time:\s*\n\s+nanoseconds_since_epoch:'
                      r'\s*(\d+)')
    duration_ns = number(r'^  duration:\s*\n\s+nanoseconds:\s*(\d+)')
    return {
        'name': os.path.basename(path),
        'size_bytes': directorySize(path),
        'modified_s': os.stat(path).st_mtime,
        'has_metadata': bool(metadata),
        'storage': storage.group(1) if storage else None,
        'start_s': start_ns / 1e9 if start_ns else None,
        'duration_s': duration_ns / 1e9 if duration_ns is not None else None,
        'message_count': number(r'^  message_count:\s*(\d+)'),
        'topics': topics,
    }


def listBags(workspace):
    root = os.path.expanduser(workspace)
    directory = os.path.join(root, BAG_DIRECTORY)
    bags = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if os.path.isdir(path):
                bags.append(bagInfo(path))
    usage = shutil.disk_usage(root if os.path.isdir(root) else '/')
    return {'directory': directory, 'bags': bags,
            'disk': {'free_bytes': usage.free, 'total_bytes': usage.total}}


def deleteBag(workspace, name):
    if not name or '/' in name or name.startswith('.'):
        raise ValueError('not a bag name: ' + str(name))
    path = os.path.join(os.path.expanduser(workspace), BAG_DIRECTORY, name)
    if not os.path.isdir(path):
        raise ValueError('no bag ' + name)
    shutil.rmtree(path)
    return {'deleted': name}



# --- Logs ---------------------------------------------------------------------



MAX_LOG_READ_BYTES = 20 * 1024 * 1024
ROS_LOG_DIRECTORY = os.path.join(os.path.expanduser('~'), '.ros', 'log')
VM_LOG_PATH = '/var/log/run-vm.log'
NUM_RECENT_ROS_LOGS = 60
NUM_RECENT_BUILDS = 4


def logEntry(path, group, label=None):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {'path': path, 'group': group,
            'label': label or os.path.basename(path),
            'size_bytes': stat.st_size, 'modified_s': stat.st_mtime}


def buildLogs(workspace):
    '''Each package's newest build log. build.sh runs colcon twice (lance on
    its own), so latest_build alone only has lance's.'''
    builds = sorted(glob.glob(os.path.join(
        os.path.expanduser(workspace), 'log', 'build_*')), reverse=True)
    entries = []
    seen = set()
    for build in builds[:NUM_RECENT_BUILDS]:
        for path in glob.glob(os.path.join(build, '*', 'stdout_stderr.log')):
            package = os.path.basename(os.path.dirname(path))
            if package not in seen:
                seen.add(package)
                entries.append(logEntry(path, 'Latest build', package))
    if builds:
        entries.append(logEntry(os.path.join(builds[0], 'events.log'),
                                'Latest build', 'colcon events'))
    return entries


def logRoots(workspace):
    roots = [AGENT_DIR, ROS_LOG_DIRECTORY, VM_LOG_PATH]
    if workspace:
        roots.append(os.path.join(os.path.expanduser(workspace), 'log'))
    return [os.path.realpath(root) for root in roots]


def modifiedTime(path):
    '''mtime, or 0 for a file that just went away (ROS rotates logs).'''
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def listLogs(workspace):
    '''Logs worth looking at on this machine, newest first per group.'''
    entries = []
    for path in glob.glob(os.path.join(RUNS_DIR, '*', '*.log')):
        run = os.path.basename(os.path.dirname(path))
        process = os.path.basename(path)[:-len('.log')]
        if process.startswith(run + '.'):
            process = process[len(run) + 1:]
        entries.append(logEntry(path, 'Orca runs',
                                '%s  %s' % (run, process)))
    if workspace:
        entries.extend(buildLogs(workspace))
    ros_logs = glob.glob(os.path.join(ROS_LOG_DIRECTORY, '*.log')) + glob.glob(
        os.path.join(ROS_LOG_DIRECTORY, '*', 'launch.log'))
    ros_logs.sort(key=modifiedTime, reverse=True)
    for path in ros_logs[:NUM_RECENT_ROS_LOGS]:
        label = os.path.relpath(path, ROS_LOG_DIRECTORY)
        entries.append(logEntry(path, 'ROS', label))
    entries.append(logEntry(LOG_PATH, 'Agent', 'agent.log'))
    if IS_MACOS:
        entries.append(logEntry(VM_LOG_PATH, 'VM', 'run-vm.log (autostart)'))
    entries = [entry for entry in entries if entry]
    entries.sort(key=lambda entry: entry['modified_s'], reverse=True)
    return entries


def readLog(path, max_bytes, workspace):
    '''The end of a log file (at most max_bytes), from the known log
    locations only.'''
    real_path = os.path.realpath(os.path.expanduser(path))
    if not any(real_path == root or real_path.startswith(root + os.sep)
               for root in logRoots(workspace)):
        raise ValueError('not a log location: ' + path)
    max_bytes = min(int(max_bytes), MAX_LOG_READ_BYTES)
    size = os.path.getsize(real_path)
    with open(real_path, 'rb') as file:
        if size > max_bytes:
            file.seek(size - max_bytes)
            file.readline()  # drop the partial first line
        data = file.read()
    return {'path': path, 'size_bytes': size, 'truncated': size > max_bytes,
            'text': data.decode('utf-8', errors='replace')}



def deleteLog(path, workspace, in_use):
    '''Delete one log file from the known log locations (not the agent's
    own log, which is open, nor a running process's), then any folders it
    leaves empty up to the log root.'''
    real_path = os.path.realpath(os.path.expanduser(path))
    if real_path in (os.path.realpath(LOG_PATH), os.path.realpath(VM_LOG_PATH)):
        raise ValueError("this log is in use and can't be deleted")
    # Not every log root: ~/.orca also holds the agent itself.
    roots = [os.path.realpath(RUNS_DIR), os.path.realpath(ROS_LOG_DIRECTORY)]
    if workspace:
        roots.append(os.path.realpath(os.path.join(
            os.path.expanduser(workspace), 'log')))
    inside = [root for root in roots if real_path.startswith(root + os.sep)]
    is_old_agent_log = real_path == os.path.realpath(LOG_PATH + '.1')
    if not (inside or is_old_agent_log) or not os.path.isfile(real_path):
        raise ValueError('not a log Orca can delete: ' + path)
    if real_path in in_use:
        raise ValueError('its process is still running')
    size = os.path.getsize(real_path)
    os.unlink(real_path)
    if inside:
        removeEmptyParents(os.path.dirname(real_path), inside[0])
    return {'deleted': path, 'bytes': size}


def removeEmptyParents(directory, root):
    while directory.startswith(root + os.sep):
        try:
            os.rmdir(directory)  # only succeeds when empty
        except OSError:
            return
        directory = os.path.dirname(directory)



# --- Cleanup ------------------------------------------------------------------



# What builds up on a machine over time, for the Logs page's cleanup panel.
# Each category lists paths that can go; anything a running process uses is
# left alone.
OLD_AGENT_DIR = os.path.join(os.path.expanduser('~'), '.orchestrator')
RECENT_FILE_S = 10 * 60
NUM_KEPT_BUILDS = NUM_RECENT_BUILDS


def pathSize(path):
    '''Bytes in a file or folder (not following links).'''
    if os.path.islink(path):
        return 0
    if not os.path.isdir(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def removePath(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def isLocked(lock_path):
    '''True if a daemon holds this lock file (an agent still running).'''
    try:
        with open(lock_path, 'a') as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(file, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def overlayFiles(overlay_directory, in_use):
    '''Launch overlays Orca wrote that no running process uses (they're
    removed when their process exits; these are from crashes and older
    Orca versions), and half-written temporary files.'''
    if not overlay_directory:
        return []
    directory = os.path.expanduser(overlay_directory)
    found = glob.glob(os.path.join(directory, 'orca-*.json')) + glob.glob(
        os.path.join(directory, '*.tmp')) + glob.glob(
        os.path.join(AGENT_DIR, '*.tmp'))
    return [path for path in found if os.path.realpath(path) not in in_use]


def runLogDirectories(in_use):
    '''Orca run logs whose processes have all ended.'''
    found = []
    for path in glob.glob(os.path.join(RUNS_DIR, '*')):
        real = os.path.realpath(path)
        if os.path.isdir(path) and not any(
                used.startswith(real + os.sep) for used in in_use):
            found.append(path)
    return found


def rosLogEntries():
    '''Everything in ~/.ros/log except what was written in the last few
    minutes (it may belong to something running).'''
    cutoff_s = time.time() - RECENT_FILE_S
    return [path for path in glob.glob(os.path.join(ROS_LOG_DIRECTORY, '*'))
            if not os.path.islink(path) and modifiedTime(path) < cutoff_s]


def oldBuildLogs(workspace):
    '''colcon's log/build_* folders, all but the newest few (the Logs page
    reads each package's newest log from those).'''
    if not workspace:
        return []
    builds = sorted(glob.glob(os.path.join(
        os.path.expanduser(workspace), 'log', 'build_*')), reverse=True)
    return [path for path in builds[NUM_KEPT_BUILDS:]
            if os.path.isdir(path) and not os.path.islink(path)]


def oldAgentFolder():
    '''~/.orchestrator, from before Orca was renamed (old run logs and the
    old agent), unless an old agent daemon is still running there.'''
    if not os.path.isdir(OLD_AGENT_DIR) \
            or os.path.realpath(OLD_AGENT_DIR) == os.path.realpath(AGENT_DIR):
        return []
    if isLocked(os.path.join(OLD_AGENT_DIR, 'daemon.lock')):
        return []
    return [OLD_AGENT_DIR]


CLEANUP_CATEGORIES = [
    ('overlays', 'Launch overlays',
     'Config overlays from launches that have ended, and half-written files.'),
    ('run_logs', 'Orca run logs',
     'Output of runs that have ended (deleted after 14 days anyway).'),
    ('ros_logs', 'ROS logs',
     '~/.ros/log, except files written in the last 10 minutes.'),
    ('build_logs', 'Old build logs',
     'colcon build logs, except the newest %d builds.' % NUM_KEPT_BUILDS),
    ('old_agent', 'Old agent folder',
     '~/.orchestrator, left from before Orca was renamed.'),
]


def cleanupPaths(category, params, in_use):
    workspace = params.get('workspace')
    if category == 'overlays':
        return overlayFiles(params.get('overlay_directory'), in_use)
    if category == 'run_logs':
        return runLogDirectories(in_use)
    if category == 'ros_logs':
        return rosLogEntries()
    if category == 'build_logs':
        return oldBuildLogs(workspace)
    if category == 'old_agent':
        return oldAgentFolder()
    raise ValueError('nothing called %s to clean up' % category)


def cleanupScan(params, in_use):
    categories = []
    for category, label, description in CLEANUP_CATEGORIES:
        paths = cleanupPaths(category, params, in_use)
        categories.append({
            'id': category, 'label': label, 'description': description,
            'count': len(paths), 'bytes': sum(map(pathSize, paths))})
    root = os.path.expanduser(params.get('workspace') or '~')
    usage = shutil.disk_usage(root if os.path.isdir(root) else '/')
    return {'categories': categories,
            'disk': {'free_bytes': usage.free, 'total_bytes': usage.total}}


def cleanup(params, in_use):
    '''Delete what the given categories list now (looked up again, so
    nothing that started being used since the scan goes).'''
    removed = {}
    errors = []
    for category in params.get('categories') or []:
        count = 0
        size = 0
        for path in cleanupPaths(category, params, in_use):
            path_size = pathSize(path)
            try:
                removePath(path)
            except OSError as error:
                errors.append('%s: %s' % (path, error.strerror or error))
                continue
            count += 1
            size += path_size
        removed[category] = {'count': count, 'bytes': size}
    return {'removed': removed, 'errors': errors[:20]}


# --- Virtual machine (macOS host) ---------------------------------------------



def qemuProcesses():
    output = runCommand(['ps', '-axww', '-o', 'pid=,etime=,user=,command='])
    processes = []
    for line in (output or '').splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4:
            continue
        pid, elapsed, user, command = fields
        # `sudo qemu-system-aarch64 ...` wrappers aren't the VM itself.
        if command.split(None, 1)[0].endswith('qemu-system-aarch64'):
            processes.append({'pid': int(pid), 'user': user,
                              'uptime_s': parseElapsedTime(elapsed),
                              'args': command.split()})
    return processes


def describeQemuArgs(args):
    '''Pull the interesting settings out of a QEMU command line.'''
    description = {'usb_devices': [], 'networks': [], 'cpus': None,
                   'memory': None, 'monitor_socket': None,
                   'serial_socket': None}
    for flag, value in zip(args, args[1:]):
        if flag == '-device' and value.startswith('usb-host'):
            vendor = re.search(r'vendorid=(0x[0-9a-fA-F]+)', value)
            product = re.search(r'productid=(0x[0-9a-fA-F]+)', value)
            if vendor and product:
                description['usb_devices'].append(
                    vendor.group(1) + ':' + product.group(1))
        elif flag == '-netdev':
            kind = value.split(',', 1)[0]
            interface = re.search(r'ifname=([^,]+)', value)
            description['networks'].append(
                {'type': kind,
                 'interface': interface.group(1) if interface else None})
        elif flag == '-smp':
            cpus = re.search(r'cpus=(\d+)', value)
            description['cpus'] = int(cpus.group(1)) if cpus else None
        elif flag == '-m':
            description['memory'] = value
        elif flag == '-monitor' and value.startswith('unix:'):
            description['monitor_socket'] = value[5:].split(',', 1)[0]
        elif flag == '-serial' and value.startswith('unix:'):
            description['serial_socket'] = value[5:].split(',', 1)[0]
    return description


def fileSize(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def autostartDaemons():
    '''LaunchDaemons that start the VM (any plist that mentions run-vm.sh).'''
    daemons = []
    for path in glob.glob('/Library/LaunchDaemons/*.plist'):
        try:
            with open(path, 'rb') as file:
                plist = plistlib.load(file)
        except Exception:
            continue
        arguments = plist.get('ProgramArguments') or []
        if any('run-vm.sh' in str(argument) for argument in arguments):
            daemons.append({'label': plist.get('Label'), 'path': path,
                            'arguments': arguments,
                            'run_at_load': bool(plist.get('RunAtLoad'))})
    return daemons


def repositoryStatus(path):
    '''Edits in a git checkout that exist only on this machine (the csm-vm
    scripts): uncommitted files, commits the current branch hasn't pushed,
    and commits on any local branch that no remote branch has. Compared
    with the remotes as of the last fetch: nothing here goes to the
    network. None when path isn't a git checkout.'''
    root = os.path.expanduser(path)
    if git(root, 'rev-parse', 'HEAD') is None:
        return None
    # Not git(): stripping would eat the first line's status column.
    porcelain = [line for line in (runCommand(
        ['git', '-C', root, 'status', '--porcelain']) or '').splitlines()
        if line]

    def count(*args):
        output = git(root, 'rev-list', '--count', *args)
        return int(output) if output and output.isdigit() else None

    upstream = git(root, 'rev-parse', '--abbrev-ref',
                   '--symbolic-full-name', '@{u}')
    return {
        'branch': git(root, 'rev-parse', '--abbrev-ref', 'HEAD'),
        'upstream': upstream,
        'num_changes': len(porcelain),
        'changed_files': [line[3:] for line in porcelain[:8]],
        # The current branch against its upstream; without one, its commits
        # that no remote has.
        'num_ahead': count('@{u}..HEAD') if upstream
        else count('HEAD', '--not', '--remotes'),
        'num_behind': count('HEAD..@{u}') if upstream else None,
        # Every local branch: work left on a branch that isn't checked out.
        'num_unpushed': count('--branches', '--not', '--remotes'),
    }


def overlayStatus(path):
    '''Who made the VM's overlay and whether it's safe to commit: one a
    force stop left behind is marked "<overlay>.unclean" by run-vm.sh, and
    one root made (an old autostart) needs root to commit.'''
    try:
        info = os.stat(path)
    except OSError:
        return None
    try:
        import pwd
        owner = pwd.getpwuid(info.st_uid).pw_name
    except (ImportError, KeyError):
        owner = str(info.st_uid)
    return {
        'owner': owner,
        'is_writable': os.access(path, os.W_OK),
        'modified_s': info.st_mtime,
        'is_unclean': os.path.exists(path + '.unclean'),
    }


def vmStatus(csm_vm_path):
    root = os.path.expanduser(csm_vm_path)
    status = {'csm_vm_path': csm_vm_path,
              'script_found': os.path.isfile(os.path.join(root, 'run-vm.sh'))}
    processes = qemuProcesses()
    status['is_running'] = bool(processes)
    if processes:
        process = processes[0]
        status.update({'pid': process['pid'], 'user': process['user'],
                       'uptime_s': process['uptime_s']})
        status.update(describeQemuArgs(process['args']))
    status['disks'] = {
        'base_bytes': fileSize(os.path.join(root, 'ubuntu-aarch64-base.qcow2')),
        'overlay_bytes': fileSize(
            os.path.join(root, 'ubuntu-aarch64-overlay.qcow2')),
        'backup_bytes': fileSize(
            os.path.join(root, 'ubuntu-aarch64-base.backup.qcow2')),
    }
    status['overlay'] = overlayStatus(
        os.path.join(root, 'ubuntu-aarch64-overlay.qcow2'))
    status['autostart'] = autostartDaemons()
    status['repo'] = repositoryStatus(csm_vm_path)
    return status



# --- Supervised processes -----------------------------------------------------



CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]')
# Terminal escapes other than colours: OSC sequences (GCC's hyperlinks) and
# cursor/erase commands (CMake's "\x1b[K").
OSC_SEQUENCE = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
NON_COLOUR_CSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-ln-z@`]')


def cleanLine(text):
    '''Keep colour codes (the web app renders them) but drop other control
    characters. A carriage return mid-line (progress bars) keeps only the
    last segment, like a terminal would show.'''
    text = text.rstrip('\r')
    if '\r' in text:
        text = text.rsplit('\r', 1)[1]
    text = OSC_SEQUENCE.sub('', text)
    text = NON_COLOUR_CSI.sub('', text)
    text = CONTROL_CHARACTERS.sub('', text)
    return text[:MAX_LINE_CHARACTERS]


def removeOldRunLogs():
    '''Delete run log directories older than RUN_LOG_MAX_AGE_S.'''
    cutoff_s = time.time() - RUN_LOG_MAX_AGE_S
    for path in glob.glob(os.path.join(RUNS_DIR, '*')):
        try:
            if os.path.isdir(path) and os.stat(path).st_mtime < cutoff_s:
                for name in os.listdir(path):
                    os.unlink(os.path.join(path, name))
                os.rmdir(path)
        except OSError:
            pass


class SupervisedProcess:
    '''One process started for Orca. Runs on a pseudo-terminal so
    programs line-buffer and colour their output as they would in a terminal,
    in its own process group so stopping it reaches every child (like Ctrl-C).

    The reader and waiter threads hand everything to the event loop through
    call_soon_threadsafe; all state below is owned by the loop thread.'''

    def __init__(self, manager, params):
        self.manager = manager
        self.process_id = params['process_id']
        self.label = params.get('label') or self.process_id
        self.argv = params['argv']
        self.cwd = os.path.expanduser(params.get('cwd') or '~')
        self.meta = params.get('meta') or {}
        self.extra_env = params.get('env') or {}
        # Files written for this process only (its launch overlay).
        self.remove_on_exit = [os.path.expanduser(path) for path
                               in params.get('remove_on_exit') or []]

        # --- Lifecycle ---
        self.popen = None
        self.state = 'starting'  # starting, running, stopping, exited
        self.started_s = None
        self.ended_s = None
        self.exit_code = None
        self.exit_signal = None
        self.stop_requested_s = None
        self.stop_task = None

        # --- Output ---
        self.lines = collections.deque(maxlen=MAX_BUFFERED_LINES)
        self.next_line_number = 0  # number the next complete line gets
        self.partial = ''
        self.partial_since_s = None
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending_lines = []  # not yet sent to watchers
        self.flush_handle = None
        run_directory = os.path.join(
            RUNS_DIR, re.sub(r'[^\w.-]', '_', self.meta.get('run_id', 'misc')))
        os.makedirs(run_directory, exist_ok=True)
        self.log_path = os.path.join(
            run_directory, re.sub(r'[^\w.-]', '_', self.process_id) + '.log')
        self.log_file = None
        self.log_bytes = 0

    def record(self):
        return {
            'process_id': self.process_id,
            'label': self.label,
            'argv': self.argv,
            'cwd': self.cwd,
            'meta': self.meta,
            'pid': self.popen.pid if self.popen else None,
            'state': self.state,
            'started_s': self.started_s,
            'ended_s': self.ended_s,
            'exit_code': self.exit_code,
            'exit_signal': self.exit_signal,
            'num_lines': self.next_line_number,
            'log_path': self.log_path,
        }

    def isRunning(self):
        return self.state in ('starting', 'running', 'stopping')

    # --- Starting ---

    def environment(self):
        environment = dict(os.environ)
        environment.update({
            'PYTHONUNBUFFERED': '1',
            'RCUTILS_COLORIZED_OUTPUT': '1',
            'RCUTILS_LOGGING_BUFFERED_STREAM': '0',
            'TERM': 'xterm-256color',
            'COLUMNS': str(PTY_COLUMNS),
        })
        environment.update(
            {str(key): str(value) for key, value in self.extra_env.items()})
        return environment

    def start(self, loop):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack('HHHH', PTY_ROWS, PTY_COLUMNS, 0, 0))
        self.log_file = open(self.log_path, 'ab')
        try:
            self.popen = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.environment(),
                stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True)
        except OSError as error:
            os.close(master)
            os.close(slave)
            self.log_file.close()
            raise RuntimeError('could not start %s: %s'
                               % (self.argv[0], error))
        os.close(slave)  # the child holds its own copies
        self.state = 'running'
        self.started_s = time.time()
        self.writeLog(('$ %s  (in %s)\n' % (
            ' '.join(self.argv), self.cwd)).encode())
        self.reader = threading.Thread(target=self.readOutput,
                                       args=(master, loop), daemon=True)
        self.reader.start()
        threading.Thread(target=self.waitForExit, args=(loop,),
                         daemon=True).start()

    # --- Threads ---

    def readOutput(self, master, loop):
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO on Linux once every writer has closed
                data = b''
            if not data:
                break
            loop.call_soon_threadsafe(self.onOutput, data)
        os.close(master)
        loop.call_soon_threadsafe(self.onOutputClosed)

    def waitForExit(self, loop):
        return_code = self.popen.wait()
        # Report the exit after the last output (the lines just before a
        # crash matter most), unless something the process left running in
        # the background keeps the terminal open.
        self.reader.join(OUTPUT_DRAIN_TIMEOUT_S)
        loop.call_soon_threadsafe(self.onExit, return_code)

    # --- Output (loop thread) ---

    def writeLog(self, data):
        if self.log_bytes >= MAX_PROCESS_LOG_BYTES or self.log_file.closed:
            return
        self.log_bytes += len(data)
        self.log_file.write(data)
        if self.log_bytes >= MAX_PROCESS_LOG_BYTES:
            self.log_file.write(b'\n[orca: log size limit reached; '
                                b'further output is not saved]\n')
        self.log_file.flush()

    def onOutput(self, chunk):
        self.writeLog(chunk)
        # The incremental decoder keeps a multi-byte character that was split
        # across two reads.
        text = self.partial + self.decoder.decode(chunk)
        pieces = text.split('\n')
        self.partial = pieces.pop()
        for piece in pieces:
            self.addLine(cleanLine(piece))
        self.partial_since_s = time.time() if self.partial else None
        self.scheduleFlush()

    def addLine(self, line):
        self.lines.append(line)
        self.pending_lines.append(line)
        self.next_line_number += 1

    def scheduleFlush(self):
        if self.flush_handle is None:
            self.flush_handle = asyncio.get_running_loop().call_later(
                OUTPUT_FLUSH_S, self.flush)

    def flush(self):
        self.flush_handle = None
        # A prompt or progress line without a newline still gets shown.
        if self.partial and self.partial_since_s \
                and time.time() - self.partial_since_s > PARTIAL_LINE_FLUSH_S:
            self.addLine(cleanLine(self.partial))
            self.partial = ''
            self.partial_since_s = None
        elif self.partial:
            self.scheduleFlush()
        if self.pending_lines:
            lines, self.pending_lines = self.pending_lines, []
            self.manager.broadcastOutput(
                self.process_id, self.next_line_number - len(lines), lines)

    def onOutputClosed(self):
        if self.partial:
            self.addLine(cleanLine(self.partial))
            self.partial = ''
        if self.flush_handle:
            self.flush_handle.cancel()
            self.flush_handle = None
        self.flush()

    def onExit(self, return_code):
        self.ended_s = time.time()
        if return_code < 0:
            self.exit_signal = signal.Signals(-return_code).name
        else:
            self.exit_code = return_code
        self.state = 'exited'
        if self.stop_task:
            self.stop_task.cancel()
        summary = ('exited with code %d' % return_code if return_code >= 0
                   else 'killed by %s' % self.exit_signal)
        self.writeLog(('\n[orca: process %s]\n' % summary).encode())
        self.log_file.close()
        for path in self.remove_on_exit:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.manager.broadcastState(self)

    def output(self, from_line):
        '''Buffered lines from line number from_line on (as far as the buffer
        reaches back).'''
        first_buffered = self.next_line_number - len(self.lines)
        start = max(from_line, first_buffered)
        lines = list(self.lines)[start - first_buffered:]
        return {'first_line_number': start, 'lines': lines,
                'next_line_number': self.next_line_number}

    # --- Stopping ---

    def signalGroup(self, signal_number):
        try:
            os.killpg(self.popen.pid, signal_number)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, grace_s):
        if not self.isRunning():
            return
        if self.state == 'stopping':
            # Asked twice: skip ahead to the next step.
            self.signalGroup(signal.SIGKILL)
            return
        self.state = 'stopping'
        self.stop_requested_s = time.time()
        self.manager.broadcastState(self)
        self.stop_task = asyncio.ensure_future(self.escalate(grace_s))

    async def escalate(self, grace_s):
        self.signalGroup(signal.SIGINT)
        await asyncio.sleep(grace_s)
        self.signalGroup(signal.SIGTERM)
        await asyncio.sleep(TERM_GRACE_S)
        self.signalGroup(signal.SIGKILL)


class ProcessManager:

    def __init__(self, daemon):
        self.daemon = daemon
        self.processes = collections.OrderedDict()  # process id -> process

    def get(self, process_id):
        process = self.processes.get(process_id)
        if process is None:
            raise KeyError('no process ' + str(process_id))
        return process

    def numRunning(self):
        return len([process for process in self.processes.values()
                    if process.isRunning()])

    def start(self, params):
        existing = self.processes.get(params['process_id'])
        if existing is not None and existing.isRunning():
            raise RuntimeError('process %s is already running'
                               % params['process_id'])
        removeOldRunLogs()
        process = SupervisedProcess(self, params)
        process.start(asyncio.get_running_loop())
        self.processes.pop(process.process_id, None)
        self.processes[process.process_id] = process
        self.forgetOldProcesses()
        self.broadcastState(process)
        return process.record()

    def forgetOldProcesses(self):
        finished = [process_id for process_id, process
                    in self.processes.items() if not process.isRunning()]
        for process_id in finished[:-MAX_FINISHED_PROCESSES]:
            del self.processes[process_id]

    def broadcastState(self, process):
        self.daemon.broadcast('process.state', process.record())

    def broadcastOutput(self, process_id, first_line_number, lines):
        self.daemon.broadcast('process.output', {
            'process_id': process_id,
            'first_line_number': first_line_number,
            'lines': lines,
        }, only_watchers=True)



# --- Terminals ----------------------------------------------------------------



TERMINAL_FLUSH_S = 0.015


def runExecTty(argv):
    '''`exec-tty` mode, run in a new session on the terminal's pty: adopt it
    as the controlling terminal (so shells get job control and Ctrl-C reaches
    the foreground job), then become the real program. A trampoline instead
    of preexec_fn, which isn't safe in the threaded daemon.'''
    request = getattr(termios, 'TIOCSCTTY', None)
    if request is not None:
        try:
            fcntl.ioctl(0, request, 0)
        except OSError:
            pass
    try:
        os.execvp(argv[0], argv)
    except OSError as error:
        sys.stderr.write('could not start %s: %s\n' % (argv[0], error))
        return 127


class Terminal:
    '''An interactive program on a pseudo-terminal (a shell, or the VM's
    serial console). Unlike supervised processes it belongs to the client
    that opened it and is hung up when that client goes away.'''

    def __init__(self, manager, session, terminal_id, params):
        self.manager = manager
        self.session = session
        self.terminal_id = terminal_id
        self.argv = params['argv']
        self.cwd = os.path.expanduser(params.get('cwd') or '~')
        self.columns = int(params.get('cols', 100))
        self.rows = int(params.get('rows', 30))
        self.extra_env = params.get('env') or {}
        self.popen = None
        self.master = None
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.pending = []
        self.flush_handle = None

    def environment(self):
        environment = dict(os.environ)
        environment.update({'TERM': 'xterm-256color',
                            'COLORTERM': 'truecolor'})
        environment.update(
            {str(key): str(value) for key, value in self.extra_env.items()})
        return environment

    def start(self, loop):
        self.master, slave = pty.openpty()
        self.setSize(self.columns, self.rows, slave)
        try:
            self.popen = subprocess.Popen(
                [sys.executable, AGENT_PATH, 'exec-tty'] + self.argv,
                cwd=self.cwd, env=self.environment(),
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True)
        except OSError as error:
            os.close(self.master)
            os.close(slave)
            raise RuntimeError('could not start %s: %s'
                               % (self.argv[0], error))
        os.close(slave)
        threading.Thread(target=self.readOutput, args=(loop,),
                         daemon=True).start()

    def setSize(self, columns, rows, descriptor=None):
        self.columns, self.rows = columns, rows
        fcntl.ioctl(descriptor if descriptor is not None else self.master,
                    termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, columns, 0, 0))

    def write(self, text):
        data = text.encode()
        while data:
            written = os.write(self.master, data)
            data = data[written:]

    def readOutput(self, loop):
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                data = b''
            if not data:
                break
            loop.call_soon_threadsafe(self.onOutput, data)
        return_code = self.popen.wait()
        loop.call_soon_threadsafe(self.onExit, return_code)

    def onOutput(self, data):
        self.pending.append(self.decoder.decode(data))
        if self.flush_handle is None:
            self.flush_handle = asyncio.get_running_loop().call_later(
                TERMINAL_FLUSH_S, self.flush)

    def flush(self):
        self.flush_handle = None
        if self.pending:
            text, self.pending = ''.join(self.pending), []
            asyncio.ensure_future(self.session.send({
                'event': 'terminal.output',
                'data': {'terminal_id': self.terminal_id, 'data': text}}))

    def onExit(self, return_code):
        if self.flush_handle:
            self.flush_handle.cancel()
        self.flush()
        try:
            os.close(self.master)
        except OSError:
            pass
        self.manager.terminals.pop(self.terminal_id, None)
        asyncio.ensure_future(self.session.send({
            'event': 'terminal.exit',
            'data': {'terminal_id': self.terminal_id,
                     'exit_code': return_code}}))

    def close(self):
        try:
            os.killpg(self.popen.pid, signal.SIGHUP)
        except (ProcessLookupError, PermissionError):
            pass


class TerminalManager:

    def __init__(self):
        self.terminals = {}  # terminal id -> Terminal
        self.next_id = 1

    def open(self, session, params):
        terminal_id = 't%d' % self.next_id
        self.next_id += 1
        terminal = Terminal(self, session, terminal_id, params)
        terminal.start(asyncio.get_running_loop())
        self.terminals[terminal_id] = terminal
        return {'terminal_id': terminal_id}

    def get(self, terminal_id):
        terminal = self.terminals.get(terminal_id)
        if terminal is None:
            raise KeyError('no terminal ' + str(terminal_id))
        return terminal

    def closeSession(self, session):
        for terminal in list(self.terminals.values()):
            if terminal.session is session:
                terminal.close()



# --- Daemon -------------------------------------------------------------------



class ClientSession:
    '''One connected client (normally one orca, through `attach`).'''

    def __init__(self, daemon, reader, writer):
        self.daemon = daemon
        self.reader = reader
        self.writer = writer
        self.write_lock = asyncio.Lock()
        self.stats_task = None
        self.request_tasks = set()
        # Output is only sent to clients that ask for it (see process.watch),
        # so a status-only client costs almost no bandwidth: all processes,
        # or only some (competition mode watches just Orca's
        # monitor).
        self.watches_output = False
        self.watched_ids = set()

    def watches(self, process_id):
        return self.watches_output or process_id in self.watched_ids

    async def run(self):
        await self.send({'event': 'hello', 'data': self.daemon.hello()})
        while True:
            line = await self.reader.readline()
            if not line:
                break
            task = asyncio.ensure_future(self.handleLine(line))
            self.request_tasks.add(task)
            task.add_done_callback(self.request_tasks.discard)
        self.close()

    def close(self):
        if self.stats_task:
            self.stats_task.cancel()
        for task in list(self.request_tasks):
            task.cancel()
        self.writer.close()

    async def send(self, message):
        data = (json.dumps(message, separators=(',', ':')) + '\n').encode()
        async with self.write_lock:
            try:
                self.writer.write(data)
                await self.writer.drain()
            except (ConnectionError, RuntimeError):
                pass

    async def handleLine(self, line):
        try:
            request = json.loads(line)
        except ValueError:
            await self.send({'id': None,
                             'error': {'message': 'request is not JSON'}})
            return
        request_id = request.get('id')
        handler = self.daemon.methods.get(request.get('method'))
        if handler is None:
            await self.send({'id': request_id, 'error': {
                'message': 'unknown method: ' + str(request.get('method'))}})
            return
        try:
            result = await handler(self, request.get('params') or {})
        except Exception as error:
            await self.send({'id': request_id, 'error': {
                'message': type(error).__name__ + ': ' + str(error)}})
            return
        await self.send({'id': request_id, 'result': result})

    def startStats(self, interval_s):
        if self.stats_task:
            self.stats_task.cancel()
            self.stats_task = None
        if interval_s:
            self.stats_task = asyncio.ensure_future(
                self.statsLoop(max(0.5, float(interval_s))))

    async def statsLoop(self, interval_s):
        while True:
            stats = await self.daemon.currentStats()
            await self.send({'event': 'stats', 'data': stats})
            await asyncio.sleep(interval_s)


class Daemon:

    def __init__(self):
        self.started_s = time.time()
        self.sessions = set()
        self.stop_event = None
        self.sampler = None
        self.stats_lock = None
        self.last_stats = None
        self.last_stats_s = 0.0
        self.processes = ProcessManager(self)
        self.terminals = TerminalManager()
        self.methods = {
            'ping': self.methodPing,
            'system.info': self.methodSystemInfo,
            'system.stats': self.methodSystemStats,
            'subscribe': self.methodSubscribe,
            'workspace.status': self.methodWorkspaceStatus,
            'vm.status': self.methodVmStatus,
            'process.start': self.methodProcessStart,
            'process.stop': self.methodProcessStop,
            'process.list': self.methodProcessList,
            'process.output': self.methodProcessOutput,
            'process.watch': self.methodProcessWatch,
            'file.write': self.methodFileWrite,
            'workspace.refs': self.methodWorkspaceRefs,
            'terminal.open': self.methodTerminalOpen,
            'terminal.input': self.methodTerminalInput,
            'terminal.resize': self.methodTerminalResize,
            'terminal.close': self.methodTerminalClose,
            'net.ping': self.methodNetPing,
            'net.tcp': self.methodNetTcp,
            'net.scan': self.methodNetScan,
            'net.neighbours': self.methodNetNeighbours,
            'share.status': self.methodShareStatus,
            'machine.survey': self.methodMachineSurvey,
            'setup.check': self.methodSetupCheck,
            'share.up': self.methodShareUp,
            'share.down': self.methodShareDown,
            'machine.reboot': self.methodMachineReboot,
            'bags.list': self.methodBagsList,
            'bags.delete': self.methodBagsDelete,
            'logs.list': self.methodLogsList,
            'logs.read': self.methodLogsRead,
            'logs.delete': self.methodLogsDelete,
            'cleanup.scan': self.methodCleanupScan,
            'cleanup.run': self.methodCleanupRun,
            'agent.shutdown': self.methodShutdown,
        }

    def isIdle(self):
        '''True when no Orca-started processes are running, so the
        daemon can be restarted (e.g. to update it) without disturbing
        anything.'''
        return self.processes.numRunning() == 0

    def broadcast(self, event, data, only_watchers=False):
        message = {'event': event, 'data': data}
        for session in list(self.sessions):
            if only_watchers and not session.watches(data['process_id']):
                continue
            asyncio.ensure_future(session.send(message))

    def hello(self):
        return {
            'agent_hash': AGENT_HASH,
            'file_hash': fileHash(AGENT_PATH),
            'protocol_version': PROTOCOL_VERSION,
            'is_idle': self.isIdle(),
            'hostname': socket.gethostname(),
            'platform': 'macos' if IS_MACOS else 'linux',
            'python': platform.python_version(),
        }

    async def currentStats(self):
        async with self.stats_lock:
            if time.time() - self.last_stats_s >= STATS_CACHE_S:
                self.last_stats = await asyncio.get_running_loop()\
                    .run_in_executor(None, self.sampler.sample)
                self.last_stats_s = time.time()
            return self.last_stats

    # --- Methods ---

    async def methodPing(self, session, params):
        return {'time_s': time.time()}

    async def methodSystemInfo(self, session, params):
        return await asyncio.get_running_loop().run_in_executor(
            None, systemInfo, self.started_s)

    async def methodSystemStats(self, session, params):
        return await self.currentStats()

    async def methodSubscribe(self, session, params):
        session.startStats(params.get('stats_interval_s'))
        return {'stats_interval_s': params.get('stats_interval_s')}

    async def methodWorkspaceStatus(self, session, params):
        return await asyncio.get_running_loop().run_in_executor(
            None, workspaceStatus, params['path'])

    async def methodVmStatus(self, session, params):
        if not IS_MACOS:
            raise RuntimeError('VM control is only available on a mac host')
        return await asyncio.get_running_loop().run_in_executor(
            None, vmStatus, params['csm_vm_path'])

    async def methodProcessStart(self, session, params):
        '''params: process_id, argv, cwd, env, label, meta (returned as is;
        Orca keeps its run information there).'''
        return self.processes.start(params)

    async def methodProcessStop(self, session, params):
        process = self.processes.get(params['process_id'])
        process.stop(float(params.get('grace_s', DEFAULT_STOP_GRACE_S)))
        return process.record()

    async def methodProcessList(self, session, params):
        return [process.record()
                for process in self.processes.processes.values()]

    async def methodProcessOutput(self, session, params):
        process = self.processes.get(params['process_id'])
        return process.output(int(params.get('from_line', 0)))

    async def methodProcessWatch(self, session, params):
        '''params: enabled (all processes), process_ids (just these).'''
        session.watches_output = bool(params.get('enabled', True))
        session.watched_ids = set(params.get('process_ids') or [])
        return {'watching': session.watches_output,
                'process_ids': sorted(session.watched_ids)}

    async def methodFileWrite(self, session, params):
        '''Write a small text file (e.g. a launch overlay). Returns its
        absolute path.'''
        path = os.path.abspath(os.path.expanduser(params['path']))
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            raise RuntimeError('directory does not exist: ' + directory)
        temporary_path = path + '.tmp'
        with open(temporary_path, 'w') as file:
            file.write(params['content'])
        os.replace(temporary_path, path)
        return {'path': path}

    async def inThread(self, function, *arguments):
        return await asyncio.get_running_loop().run_in_executor(
            None, function, *arguments)

    async def methodTerminalOpen(self, session, params):
        '''params: argv (default: the user's login shell), cwd, cols, rows.'''
        if not params.get('argv'):
            shell = os.environ.get('SHELL') or '/bin/bash'
            params = dict(params, argv=[shell, '-l'])
        return self.terminals.open(session, params)

    async def methodTerminalInput(self, session, params):
        self.terminals.get(params['terminal_id']).write(params['data'])
        return {}

    async def methodTerminalResize(self, session, params):
        self.terminals.get(params['terminal_id']).setSize(
            int(params['cols']), int(params['rows']))
        return {}

    async def methodTerminalClose(self, session, params):
        self.terminals.get(params['terminal_id']).close()
        return {}

    async def methodWorkspaceRefs(self, session, params):
        return await self.inThread(workspaceRefs, params['path'])

    async def methodNetPing(self, session, params):
        return await self.inThread(ping, params['host'],
                                   max(1, min(int(params.get('count', 4)), 20)))

    async def methodNetTcp(self, session, params):
        state, elapsed_ms = await tcpProbe(
            params['host'], int(params['port']),
            float(params.get('timeout_s', 2.0)))
        return {'host': params['host'], 'port': int(params['port']),
                'state': state, 'time_ms': elapsed_ms}

    async def methodNetScan(self, session, params):
        ports = [int(port) for port in params.get('ports') or [22]]
        return await scanNetwork(params['cidr'], ports,
                                 float(params.get('timeout_s', 0.6)))

    async def methodSetupCheck(self, session, params):
        return {'checks': await self.inThread(setupChecks, params),
                'user': getpass.getuser()}

    async def methodMachineSurvey(self, session, params):
        stats = await self.currentStats()
        return await self.inThread(machineSurvey, stats['interfaces'])

    async def methodShareStatus(self, session, params):
        return await self.inThread(shareStatus, params.get('expected_hash'))

    async def methodShareUp(self, session, params):
        return await self.inThread(shareUp, params)

    async def methodShareDown(self, session, params):
        return await self.inThread(shareDown, params)

    async def methodMachineReboot(self, session, params):
        return await self.inThread(rebootMachine,
                                   bool(params.get('check_only')))

    async def methodNetNeighbours(self, session, params):
        '''{ip: mac} of hosts this machine has talked to recently.'''
        return await self.inThread(neighbourTable)

    async def methodBagsList(self, session, params):
        return await self.inThread(listBags, params['workspace'])

    async def methodBagsDelete(self, session, params):
        return await self.inThread(deleteBag, params['workspace'],
                                   params['name'])

    async def methodLogsList(self, session, params):
        return await self.inThread(listLogs, params.get('workspace'))

    async def methodLogsRead(self, session, params):
        return await self.inThread(
            readLog, params['path'], params.get('max_bytes', 1024 * 1024),
            params.get('workspace'))

    def pathsInUse(self):
        '''Files running processes use: their run logs and overlays.'''
        paths = set()
        for process in self.processes.processes.values():
            if process.isRunning():
                paths.add(os.path.realpath(process.log_path))
                paths.update(os.path.realpath(path)
                             for path in process.remove_on_exit)
                paths.update(os.path.realpath(os.path.expanduser(
                    argument.split(':=', 1)[1])) for argument in process.argv
                    if argument.startswith('json_path:='))
        return paths

    async def methodLogsDelete(self, session, params):
        return await self.inThread(deleteLog, params['path'],
                                   params.get('workspace'), self.pathsInUse())

    async def methodCleanupScan(self, session, params):
        return await self.inThread(cleanupScan, params, self.pathsInUse())

    async def methodCleanupRun(self, session, params):
        return await self.inThread(cleanup, params, self.pathsInUse())

    async def methodShutdown(self, session, params):
        if not self.isIdle() and not params.get('force'):
            return {'stopping': False, 'reason': 'busy'}
        asyncio.get_running_loop().call_later(0.2, self.stop_event.set)
        return {'stopping': True}

    # --- Serving ---

    async def handleClient(self, reader, writer):
        session = ClientSession(self, reader, writer)
        self.sessions.add(session)
        try:
            await session.run()
        except (ConnectionError, asyncio.IncompleteReadError):
            session.close()
        finally:
            self.sessions.discard(session)
            self.terminals.closeSession(session)

    async def serve(self):
        loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.stats_lock = asyncio.Lock()
        self.sampler = await loop.run_in_executor(None, StatsSampler)
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signal_number, self.stop_event.set)

        server = await asyncio.start_unix_server(
            self.handleClient, path=SOCKET_NAME, limit=16 * 1024 * 1024)
        os.chmod(SOCKET_NAME, 0o600)
        log('daemon %s listening on %s' % (AGENT_HASH, SOCKET_PATH))

        await self.stop_event.wait()
        server.close()
        for session in list(self.sessions):
            session.close()
        log('daemon stopping')



# --- Daemon process management ------------------------------------------------



def log(message):
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print('[%s] %s' % (stamp, message), flush=True)


def openLogFile():
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
        os.replace(LOG_PATH, LOG_PATH + '.1')
    return open(LOG_PATH, 'a')


def acquireDaemonLock():
    '''Only one daemon per user. A daemon that's shutting down holds the lock
    briefly, so wait a little before giving up.'''
    lock_file = open(LOCK_PATH, 'w')
    deadline = time.time() + DAEMON_LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock_file
        except OSError:
            if time.time() > deadline:
                lock_file.close()
                return None
            time.sleep(0.1)


def runDaemon():
    os.makedirs(AGENT_DIR, mode=0o700, exist_ok=True)
    os.chdir(AGENT_DIR)
    os.umask(0o077)
    lock_file = acquireDaemonLock()
    if lock_file is None:
        log('another daemon is already running')
        return 1
    if IS_MACOS:
        paths = os.environ.get('PATH', '').split(':')
        os.environ['PATH'] = ':'.join(
            [path for path in MACOS_EXTRA_PATHS if path not in paths] + paths)
    if os.path.exists(SOCKET_NAME):
        os.unlink(SOCKET_NAME)
    try:
        asyncio.run(Daemon().serve())
    finally:
        if os.path.exists(SOCKET_NAME):
            os.unlink(SOCKET_NAME)
        lock_file.close()
    return 0


def connectToDaemon():
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(SOCKET_NAME)
    except OSError:
        connection.close()
        return None
    return connection


def startDaemonProcess():
    os.makedirs(AGENT_DIR, mode=0o700, exist_ok=True)
    log_file = openLogFile()
    subprocess.Popen(
        [sys.executable, '-u', AGENT_PATH, 'daemon'],
        stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
        cwd=os.path.expanduser('~'), start_new_session=True, close_fds=True)
    log_file.close()


def waitForDaemon():
    deadline = time.time() + DAEMON_START_TIMEOUT_S
    while time.time() < deadline:
        connection = connectToDaemon()
        if connection is not None:
            return connection
        time.sleep(0.1)
    return None



# --- Attach -------------------------------------------------------------------



def writeAll(file_descriptor, data):
    while data:
        written = os.write(file_descriptor, data)
        data = data[written:]


def bridge(connection):
    '''Copy stdin to the daemon and the daemon's replies to stdout until
    either side closes.'''

    def forwardStdin():
        while True:
            try:
                data = os.read(0, 65536)
                if not data:
                    break
                connection.sendall(data)
            except OSError:
                break
        try:
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=forwardStdin, daemon=True).start()
    while True:
        try:
            data = connection.recv(65536)
            if not data:
                break
            writeAll(1, data)
        except OSError:
            break
    return 0


def runAttach():
    os.makedirs(AGENT_DIR, mode=0o700, exist_ok=True)
    os.chdir(AGENT_DIR)
    connection = connectToDaemon()
    if connection is None:
        startDaemonProcess()
        connection = waitForDaemon()
    if connection is None:
        message = {'event': 'agent-error', 'data': {
            'message': 'agent daemon did not start; see ' + LOG_PATH}}
        writeAll(1, (json.dumps(message) + '\n').encode())
        return 1
    return bridge(connection)



# --- Main ---------------------------------------------------------------------



def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'daemon':
        return runDaemon()
    if mode == 'attach':
        return runAttach()
    if mode == 'hash':
        print(AGENT_HASH)
        return 0
    if mode == 'exec-tty' and len(sys.argv) > 2:
        return runExecTty(sys.argv[2:])
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
