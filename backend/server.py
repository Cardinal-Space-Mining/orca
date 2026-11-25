'''
Small HTTP server for Orca's web interface (standard library only).

    GET  /                                  the app
    GET  /static/<path>                     files under web/
    GET  /api/state                         full state snapshot (JSON)
    GET  /api/events                        Server-Sent Events: a "snapshot",
                                            then incremental updates
    POST /api/machines/<id>/reconnect       drop and retry a machine's link
    GET  /api/machines/<id>/host-key        scan a route's unknown host key
    POST /api/machines/<id>/host-key        trust it {"fingerprints": [...]}
    GET  /api/machines/<id>/processes/<process>/output?from=N
    POST /api/machines/<id>/processes/<process>/stop
    GET  /api/scenarios                     scenarios and file errors
    GET  /api/scenarios/<id>/source         a scenario file's text
    POST /api/scenarios/<id>                {"text"}: check and save it
    POST /api/scenarios/<id>/check          {"text"}: check without saving
    POST /api/scenarios/<id>/delete
    POST /api/plan                          plan a launch (nothing starts)
    POST /api/runs                          launch
    POST /api/runs/<id>/stop
    POST /api/runs/<id>/dismiss             forget a finished run
    POST /api/deploy/compare                {"machine_id"}: what a push changes
    POST /api/deploy                        push and/or build (a run)
    GET  /api/machines/<id>/bags            bags there, and whether pulled
    POST /api/machines/<id>/bags/pull       {"names": [...]} (a run)
    POST /api/machines/<id>/bags/delete     {"name"}, only once pulled
    GET  /api/machines/<id>/logs            log files there
    GET  /api/machines/<id>/logs/read?path=&max_bytes=[&download=1]
    POST /api/machines/<id>/logs/delete     {"path"}
    GET  /api/machines/<id>/cleanup         what could be deleted there
    POST /api/machines/<id>/cleanup         {"categories": [...]}: delete it
    POST /api/machines/<id>/vm/<action>     start, stop, restart, kill, commit,
                                            backup, restore (a run)
    POST /api/machines/<id>/terminals       {"kind": "shell"|"serial"|
                                            "share-install"|"install-key"|
                                            "setup" + {"check"}}
    POST /api/machines/<id>/sharing/up      {"downstream", "upstream",
                                            "subnet", "dns", "lease"}
    POST /api/machines/<id>/sharing/down    {"name"}
    POST /api/machines/<id>/sharing/refresh
    POST /api/machines/<id>/setup/refresh   re-run its setup checklist
    GET  /api/discovery/networks?machine_id= networks worth scanning from it
    POST /api/discovery/scan                {"machine_id", "cidr"}: hosts,
                                            with known machines marked
    POST /api/discovery/host-key            {"address", "user", "port", "via",
                                            "host_key_alias"}: its keys
    POST /api/discovery/trust               the same + {"fingerprints"}
    POST /api/discovery/probe               the same + {"id", "label"}:
                                            connect once, suggest an entry
    POST /api/discovery/add                 {"scope", "entry"}
    POST /api/machines/<id>/terminals/<t>/input|resize|close
    GET  /api/settings, POST /api/settings  Orca settings (competition
                                            mode, budget, startup)
    POST /api/settings/forget               {"what": "sharing"|"addresses",
                                            "machine_id"}
    GET  /api/budget                        bandwidth budget with history
    POST /api/budget/reset                  start a new averaging window
    POST /api/budget/monitor                {"machine_id"} start / {} stop the
                                            net_adapter monitor
    POST /api/machines/<id>/net/ping|tcp|scan
    GET  /api/inventory                     both inventory files, as written
    POST /api/inventory/<scope>             {"machines": [...]}: save, apply
    GET  /api/config                        launch config actions and presets
    GET  /api/config/preset?action=&preset= one preset, resolved
    POST /api/config/resolve                {"arguments": {key: value}}
    POST /api/config/check                  {"action", "preset", "value"}:
                                            what's wrong, what it resolves to
    POST /api/config/save                   the same + {"description"}: write
                                            it into its lance config file
    POST /api/config/delete                 {"action", "preset"}
    POST /api/config/description            {"key", "text"} ("action" or
                                            "action:=preset")

Every response except the event stream closes its connection, which keeps the
parser trivial. The server binds to localhost by default: Orca is
meant to be used on the machine it runs on.

Other web pages open in the same browser can reach localhost too, so API
requests from another origin are refused (Origin must match Host), POSTs must
be JSON (which a cross-site form can't send), and when bound to localhost the
Host must be a loopback name (so a DNS-rebound name can't read answers).
'''

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import sys
import traceback
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .core import ORCA_ROOT, Orca
from .inventory import InventoryError
from .config_edit import ConfigEditor, configFiles, overrideProblems
from .lance_config import ConfigError
from .link import AgentError
from .runs import PlanError
from .scenarios import (ScenarioError, deleteScenario, parseScenarioText,
                        readScenarioSource, writeScenario)


WEB_ROOT = os.path.join(ORCA_ROOT, 'web')
MAX_HEADER_LINES = 100
MAX_BODY_BYTES = 1024 * 1024
KEEPALIVE_PERIOD_S = 15.0

# Errors a request can cause; reported to the browser as 400 + message.
REQUEST_ERRORS = (PlanError, ConfigError, ScenarioError, AgentError,
                  InventoryError,
                  ConnectionError, asyncio.TimeoutError, ValueError, KeyError)

STATUS_TEXT = {200: 'OK', 400: 'Bad Request', 403: 'Forbidden',
               404: 'Not Found', 405: 'Method Not Allowed',
               500: 'Internal Server Error'}

LOOPBACK_NAMES = ('localhost', '127.0.0.1', '::1')

mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/css', '.css')


class Request:

    def __init__(self, method: str, path: str, query: dict, headers: dict,
                 body: bytes):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def json(self) -> dict:
        return json.loads(self.body or b'{}')

    def queryValue(self, name: str, default: str = '') -> str:
        return self.query.get(name, [default])[0]



# --- Parsing and responses ----------------------------------------------------



async def readRequest(reader: asyncio.StreamReader) -> Optional[Request]:
    request_line = (await reader.readline()).decode('latin-1').strip()
    if not request_line:
        return None
    parts = request_line.split()
    if len(parts) != 3:
        return None
    method, target, _ = parts

    headers = {}
    for _ in range(MAX_HEADER_LINES):
        line = (await reader.readline()).decode('latin-1').strip()
        if not line:
            break
        name, _, value = line.partition(':')
        headers[name.strip().lower()] = value.strip()

    length = min(int(headers.get('content-length', '0') or 0), MAX_BODY_BYTES)
    body = await reader.readexactly(length) if length else b''
    parts = urlsplit(target)
    return Request(method, unquote(parts.path), parse_qs(parts.query),
                   headers, body)


def writeResponse(writer, status: int, body: bytes, content_type: str,
                  extra_headers: str = ''):
    head = ('HTTP/1.1 %d %s\r\n'
            'Content-Type: %s\r\n'
            'Content-Length: %d\r\n'
            'Cache-Control: no-cache\r\n'
            '%s'
            'Connection: close\r\n\r\n'
            % (status, STATUS_TEXT.get(status, ''), content_type, len(body),
               extra_headers))
    writer.write(head.encode('latin-1') + body)


def writeJson(writer, status: int, data):
    writeResponse(writer, status, json.dumps(data).encode(),
                  'application/json')


def writeError(writer, status: int, message: str):
    writeJson(writer, status, {'error': message})


def hostName(host: str) -> str:
    '''"localhost:8740" -> "localhost", "[::1]:8740" -> "::1".'''
    return (urlsplit('//' + host).hostname or '').lower()


def foreignRequest(request: Request, loopback_only: bool) -> Optional[str]:
    '''Why a request looks like it comes from another web page, or None.'''
    host = request.headers.get('host', '')
    if loopback_only and hostName(host) not in LOOPBACK_NAMES:
        return 'unexpected Host %r' % host
    origin = request.headers.get('origin')
    if origin is not None and urlsplit(origin).netloc != host:
        return 'requests from %s are not allowed' % origin
    if request.method == 'POST' and not request.headers.get(
            'content-type', '').startswith('application/json'):
        return 'POST bodies must be application/json'
    return None



# --- Server -------------------------------------------------------------------



class HttpServer:

    def __init__(self, orca: Orca):
        self.orca = orca
        self.loopback_only = True

    async def start(self, host: str, port: int):
        '''"localhost" listens on both loopback addresses: browsers often try
        ::1 first and log a refused connection when only 127.0.0.1 answers.'''
        self.loopback_only = host in LOOPBACK_NAMES
        if host != 'localhost':
            return await asyncio.start_server(self.handle, host, port)
        try:
            return await asyncio.start_server(
                self.handle, ['127.0.0.1', '::1'], port)
        except OSError:  # no IPv6 on this machine
            return await asyncio.start_server(self.handle, '127.0.0.1', port)

    async def handle(self, reader, writer):
        try:
            request = await readRequest(reader)
            if request is not None:
                await self.route(request, writer)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        finally:
            writer.close()

    async def route(self, request: Request, writer):
        path = request.path
        if path.startswith('/api/'):
            await self.routeApi(request, writer)
        elif request.method != 'GET':
            writeError(writer, 405, 'method not allowed')
        elif path == '/':
            self.serveFile(writer, 'index.html')
        elif path.startswith('/static/'):
            self.serveFile(writer, path[len('/static/'):])
        else:
            writeError(writer, 404, 'not found')

    async def routeApi(self, request: Request, writer):
        refusal = foreignRequest(request, self.loopback_only)
        if refusal:
            writeError(writer, 403, refusal)
            return
        parts = request.path.strip('/').split('/')[1:]  # drop "api"
        if request.method == 'GET' and parts == ['events']:
            await self.streamEvents(writer)
            return
        try:
            if request.queryValue('download') and parts[2:] == ['logs', 'read']:
                await self.downloadLog(request, parts[1], writer)
                return
            result = await self.answerApi(request, parts)
        except REQUEST_ERRORS as error:
            message = str(error) or type(error).__name__
            if isinstance(error, KeyError):
                message = 'missing ' + message
            writeError(writer, 400, message)
            return
        except Exception as error:  # a bug: say so instead of hanging up
            print('[ORCA]: %s %s failed:' % (request.method, request.path),
                  file=sys.stderr)
            traceback.print_exc()
            writeError(writer, 500, '%s: %s' % (type(error).__name__, error))
            return
        if result is None:
            writeError(writer, 404, 'not found')
        else:
            writeJson(writer, 200, result)

    async def answerApi(self, request: Request, parts: list):
        """The JSON answer for an API request, or None if there's no such
        endpoint."""
        orca = self.orca
        method = request.method
        if method == 'GET' and parts == ['state']:
            return orca.snapshot()
        if parts[:1] == ['machines'] and len(parts) >= 3:
            return await self.answerMachine(request, parts[1], parts[2:])
        if method == 'GET' and parts == ['scenarios']:
            scenarios, errors = orca.scenarios()
            return {'scenarios': scenarios, 'errors': errors}
        if parts[:1] == ['scenarios'] and len(parts) in (2, 3):
            return self.answerScenario(request, parts[1], parts[2:])
        if method == 'POST' and parts == ['plan']:
            return orca.plan(request.json())
        if method == 'POST' and parts == ['runs']:
            return orca.launch(request.json())
        if method == 'POST' and len(parts) == 3 and parts[0] == 'runs':
            if parts[2] == 'stop':
                await orca.stopRun(parts[1])
                return {'ok': True}
            if parts[2] == 'dismiss':
                orca.dismissRun(parts[1])
                return {'ok': True}
        if parts[:1] == ['config']:
            return self.answerConfig(request, parts[1:])
        if parts[:1] == ['discovery'] and len(parts) == 2:
            return await self.answerDiscovery(request, parts[1])
        if method == 'GET' and parts == ['settings']:
            return orca.settings
        if method == 'POST' and parts == ['settings']:
            return await orca.updateSettings(request.json())
        if method == 'POST' and parts == ['settings', 'forget']:
            body = request.json()
            return orca.forget(body['what'], body['machine_id'])
        if method == 'GET' and parts == ['budget']:
            return orca.budget.snapshot(with_history=True)
        if method == 'POST' and parts == ['budget', 'reset']:
            orca.budget.reset()
            return {'ok': True}
        if method == 'POST' and parts == ['budget', 'monitor']:
            body = request.json()
            if body.get('machine_id'):
                return await orca.startMonitor(body['machine_id'])
            return await orca.stopMonitor()
        if method == 'GET' and parts == ['inventory']:
            return orca.inventory()
        if method == 'POST' and len(parts) == 2 and parts[0] == 'inventory':
            return await orca.saveInventory(
                parts[1], request.json()['machines'])
        if method == 'POST' and parts == ['deploy', 'compare']:
            body = request.json()
            return await orca.deployer.compare(
                body['machine_id'], body.get('delete', True))
        if method == 'POST' and parts == ['deploy']:
            return orca.deployer.deploy(request.json()).snapshot()
        return None

    async def answerMachine(self, request: Request, machine_id: str,
                            parts: list):
        orca = self.orca
        method = request.method
        if method == 'POST' and parts == ['reconnect']:
            orca.reconnect(machine_id)
            return {'ok': True}
        if parts == ['host-key']:
            link = orca.link(machine_id)
            if method == 'GET':
                return await link.scanHostKey()
            await link.trustHostKey(request.json()['fingerprints'])
            return {'ok': True}
        if method == 'POST' and len(parts) == 2 and parts[0] == 'net':
            return await self.answerNetTool(machine_id, parts[1],
                                            request.json())
        if method == 'POST' and len(parts) == 2 and parts[0] == 'vm':
            return orca.vm.run(machine_id, parts[1],
                               request.json()).snapshot()
        if method == 'POST' and len(parts) == 2 and parts[0] == 'sharing':
            body = request.json()
            if parts[1] == 'up':
                return await orca.sharing.up(machine_id, body)
            if parts[1] == 'down':
                return await orca.sharing.down(machine_id,
                                               body.get('name'))
            if parts[1] == 'refresh':
                link = orca.link(machine_id)
                await orca.sharing.refresh(link)
                return link.state['sharing'] or {}
        if method == 'POST' and parts == ['setup', 'refresh']:
            link = orca.link(machine_id)
            await orca.setup.refresh(link)
            return link.state['setup']
        if method == 'POST' and parts[:1] == ['terminals']:
            return await self.answerTerminal(request, machine_id, parts[1:])
        if parts[:1] == ['bags']:
            if method == 'GET' and len(parts) == 1:
                return await orca.deployer.listBags(machine_id)
            if method == 'POST' and parts[1:] == ['pull']:
                return orca.deployer.pullBags(
                    machine_id, request.json()['names']).snapshot()
            if method == 'POST' and parts[1:] == ['delete']:
                return await orca.deployer.deleteBag(
                    machine_id, request.json()['name'])
        if method == 'GET' and parts == ['logs']:
            return {'logs': await orca.listLogs(machine_id)}
        if method == 'POST' and parts == ['logs', 'delete']:
            return await orca.deleteLog(machine_id, request.json()['path'])
        if parts == ['cleanup']:
            if method == 'GET':
                return await orca.cleanupScan(machine_id)
            return await orca.cleanup(machine_id,
                                      request.json()['categories'])
        if method == 'GET' and parts == ['logs', 'read']:
            return await orca.readLog(
                machine_id, request.queryValue('path'),
                int(request.queryValue('max_bytes', str(1024 * 1024))))
        if len(parts) == 3 and parts[0] == 'processes':
            process_id = parts[1]
            if method == 'GET' and parts[2] == 'output':
                return await orca.processOutput(
                    machine_id, process_id, int(request.queryValue('from', '0')),
                    request.queryValue('refresh') == '1')
            if method == 'POST' and parts[2] == 'stop':
                await orca.stopProcess(machine_id, process_id)
                return {'ok': True}
        return None

    async def answerNetTool(self, machine_id: str, tool: str, body: dict):
        link = self.orca.link(machine_id)
        if tool == 'ping':
            return await link.call('net.ping', {
                'host': body['host'], 'count': body.get('count', 4)},
                timeout_s=60.0)
        if tool == 'tcp':
            return await link.call('net.tcp', {
                'host': body['host'], 'port': int(body['port'])},
                timeout_s=30.0)
        if tool == 'scan':
            result = await link.call('net.scan', {
                'cidr': body['cidr'], 'ports': body.get('ports') or [22]},
                timeout_s=120.0)
            self.orca.noteScan(machine_id, result)
            return result
        return None

    def answerScenario(self, request: Request, scenario_id: str,
                       action: list):
        workspace = self.orca.workspace_root
        if request.method == 'GET' and action == ['source']:
            return readScenarioSource(workspace, scenario_id)
        if request.method != 'POST':
            return None
        if action == ['check']:
            scenario = parseScenarioText(scenario_id,
                                         request.json().get('text', ''))
            return {'ok': True, 'scenario': scenario}
        if action == ['delete']:
            return {'path': deleteScenario(workspace, scenario_id)}
        if action == []:
            return {'path': writeScenario(workspace, scenario_id,
                                          request.json().get('text', ''))}
        return None

    async def answerDiscovery(self, request: Request, action: str):
        discovery = self.orca.discovery
        if request.method == 'GET' and action == 'networks':
            return {'networks': discovery.networks(
                request.queryValue('machine_id', 'local'))}
        if request.method != 'POST':
            return None
        body = request.json()
        if action == 'scan':
            return await discovery.scan(body.get('machine_id') or 'local',
                                        body['cidr'])
        if action == 'host-key':
            return await discovery.hostKey(body)
        if action == 'trust':
            return await discovery.trustKey(body)
        if action == 'probe':
            return await discovery.probe(body)
        if action == 'add':
            return await discovery.add(body.get('scope'), body['entry'])
        return None

    async def answerTerminal(self, request: Request, machine_id: str,
                             parts: list):
        orca = self.orca
        body = request.json()
        if parts == []:
            return await orca.openTerminal(
                machine_id, body.get('kind', 'shell'),
                int(body.get('cols', 100)), int(body.get('rows', 30)), body)
        if len(parts) != 2:
            return None
        terminal_id, action = parts
        if action == 'input':
            params = {'terminal_id': terminal_id, 'data': body['data']}
        elif action == 'resize':
            params = {'terminal_id': terminal_id, 'cols': int(body['cols']),
                      'rows': int(body['rows'])}
        elif action == 'close':
            params = {'terminal_id': terminal_id}
        else:
            return None
        await orca.terminalCall(machine_id, 'terminal.' + action,
                                params)
        return {'ok': True}

    async def downloadLog(self, request: Request, machine_id: str, writer):
        log = await self.orca.readLog(
            machine_id, request.queryValue('path'), 20 * 1024 * 1024)
        name = '%s-%s' % (machine_id, os.path.basename(log['path']))
        name = ''.join(character if character.isalnum() or character in '._-'
                       else '_' for character in name)
        writeResponse(writer, 200, log['text'].encode(),
                      'text/plain; charset=utf-8',
                      'Content-Disposition: attachment; filename="%s"\r\n'
                      % name)

    def answerConfig(self, request: Request, parts: list):
        config = self.orca.lanceConfig()
        if config is None:
            raise ConfigError(self.orca.lance_config_error
                              or 'Orca has no workspace')
        editor = ConfigEditor(config, self.orca.workspace_root)
        if request.method == 'GET' and parts == []:
            return self.configCatalog(config, editor)
        if request.method == 'GET' and parts == ['preset']:
            return config.resolvePreset(request.queryValue('action'),
                                        request.queryValue('preset'))
        if request.method != 'POST':
            return None
        body = request.json()
        if parts == ['resolve']:
            return config.resolve(body.get('arguments') or {})
        if parts == ['check']:
            return editor.check(body['action'], body['preset'], body['value'])
        if parts == ['save']:
            path = editor.edit(body['action'], body['preset'], body['value'])
            if 'description' in body:
                editor.setDescription('%s:=%s' % (body['action'],
                                                  body['preset']),
                                      body['description'])
            return {'ok': True, 'path': editor.relative(path)}
        if parts == ['delete']:
            path = editor.delete(body['action'], body['preset'],
                                 self.scenarioReferences(body['action'],
                                                         body['preset']))
            return {'ok': True, 'path': editor.relative(path)}
        if parts == ['description']:
            editor.setDescription(body['key'], body.get('text') or '')
            return {'ok': True}
        return None

    def configCatalog(self, config, editor: ConfigEditor) -> dict:
        actions = config.actions()
        merged = config.load()
        descriptions = editor.descriptions()
        sources = {}
        for path, data in configFiles(config):
            for action, block in data.items():
                if isinstance(block, dict) and not action.startswith(
                        'pragma:'):
                    for preset in block:
                        sources.setdefault((action, preset),
                                           editor.relative(path))
        for action in actions:
            action['description'] = descriptions.get(action['name'])
            action['source'] = editor.relative(editor.sourceOf(
                action['name']))
            for preset in action['presets']:
                key = '%s:=%s' % (action['name'], preset['name'])
                preset['description'] = descriptions.get(key)
                preset['source'] = sources.get((action['name'],
                                                preset['name']))
                preset['problems'] = overrideProblems(merged, preset['raw'])
        return {'actions': actions,
                'constants': config.addressConstants(),
                'path': config.config_path,
                'descriptions_path': editor.relative(
                    editor.descriptions_path)}

    def scenarioReferences(self, action: str, preset: str) -> list:
        """Scenarios that can select action:=preset ("{robot}_{arena}"
        counts as matching any name)."""
        def matches(value) -> bool:
            pattern = re.sub(r'\\\{[^}]*\\\}', '.+', re.escape(str(value)))
            return re.fullmatch(pattern, preset) is not None

        scenarios, _ = self.orca.scenarios()
        return ['the scenario "%s" may use it' % scenario['id']
                for scenario in scenarios
                if any(matches(((process.get('lance') or {}).get('args')
                                or {}).get(action, ''))
                       for process in scenario.get('processes', []))]

    def serveFile(self, writer, relative_path: str):
        full_path = os.path.realpath(os.path.join(WEB_ROOT, relative_path))
        # Refuse paths that escape the web directory ("../").
        if not full_path.startswith(os.path.realpath(WEB_ROOT) + os.sep) \
                or not os.path.isfile(full_path):
            writeError(writer, 404, 'not found')
            return
        with open(full_path, 'rb') as file:
            body = file.read()
        content_type = (mimetypes.guess_type(full_path)[0]
                        or 'application/octet-stream')
        if content_type.startswith('text/'):
            content_type += '; charset=utf-8'
        writeResponse(writer, 200, body, content_type)

    async def streamEvents(self, writer):
        writer.write(b'HTTP/1.1 200 OK\r\n'
                     b'Content-Type: text/event-stream\r\n'
                     b'Cache-Control: no-cache\r\n'
                     b'Connection: keep-alive\r\n\r\n')
        queue = self.orca.subscribe()
        try:
            self.writeEvent(writer, 'snapshot', self.orca.snapshot())
            await writer.drain()
            # publish() drops a queue that fell too far behind; closing the
            # stream then makes the browser reconnect and take a snapshot.
            while queue in self.orca.subscribers:
                try:
                    event, data = await asyncio.wait_for(
                        queue.get(), KEEPALIVE_PERIOD_S)
                    self.writeEvent(writer, event, data)
                except asyncio.TimeoutError:
                    writer.write(b': keepalive\n\n')
                await writer.drain()
        finally:
            self.orca.unsubscribe(queue)

    @staticmethod
    def writeEvent(writer, event: str, data):
        payload = json.dumps(data, separators=(',', ':'))
        writer.write(('event: %s\ndata: %s\n\n' % (event, payload)).encode())
