'''
Restarting a machine from its card: a quick way to recover hardware that
only a restart brings back (a USB adapter macOS lost after a VM let go of
it, say) when nobody can reach the robot to replug it.

A VM host first shuts its VM down cleanly, so its overlay is committed rather
than lost to the host's shutdown killing QEMU, and starts it again once the
host is back. Each step shows on the machine's card (link.state["restart"]).

Restarting takes root. With passwordless sudo for the restart command (the
"Restart without a password" setup item), Orca does it in one go; otherwise
the card opens a terminal that runs it, where sudo asks for the password,
and Orca carries on from there once the machine drops off.
'''

from __future__ import annotations

import asyncio
import shlex
import time

from .link import AgentError
from .runs import PlanError


GONE_TIMEOUT_S = 90            # the restart command -> the machine drops off
TERMINAL_GONE_TIMEOUT_S = 300  # someone types the sudo password first
BACK_TIMEOUT_S = 600           # dropped off -> back online
VM_RUN_TIMEOUT_S = 300         # a VM shutdown or start
AUTOSTART_WAIT_S = 150         # a host with autostart starts its VM itself
VM_POLL_S = 5.0
POLL_S = 1.0
WAKE_PERIOD_S = 5.0            # retry connecting this often while it boots
DONE_SHOWN_S = 60              # "Restarted" stays on the card this long


class Restarter:

    def __init__(self, orca):
        self.orca = orca
        self.tasks = {}  # machine id -> task

    # --- Starting ---

    def check(self, machine_id: str):
        link = self.orca.link(machine_id)
        if link.machine.is_local:
            raise PlanError("Orca can't restart the machine it runs on")
        if link.connection is None:
            raise PlanError('%s is not connected' % link.machine.label)
        task = self.tasks.get(machine_id)
        if task is not None and not task.done():
            raise PlanError('%s is already restarting' % link.machine.label)
        return link

    async def restart(self, machine_id: str) -> dict:
        '''Restart now, or {"needs_password": true} if sudo would ask for
        one (then the card offers a terminal instead).'''
        link = self.check(machine_id)
        result = await link.call('machine.reboot', {'check_only': True})
        if result.get('needs_password'):
            return {'needs_password': True}
        self.begin(link, via_terminal=False)
        return {'ok': True}

    async def terminalArgv(self, machine_id: str) -> list:
        '''What a restart terminal runs: shutting the VM down first on a
        host, then the restart command through sudo, which asks for the
        password there. Orca takes over again once the machine drops off.'''
        link = self.check(machine_id)
        result = await link.call('machine.reboot', {'check_only': True})
        command = shlex.join(result.get('command')
                             or ['sudo'] + REBOOT_FALLBACK)
        if self.isVmRunning(link):
            command = '%s --stopd && %s' % (
                shlex.quote(self.orca.vm.scriptPath(link)), command)
        self.begin(link, via_terminal=True)
        return ['/bin/sh', '-c', 'echo "$ %s"; %s' % (
            command.replace('"', '\\"'), command)]

    def begin(self, link, via_terminal: bool):
        self.tasks[link.machine.id] = asyncio.ensure_future(
            self.run(link, via_terminal))

    # --- The steps ---

    def isVmRunning(self, link) -> bool:
        return bool(link.machine.vm
                    and (link.state['vm'] or {}).get('is_running'))

    def show(self, link, phase: str, error=None, done=False):
        link.state['restart'] = {'phase': phase, 'error': error,
                                 'done': done, 'since_s': time.time()}
        link.publish()

    async def run(self, link, via_terminal: bool):
        had_vm = self.isVmRunning(link)
        try:
            if had_vm and not via_terminal:
                self.show(link, 'Shutting down the VM')
                await self.vmAction(link, 'stop', {}, "the VM didn't shut "
                                    'down, so the restart stopped: force '
                                    'stop it, then restart again')
            if via_terminal:
                self.show(link, 'Waiting for the restart (type the sudo '
                          'password in the terminal)')
            else:
                self.show(link, 'Restarting')
                result = await link.call('machine.reboot')
                if result.get('needs_password'):
                    raise PlanError('sudo asks for a password now: restart '
                                    'it from a terminal')
            await self.waitUntil(
                link, lambda: not self.isOnline(link),
                TERMINAL_GONE_TIMEOUT_S if via_terminal else GONE_TIMEOUT_S,
                "it didn't go down: the restart didn't happen")
            self.show(link, 'Waiting for it to come back')
            await self.waitUntil(link, lambda: self.isOnline(link),
                                 BACK_TIMEOUT_S, "it hasn't come back yet "
                                 '(Orca keeps trying to connect)', wake=True)
            if had_vm:
                await self.restartVm(link)
            self.show(link, 'Restarted', done=True)
            # The task ends here (so another restart can start right away);
            # the "Restarted" note goes a minute later, unless replaced.
            shown = link.state['restart']
            asyncio.get_running_loop().call_later(
                DONE_SHOWN_S, self.clearDone, link, shown)
        except (PlanError, AgentError, ConnectionError,
                asyncio.TimeoutError) as error:
            self.show(link, 'Restart failed', error=str(error))

    async def restartVm(self, link):
        '''Start the VM again, unless the host's autostart does: then wait
        for it rather than race it (run-vm.sh refuses a second VM anyway).'''
        await link.refreshVm()
        if self.isVmRunning(link):
            return
        if (link.state['vm'] or {}).get('autostart'):
            self.show(link, 'Waiting for autostart to start the VM')
            start_s = time.monotonic()
            while time.monotonic() - start_s < AUTOSTART_WAIT_S:
                await asyncio.sleep(VM_POLL_S)
                await link.refreshVm()
                if self.isVmRunning(link):
                    return
        self.show(link, 'Starting the VM')
        try:
            await self.vmAction(link, 'start', {'overlay': 'reuse'},
                                "the VM didn't start again")
        except PlanError:
            await link.refreshVm()
            if not self.isVmRunning(link):  # not started by someone else
                raise

    def clearDone(self, link, shown: dict):
        if link.state.get('restart') is shown:
            link.state['restart'] = None
            link.publish()

    def isOnline(self, link) -> bool:
        return link.state['connection']['state'] == 'online'

    async def waitUntil(self, link, condition, timeout_s: float,
                        message: str, wake: bool = False):
        '''Wait for condition(); with wake, ask the link to try connecting
        every few seconds rather than wait out its back-off.'''
        start_s = time.monotonic()
        last_wake_s = start_s
        while not condition():
            now_s = time.monotonic()
            if now_s - start_s > timeout_s:
                raise PlanError(message)
            if wake and now_s - last_wake_s >= WAKE_PERIOD_S \
                    and link.state['connection']['state'] == 'offline':
                link.requestReconnect()
                last_wake_s = now_s
            await asyncio.sleep(POLL_S)

    async def vmAction(self, link, action: str, options: dict,
                       message: str):
        run = self.orca.vm.run(link.machine.id, action, options)
        start_s = time.monotonic()
        while run.snapshot()['state'] not in ('finished', 'failed'):
            if time.monotonic() - start_s > VM_RUN_TIMEOUT_S:
                raise PlanError(message)
            await asyncio.sleep(POLL_S)
        if run.snapshot()['state'] != 'finished':
            raise PlanError(message)


# If an older agent doesn't say which command it would run.
REBOOT_FALLBACK = ['reboot']
