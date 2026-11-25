'''
Deploying code and pulling bags (what sync.sh does, plus builds).

Pushes copy Orca's workspace to other machines with rsync over the
same SSH route Orca uses, with sync.sh's `--ws` rules: everything
under src/ (including .git, so the copy reports the same commit), never
csm-sim, and files removed here are removed there. A build can follow on the
target: `./src/build.sh <flags>` with the ROS distro the workspace was built
against.

Both run as Runs, so their output streams like a launch: the push runs on
this machine's agent (it's this machine's rsync), the build on the target's.
'''

from __future__ import annotations

import asyncio
import os
import shlex
import time
from typing import Optional

from .runs import PlanError, Run, expandHome, newRunId
from .ssh import sshBaseCommand


# sync.sh --ws (exclusions take precedence over inclusions), plus one rule
# sync.sh doesn't have: nothing inside .git is ever deleted on the target, so
# commits, branches and stashes that only exist there survive a push. The
# working tree is still mirrored exactly.
# Python caches are made by each machine for itself, so they're never sent
# (and, being excluded, never deleted there either).
WORKSPACE_FILTERS = ['--filter', 'P /src/.git/***',
                     '--exclude', '__pycache__/', '--exclude', '*.pyc',
                     '--exclude', '*csm-sim*', '--include', 'src/***',
                     '--exclude', '*']
BAG_DIRECTORY = 'bag_recordings'
BUILD_TARGETS = {'all': '--la', 'lance1': '--l1', 'lance2': '--l2'}
COMPARE_TIMEOUT_S = 120.0
MAX_LISTED_CHANGES = 400


class Deployer:

    def __init__(self, orca):
        self.orca = orca

    # --- Machines and paths ---

    def localWorkspace(self) -> str:
        if not self.orca.workspace_root:
            raise PlanError("Orca isn't inside a workspace")
        return self.orca.workspace_root

    def target(self, machine_id: str):
        link = self.orca.link(machine_id)
        if link.machine.is_local:
            raise PlanError('%s is this machine' % link.machine.label)
        if link.connection is None or link.active_route is None:
            raise PlanError('%s is not connected' % link.machine.label)
        if not link.machine.workspace:
            raise PlanError('%s has no workspace in the inventory'
                            % link.machine.label)
        return link

    def remoteWorkspace(self, link) -> str:
        return expandHome(link.machine.workspace, link.state['info'])

    def rsyncShell(self, link) -> str:
        '''The -e command for rsync: Orca's SSH options for the
        route the machine is connected on.'''
        return shlex.join(sshBaseCommand(link.active_route,
                                         self.orca.jumpRouteFor))

    def remotePath(self, link, path: str) -> str:
        return '%s:%s' % (link.active_route.target(), path)

    # --- Pushing code ---

    def pushArgv(self, link, delete: bool, dry_run: bool = False) -> list:
        argv = ['rsync', '-az', '-e', self.rsyncShell(link)]
        if delete:
            argv.append('--delete')
        if dry_run:
            argv += ['--dry-run', '--itemize-changes']
        else:
            argv += ['--info=stats1,progress2']
        argv += WORKSPACE_FILTERS
        argv += [self.localWorkspace().rstrip('/') + '/',
                 self.remotePath(link, self.remoteWorkspace(link) + '/')]
        return argv

    async def compare(self, machine_id: str, delete: bool = True) -> dict:
        '''What a push would change on the target (rsync dry run).'''
        link = self.target(machine_id)
        process = await asyncio.create_subprocess_exec(
            *self.pushArgv(link, delete, dry_run=True),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, errors = await asyncio.wait_for(process.communicate(),
                                                    COMPARE_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            raise PlanError('comparing with %s timed out'
                            % link.machine.label)
        if process.returncode != 0:
            raise PlanError('rsync failed: '
                            + errors.decode(errors='replace').strip())
        summary = summarizeItemized(output.decode(errors='replace'))
        summary.update(await self.whatWouldBeLost(link))
        return summary

    async def whatWouldBeLost(self, link) -> dict:
        '''Work on the target that a push overwrites: commits no local ref
        has, and uncommitted changes there.'''
        remote = await link.call('workspace.refs',
                                 {'path': link.machine.workspace})
        shas = sorted(set(remote['refs'].values()))
        known = await self.localCommits(shas)
        unique = sorted(name for name, sha in remote['refs'].items()
                        if sha not in known
                        and not name.startswith('refs/remotes/'))
        loose = set(remote.get('loose') or [])
        return {'remote_only_refs': [
                    {'ref': name, 'sha': remote['refs'][name][:7],
                     'kept': name in loose}
                    for name in unique],
                'remote_changes': remote['changes']}

    async def localCommits(self, shas: list) -> set:
        '''Which of these commits exist in the local repository.'''
        if not shas:
            return set()
        process = await asyncio.create_subprocess_exec(
            'git', '-C', os.path.join(self.localWorkspace(), 'src'),
            'cat-file', '--batch-check=%(objectname)',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        output, _ = await process.communicate(
            ('\n'.join(shas) + '\n').encode())
        return {line for line in output.decode().splitlines()
                if not line.endswith('missing')}

    # --- Building ---

    def rosDistro(self, link) -> str:
        workspace = link.state['workspace'] or {}
        distro = workspace.get('ros_distro')
        installed = workspace.get('ros_distros') or []
        if not distro and installed:
            distro = installed[-1]
        if not distro:
            raise PlanError('no ROS installation found on %s (/opt/ros)'
                            % link.machine.label)
        return distro

    def buildArgv(self, link, options: dict) -> list:
        flags = [BUILD_TARGETS.get(options.get('robot_target', 'all'), '--la')]
        if options.get('no_sim', True):
            flags.append('--no-sim')
        if options.get('native_arch'):
            flags.append('--native-arch')
        command = 'source /opt/ros/%s/setup.bash && exec ./src/build.sh %s' % (
            shlex.quote(self.rosDistro(link)), ' '.join(flags))
        return ['bash', '-c', command]

    # --- Deploy runs ---

    def plan(self, request: dict) -> dict:
        '''request: {"machine_ids": [...], "push": bool, "build": bool,
        "delete": bool, "build_options": {...}}'''
        push = request.get('push', True)
        build = request.get('build', True)
        if not push and not build:
            raise PlanError('nothing to do: choose push and/or build')
        machine_ids = request.get('machine_ids') or []
        if not machine_ids:
            raise PlanError('choose at least one machine')
        if push:
            # rsync runs on this machine's agent
            self.orca.link('local')
        entries = []
        for machine_id in machine_ids:
            link = self.target(machine_id)
            push_key = 'push-' + machine_id
            if push:
                entries.append({
                    'key': push_key, 'label': 'Push to ' + link.machine.label,
                    'machine_id': 'local', 'task': True, 'after': [],
                    'start_delay_s': 0, 'cwd': self.localWorkspace(),
                    'argv': self.pushArgv(link, request.get('delete', True)),
                })
            if build:
                entries.append({
                    'key': 'build-' + machine_id,
                    'label': 'Build on ' + link.machine.label,
                    'machine_id': machine_id, 'task': True,
                    'after': [push_key] if push else [], 'start_delay_s': 0,
                    'cwd': self.remoteWorkspace(link),
                    'argv': self.buildArgv(link,
                                           request.get('build_options') or {}),
                })
        return {'processes': entries}

    def deploy(self, request: dict) -> Run:
        plan = self.plan(request)
        names = ', '.join(self.orca.link(machine_id).machine.label
                          for machine_id in request['machine_ids'])
        verb = {(True, True): 'Deploy to', (True, False): 'Push to',
                (False, True): 'Build on'}[(request.get('push', True),
                                            request.get('build', True))]
        return self.startRun('%s %s' % (verb, names), plan, 'deploy')

    def startRun(self, label: str, plan: dict, kind: str) -> Run:
        run = Run(self.orca, newRunId(), label, None, plan, kind=kind)
        self.orca.runs[run.run_id] = run
        self.orca.forgetOldRuns()
        run.start()
        return run

    # --- Bags ---

    def localBags(self) -> dict:
        '''Sizes of the bags already in this workspace, by name.'''
        directory = os.path.join(self.localWorkspace(), BAG_DIRECTORY)
        sizes = {}
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                path = os.path.join(directory, name)
                if os.path.isdir(path):
                    sizes[name] = directorySize(path)
        return sizes

    async def listBags(self, machine_id: str) -> dict:
        link = self.orca.link(machine_id)
        if not link.machine.workspace:
            raise PlanError('%s has no workspace' % link.machine.label)
        listing = await link.call('bags.list',
                                  {'workspace': link.machine.workspace})
        local = {} if link.machine.is_local else self.localBags()
        for bag in listing['bags']:
            if link.machine.is_local:
                bag['pulled'] = 'here'
            elif bag['name'] not in local:
                bag['pulled'] = 'no'
            else:
                bag['pulled'] = ('yes' if local[bag['name']]
                                 == bag['size_bytes'] else 'partial')
        return listing

    def pullBags(self, machine_id: str, names: list) -> Run:
        link = self.target(machine_id)
        if not names:
            raise PlanError('choose at least one bag')
        destination = os.path.join(self.localWorkspace(), BAG_DIRECTORY)
        os.makedirs(destination, exist_ok=True)
        remote_directory = self.remoteWorkspace(link) + '/' + BAG_DIRECTORY
        sources = [self.remotePath(link, remote_directory + '/' + name)
                   for name in names]
        argv = (['rsync', '-a', '--partial', '--info=stats1,progress2', '-e',
                 self.rsyncShell(link)] + sources + [destination + '/'])
        label = 'Pull %d bag%s from %s' % (len(names),
                                           '' if len(names) == 1 else 's',
                                           link.machine.label)
        plan = {'processes': [{
            'key': 'pull', 'label': label, 'machine_id': 'local',
            'task': True, 'after': [], 'start_delay_s': 0,
            'cwd': self.localWorkspace(), 'argv': argv}]}
        return self.startRun(label, plan, 'pull')

    async def deleteBag(self, machine_id: str, name: str) -> dict:
        '''Delete a bag on a machine, only once an identical copy is here.'''
        listing = await self.listBags(machine_id)
        bag = next((bag for bag in listing['bags'] if bag['name'] == name),
                   None)
        if bag is None:
            raise PlanError('no bag ' + name)
        if bag['pulled'] != 'yes':
            raise PlanError("%s hasn't been pulled completely; pull it first"
                            % name)
        link = self.orca.link(machine_id)
        return await link.call('bags.delete', {
            'workspace': link.machine.workspace, 'name': name})



# --- Helpers ------------------------------------------------------------------



def directorySize(path: str) -> int:
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def summarizeItemized(output: str) -> dict:
    '''Parse `rsync --itemize-changes` lines: "*deleting   path",
    ">f.st...... path" (file sent), "cd+++++++++ path/" (directory made).'''
    changes = []
    for line in output.splitlines():
        if line.startswith('*deleting'):
            changes.append({'change': 'delete',
                            'path': line.split(None, 1)[1]})
        elif len(line) > 12 and line[0] in '<>c' and line[1] == 'f':
            is_new = line[2:11].startswith('+++')
            changes.append({'change': 'new' if is_new else 'update',
                            'path': line[12:]})
    # Git's own files (objects, refs, logs) are counted but listed last: the
    # code changes are what a person wants to read.
    for change in changes:
        change['is_git'] = '/.git/' in '/' + change['path']
    changes.sort(key=lambda change: change['is_git'])
    code = [change for change in changes if not change['is_git']]

    def count(entries, kind):
        return len([change for change in entries if change['change'] == kind])

    return {'num_sent': len(changes) - count(changes, 'delete'),
            'num_deleted': count(changes, 'delete'),
            'num_code_new': count(code, 'new'),
            'num_code_updated': count(code, 'update'),
            'num_code_deleted': count(code, 'delete'),
            'num_git': len(changes) - len(code),
            # Git's own files always differ a little (each side's index caches
            # its own file timestamps); the code is what has to match.
            'is_up_to_date': not code,
            'changes': changes[:MAX_LISTED_CHANGES],
            'num_unlisted': max(0, len(changes) - MAX_LISTED_CHANGES),
            'compared_s': time.time()}
