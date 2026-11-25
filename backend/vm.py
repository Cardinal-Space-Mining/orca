'''
Controlling a csm-vm virtual machine on a mac host, through its run-vm.sh.

Each action is a one-step Run on the host (so its output streams like any
launch). run-vm.sh needs passwordless sudo for qemu-system-aarch64 and socat
when started without a terminal (docs/machine-setup.md).
'''

from __future__ import annotations

import asyncio
import posixpath

from .runs import PlanError, Run, expandHome, newRunId


# action -> (run-vm.sh flags, VM must be running?, label)
VM_ACTIONS = {
    'start': (['--startd'], False, 'Start VM'),
    'stop': (['--stopd'], True, 'Shut down VM'),
    'restart': (['--restartd'], True, 'Restart VM'),
    'kill': (['--fkilld'], True, 'Force stop VM'),
    'commit': (['--clean'], False, 'Commit VM overlay'),
    'backup': (['--backup'], False, 'Back up VM disk'),
    'restore': (['--restore'], False, 'Restore VM disk'),
}
OVERLAY_CHOICES = ('commit', 'reuse', 'discard')
# After a start, the guest boots for a while; wake its link a few times
# instead of waiting out the reconnect back-off.
GUEST_WAKE_DELAYS_S = [15, 30, 45, 60, 90]


class VmController:

    def __init__(self, orca):
        self.orca = orca

    def host(self, machine_id: str):
        link = self.orca.link(machine_id)
        if not link.machine.vm:
            raise PlanError('%s has no VM in the inventory'
                            % link.machine.label)
        if link.connection is None:
            raise PlanError('%s is not connected' % link.machine.label)
        return link

    def activeRun(self, machine_id: str):
        for run in self.orca.runs.values():
            if run.kind == 'vm' and run.scenario_id == machine_id \
                    and run.snapshot()['state'] in ('starting', 'running'):
                return run
        return None

    def check(self, link, action: str, vm: dict):
        _, needs_running, _ = VM_ACTIONS[action]
        is_running = bool(vm and vm.get('is_running'))
        if needs_running and not is_running:
            raise PlanError('the VM is not running')
        if not needs_running and action != 'start' and is_running:
            raise PlanError('stop the VM first')
        if action == 'start' and is_running:
            raise PlanError('the VM is already running')
        disks = (vm or {}).get('disks') or {}
        if action == 'commit' and disks.get('overlay_bytes') is None:
            raise PlanError('there is no overlay to commit')
        if action == 'restore' and disks.get('backup_bytes') is None:
            raise PlanError('there is no backup to restore')

    def run(self, machine_id: str, action: str, options: dict) -> Run:
        if action not in VM_ACTIONS:
            raise PlanError('unknown VM action ' + action)
        link = self.host(machine_id)
        if self.activeRun(machine_id):
            raise PlanError('another VM action is still running')
        self.check(link, action, link.state['vm'])
        flags, _, label = VM_ACTIONS[action]
        argv = [self.scriptPath(link)] + flags
        if action in ('start', 'restart'):
            overlay = options.get('overlay') or 'reuse'
            if overlay not in OVERLAY_CHOICES:
                raise PlanError('overlay must be commit, reuse or discard')
            argv += ['--overlay', overlay]
            if options.get('wifi'):
                argv.append('--wifi')
        label = '%s on %s' % (label, link.machine.label)
        plan = {'processes': [{
            'key': action, 'label': label, 'machine_id': machine_id,
            'task': True, 'after': [], 'start_delay_s': 0,
            'cwd': posixpath.dirname(argv[0]), 'argv': argv}]}
        # The run's scenario_id names the host, so activeRun can find it.
        run = Run(self.orca, newRunId(), label, machine_id, plan,
                  kind='vm')
        self.orca.runs[run.run_id] = run
        self.orca.forgetOldRuns()
        run.start()
        if action in ('start', 'restart'):
            asyncio.ensure_future(self.wakeGuest(link))
        return run

    def scriptPath(self, link) -> str:
        directory = expandHome(link.machine.vm.csm_vm_path, link.state['info'])
        return posixpath.join(directory, 'run-vm.sh')

    async def wakeGuest(self, link):
        guest = self.orca.links_by_id.get(link.machine.vm.guest or '')
        if guest is None:
            return
        elapsed = 0
        for delay in GUEST_WAKE_DELAYS_S:
            await asyncio.sleep(delay - elapsed)
            elapsed = delay
            if guest.state['connection']['state'] == 'online':
                return
            guest.requestReconnect()

    def serialConsoleArgv(self, machine_id: str) -> list:
        link = self.host(machine_id)
        vm = link.state['vm'] or {}
        if not vm.get('is_running'):
            raise PlanError('the VM is not running')
        socket = vm.get('serial_socket')
        if not socket:
            raise PlanError('the VM was started without a serial console; '
                            'restart it from Orca to get one')
        # Raw mode: every key (Ctrl-C included) goes to the VM.
        return ['sudo', '-n', 'socat', '-,raw,echo=0',
                'UNIX-CONNECT:' + socket]
