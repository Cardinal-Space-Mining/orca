'''
Small HTTP server for the hub's web interface (standard library only).

    GET  /                                  the app
    GET  /static/<path>                     files under web/
    GET  /api/state                         full state snapshot (JSON)
    GET  /api/events                        Server-Sent Events: a "snapshot",
                                            then incremental updates
    POST /api/machines/<id>/reconnect       drop and retry a machine's link

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
from urllib.parse import unquote, urlsplit

from .core import HUB_ROOT, Hub


WEB_ROOT = os.path.join(HUB_ROOT, 'web')
MAX_HEADER_LINES = 100
MAX_BODY_BYTES = 1024 * 1024
KEEPALIVE_PERIOD_S = 15.0

STATUS_TEXT = {200: 'OK', 400: 'Bad Request', 404: 'Not Found',
               405: 'Method Not Allowed', 500: 'Internal Server Error'}

mimetypes.add_type('text/javascript', '.js')
mimetypes.add_type('text/css', '.css')


class Request:

    def __init__(self, method: str, path: str, headers: dict, body: bytes):
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body



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
    return Request(method, unquote(urlsplit(target).path), headers, body)


def writeResponse(writer, status: int, body: bytes, content_type: str):
    head = ('HTTP/1.1 %d %s\r\n'
            'Content-Type: %s\r\n'
            'Content-Length: %d\r\n'
            'Cache-Control: no-cache\r\n'
            'Connection: close\r\n\r\n'
            % (status, STATUS_TEXT.get(status, ''), content_type, len(body)))
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
        return await asyncio.start_server(self.handle, host, port)

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
        if request.method == 'GET' and parts == ['state']:
            writeJson(writer, 200, self.hub.snapshot())
        elif request.method == 'GET' and parts == ['events']:
            await self.streamEvents(writer)
        elif (request.method == 'POST' and len(parts) == 3
              and parts[0] == 'machines' and parts[2] == 'reconnect'):
            if self.hub.reconnect(parts[1]):
                writeJson(writer, 200, {'ok': True})
            else:
                writeError(writer, 404, 'no such machine')
        else:
            writeError(writer, 404, 'not found')

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
