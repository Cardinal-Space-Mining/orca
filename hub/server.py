'''
Small HTTP server for the hub's web interface (standard library only).

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
    POST /api/machines/<id>/vm/<action>     start, stop, restart, kill, commit,
                                            backup, restore (a run)
    POST /api/machines/<id>/terminals       {"kind": "shell"|"serial"}
    POST /api/machines/<id>/terminals/<t>/input|resize|close
    GET  /api/inventory                     both inventory files, as written
    POST /api/inventory/<scope>             {"machines": [...]}: save, apply
    GET  /api/config                        launch config actions and presets
    GET  /api/config/preset?action=&preset= one preset, resolved
    POST /api/config/resolve                {"arguments": {key: value}}

Every response except the event stream closes its connection, which keeps the
parser trivial. The server binds to localhost by default: the hub is meant to
be used on the machine it runs on.
'''

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .core import HUB_ROOT, Hub
from .inventory import InventoryError
from .lance_config import ConfigError
from .link import AgentError
from .runs import PlanError
from .scenarios import ScenarioError


WEB_ROOT = os.path.join(HUB_ROOT, 'web')
MAX_HEADER_LINES = 100
MAX_BODY_BYTES = 1024 * 1024
KEEPALIVE_PERIOD_S = 15.0

# Errors a request can cause; reported to the browser as 400 + message.
REQUEST_ERRORS = (PlanError, ConfigError, ScenarioError, AgentError,
                  InventoryError,
                  ConnectionError, asyncio.TimeoutError, ValueError, KeyError)

STATUS_TEXT = {200: 'OK', 400: 'Bad Request', 404: 'Not Found',
               405: 'Method Not Allowed', 500: 'Internal Server Error'}

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



# --- Server -------------------------------------------------------------------



class HttpServer:

    def __init__(self, hub: Hub):
        self.hub = hub

    async def start(self, host: str, port: int):
        '''"localhost" listens on both loopback addresses: browsers often try
        ::1 first and log a refused connection when only 127.0.0.1 answers.'''
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
        if result is None:
            writeError(writer, 404, 'not found')
        else:
            writeJson(writer, 200, result)

    async def answerApi(self, request: Request, parts: list):
        """The JSON answer for an API request, or None if there's no such
        endpoint."""
        hub = self.hub
        method = request.method
        if method == 'GET' and parts == ['state']:
            return hub.snapshot()
        if parts[:1] == ['machines'] and len(parts) >= 3:
            return await self.answerMachine(request, parts[1], parts[2:])
        if method == 'GET' and parts == ['scenarios']:
            scenarios, errors = hub.scenarios()
            return {'scenarios': scenarios, 'errors': errors}
        if method == 'POST' and parts == ['plan']:
            return hub.plan(request.json())
        if method == 'POST' and parts == ['runs']:
            return hub.launch(request.json())
        if method == 'POST' and len(parts) == 3 and parts[0] == 'runs':
            if parts[2] == 'stop':
                await hub.stopRun(parts[1])
                return {'ok': True}
            if parts[2] == 'dismiss':
                hub.dismissRun(parts[1])
                return {'ok': True}
        if parts[:1] == ['config']:
            return self.answerConfig(request, parts[1:])
        if method == 'GET' and parts == ['inventory']:
            return hub.inventory()
        if method == 'POST' and len(parts) == 2 and parts[0] == 'inventory':
            return await hub.saveInventory(parts[1],
                                           request.json()['machines'])
        if method == 'POST' and parts == ['deploy', 'compare']:
            body = request.json()
            return await hub.deployer.compare(body['machine_id'],
                                              body.get('delete', True))
        if method == 'POST' and parts == ['deploy']:
            return hub.deployer.deploy(request.json()).snapshot()
        return None

    async def answerMachine(self, request: Request, machine_id: str,
                            parts: list):
        hub = self.hub
        method = request.method
        if method == 'POST' and parts == ['reconnect']:
            hub.reconnect(machine_id)
            return {'ok': True}
        if parts == ['host-key']:
            link = hub.link(machine_id)
            if method == 'GET':
                return await link.scanHostKey()
            await link.trustHostKey(request.json()['fingerprints'])
            return {'ok': True}
        if method == 'POST' and len(parts) == 2 and parts[0] == 'vm':
            return hub.vm.run(machine_id, parts[1], request.json()).snapshot()
        if method == 'POST' and parts[:1] == ['terminals']:
            return await self.answerTerminal(request, machine_id, parts[1:])
        if parts[:1] == ['bags']:
            if method == 'GET' and len(parts) == 1:
                return await hub.deployer.listBags(machine_id)
            if method == 'POST' and parts[1:] == ['pull']:
                return hub.deployer.pullBags(
                    machine_id, request.json()['names']).snapshot()
            if method == 'POST' and parts[1:] == ['delete']:
                return await hub.deployer.deleteBag(machine_id,
                                                    request.json()['name'])
        if method == 'GET' and parts == ['logs']:
            return {'logs': await hub.listLogs(machine_id)}
        if method == 'GET' and parts == ['logs', 'read']:
            return await hub.readLog(
                machine_id, request.queryValue('path'),
                int(request.queryValue('max_bytes', str(1024 * 1024))))
        if len(parts) == 3 and parts[0] == 'processes':
            process_id = parts[1]
            if method == 'GET' and parts[2] == 'output':
                return hub.processOutput(
                    machine_id, process_id, int(request.queryValue('from', '0')))
            if method == 'POST' and parts[2] == 'stop':
                await hub.stopProcess(machine_id, process_id)
                return {'ok': True}
        return None

    async def answerTerminal(self, request: Request, machine_id: str,
                             parts: list):
        hub = self.hub
        body = request.json()
        if parts == []:
            return await hub.openTerminal(
                machine_id, body.get('kind', 'shell'),
                int(body.get('cols', 100)), int(body.get('rows', 30)))
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
        await hub.terminalCall(machine_id, 'terminal.' + action, params)
        return {'ok': True}

    async def downloadLog(self, request: Request, machine_id: str, writer):
        log = await self.hub.readLog(machine_id, request.queryValue('path'),
                                     20 * 1024 * 1024)
        name = '%s-%s' % (machine_id, os.path.basename(log['path']))
        name = ''.join(character if character.isalnum() or character in '._-'
                       else '_' for character in name)
        writeResponse(writer, 200, log['text'].encode(),
                      'text/plain; charset=utf-8',
                      'Content-Disposition: attachment; filename="%s"\r\n'
                      % name)

    def answerConfig(self, request: Request, parts: list):
        config = self.hub.lanceConfig()
        if config is None:
            raise ConfigError(self.hub.lance_config_error
                              or 'the hub has no workspace')
        if request.method == 'GET' and parts == []:
            return {'actions': config.actions(),
                    'path': config.config_path}
        if request.method == 'GET' and parts == ['preset']:
            return config.resolvePreset(request.queryValue('action'),
                                        request.queryValue('preset'))
        if request.method == 'POST' and parts == ['resolve']:
            return config.resolve(request.json().get('arguments') or {})
        return None

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
        queue = self.hub.subscribe()
        try:
            self.writeEvent(writer, 'snapshot', self.hub.snapshot())
            await writer.drain()
            while True:
                try:
                    event, data = await asyncio.wait_for(
                        queue.get(), KEEPALIVE_PERIOD_S)
                    self.writeEvent(writer, event, data)
                except asyncio.TimeoutError:
                    writer.write(b': keepalive\n\n')
                await writer.drain()
        finally:
            self.hub.unsubscribe(queue)

    @staticmethod
    def writeEvent(writer, event: str, data):
        payload = json.dumps(data, separators=(',', ':'))
        writer.write(('event: %s\ndata: %s\n\n' % (event, payload)).encode())
