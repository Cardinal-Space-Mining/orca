'''
Start the hub:  ./src/csm-hub/csm-hub [--port 8740] [--no-browser]
'''

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import webbrowser

from .core import HUB_ROOT, Hub
from .inventory import InventoryError, inventoryPaths, loadInventory
from .server import HttpServer


DEFAULT_PORT = 8740


def detectWorkspace():
    '''The hub normally lives at <workspace>/src/csm-hub.'''
    source_directory = os.path.dirname(HUB_ROOT)
    workspace_root = os.path.dirname(source_directory)
    if os.path.basename(source_directory) == 'src' \
            and os.path.isdir(os.path.join(workspace_root, 'src')):
        return workspace_root
    return None


def parseArguments():
    parser = argparse.ArgumentParser(
        prog='csm-hub', description='CSM robot management hub')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument(
        '--bind', default='localhost',
        help='address to serve the web interface on (default: localhost only)')
    parser.add_argument(
        '--workspace', default=None,
        help='colcon workspace root (default: the one containing csm-hub)')
    parser.add_argument('--no-browser', action='store_true',
                        help="don't open a browser window")
    return parser.parse_args()


async def serve(arguments, workspace_root):
    paths = inventoryPaths(workspace_root)
    hub = Hub(loadInventory(workspace_root), workspace_root, paths)
    server = await HttpServer(hub).start(arguments.bind, arguments.port)
    url = 'http://%s:%d/' % (
        'localhost' if arguments.bind in ('localhost', '127.0.0.1', '0.0.0.0')
        else arguments.bind, arguments.port)
    print('[CSM HUB]: serving on ' + url, flush=True)
    print('[CSM HUB]: workspace: ' + (workspace_root or 'none'), flush=True)
    for path in paths:
        state = 'found' if os.path.isfile(path) else 'not found'
        print('[CSM HUB]: inventory %s (%s)' % (path, state), flush=True)
    if not arguments.no_browser:
        webbrowser.open(url)
    async with server:
        await hub.run()


def main():
    arguments = parseArguments()
    workspace_root = arguments.workspace or detectWorkspace()
    if workspace_root:
        workspace_root = os.path.abspath(os.path.expanduser(workspace_root))
    try:
        asyncio.run(serve(arguments, workspace_root))
    except InventoryError as error:
        print('[CSM HUB]: inventory error: %s' % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
