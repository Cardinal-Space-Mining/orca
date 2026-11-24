#!/usr/bin/env python3
'''
csm-hub agent.

Runs on every machine the hub manages: the robot's mac host, its Ubuntu VM,
client laptops, and the hub's own machine. It is one file that uses only the
standard library, so the hub can copy it over SSH and run it with any
Python 3.9+, including macOS's /usr/bin/python3.

Modes (first command-line argument):
    daemon   Long-running server. Answers requests on a unix socket and will
             own the processes the hub starts, so they outlive the hub's
             connection.
    attach   Bridges stdin/stdout to the daemon's socket, starting the daemon
             first if it isn't running. The hub runs this over SSH, so the
             agent never opens a network port and SSH keys are the only
             authentication.
    hash     Prints the hash of this file (the hub compares it with its copy).

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
import json
import os
import platform
import plistlib
import pty
import re
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
# CSM_HUB_AGENT_DIR lets tests run an agent beside the real one.
AGENT_DIR = os.environ.get('CSM_HUB_AGENT_DIR') \
    or os.path.join(os.path.expanduser('~'), '.csm-hub')
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
    with open(path, 'rb') as file:
        return hashlib.sha256(file.read()).hexdigest()[:12]


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
        interfaces.append({
            'name': name,
            'label': None,
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
        interfaces.append({
            'name': name,
            'label': port,
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
    status = {'path': path, 'exists': os.path.isdir(repository)}
    if not status['exists']:
        return status

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
    return status



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
                   'memory': None, 'monitor_socket': None}
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
    status['autostart'] = autostartDaemons()
    return status



# --- Supervised processes -----------------------------------------------------



CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]')


def cleanLine(text):
    '''Keep colour codes (the web app renders them) but drop other control
    characters. A carriage return mid-line (progress bars) keeps only the
    last segment, like a terminal would show.'''
    text = text.rstrip('\r')
    if '\r' in text:
        text = text.rsplit('\r', 1)[1]
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
    '''One process started for the hub. Runs on a pseudo-terminal so programs
    line-buffer and colour their output as they would in a terminal, in its
    own process group so stopping it reaches every child (like Ctrl-C).

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
        threading.Thread(target=self.readOutput, args=(master, loop),
                         daemon=True).start()
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
        loop.call_soon_threadsafe(self.onExit, return_code)

    # --- Output (loop thread) ---

    def writeLog(self, data):
        if self.log_bytes >= MAX_PROCESS_LOG_BYTES:
            return
        self.log_bytes += len(data)
        self.log_file.write(data)
        if self.log_bytes >= MAX_PROCESS_LOG_BYTES:
            self.log_file.write(b'\n[csm-hub: log size limit reached; '
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
        self.writeLog(('\n[csm-hub: process %s]\n' % summary).encode())
        self.log_file.close()
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



# --- Daemon -------------------------------------------------------------------



class ClientSession:
    '''One connected client (normally one hub, through `attach`).'''

    def __init__(self, daemon, reader, writer):
        self.daemon = daemon
        self.reader = reader
        self.writer = writer
        self.write_lock = asyncio.Lock()
        self.stats_task = None
        self.request_tasks = set()
        # Output is only sent to clients that ask for it (see process.watch),
        # so a status-only client costs almost no bandwidth.
        self.watches_output = False

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
            'agent.shutdown': self.methodShutdown,
        }

    def isIdle(self):
        '''True when no hub-started processes are running, so the daemon
        can be restarted (e.g. to update it) without disturbing anything.'''
        return self.processes.numRunning() == 0

    def broadcast(self, event, data, only_watchers=False):
        message = {'event': event, 'data': data}
        for session in list(self.sessions):
            if only_watchers and not session.watches_output:
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
        the hub keeps its run information there).'''
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
        session.watches_output = bool(params.get('enabled', True))
        return {'watching': session.watches_output}

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
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
