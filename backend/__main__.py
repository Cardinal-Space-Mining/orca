'''
Start Orca (from the workspace root):

    ./src/orca/orca [--port 8740] [--no-browser]
'''

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import webbrowser

from .core import ORCA_ROOT, Orca
from .inventory import InventoryError, inventoryPaths, loadInventory
from .server import HttpServer
from .settings import loadSettings


def openBrowser(url: str):
    """Open the page without the browser's own console messages (crash
    reporter, GPU warnings) ending up in Orca's output."""
    opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
    if shutil.which(opener):
        subprocess.Popen([opener, url], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        webbrowser.open(url)


def detectWorkspace():
    '''Orca normally lives at <workspace>/src/orca.'''
    source_directory = os.path.dirname(ORCA_ROOT)
    workspace_root = os.path.dirname(source_directory)
    if os.path.basename(source_directory) == 'src' \
            and os.path.isdir(os.path.join(workspace_root, 'src')):
        return workspace_root
    return None


def parseArguments():
    parser = argparse.ArgumentParser(
        prog='orca', description='Orca: the CSM robot orchestrator')
    parser.add_argument(
        '--port', type=int, default=None,
        help='web interface port (default: the Settings page\'s, 8740)')
    parser.add_argument(
        '--bind', default='localhost',
        help='address to serve the web interface on (default: localhost only)')
    parser.add_argument(
        '--workspace', default=None,
        help='colcon workspace root (default: the one containing the '
             'orca)')
    parser.add_argument('--no-browser', action='store_true',
                        help="don't open a browser window")
    return parser.parse_args()


async def serve(arguments, workspace_root):
    startup = loadSettings()['startup']
    if arguments.port is None:
        arguments.port = startup['port']
    paths = inventoryPaths(workspace_root)
    orca = Orca(loadInventory(workspace_root), workspace_root,
                paths)
    server = await HttpServer(orca).start(arguments.bind,
                                          arguments.port)
    url = 'http://%s:%d/' % (
        'localhost' if arguments.bind in ('localhost', '127.0.0.1', '0.0.0.0')
        else arguments.bind, arguments.port)
    print('[ORCA]: serving on ' + url, flush=True)
    print('[ORCA]: workspace: ' + (workspace_root or 'none'),
          flush=True)
    for path in paths:
        state = 'found' if os.path.isfile(path) else 'not found'
        print('[ORCA]: inventory %s (%s)' % (path, state), flush=True)
    if not arguments.no_browser and startup['open_browser']:
        openBrowser(url)
    async with server:
        await orca.run()


def moveOldConfig():
    """~/.config/orchestrator (from before the rename to Orca) becomes
    ~/.config/orca."""
    old = os.path.expanduser('~/.config/orchestrator')
    new = os.path.expanduser('~/.config/orca')
    if os.path.isdir(old) and not os.path.exists(new):
        os.rename(old, new)
        print('[ORCA]: moved %s to %s' % (old, new), flush=True)


def main():
    moveOldConfig()
    arguments = parseArguments()
    workspace_root = arguments.workspace or detectWorkspace()
    if workspace_root:
        workspace_root = os.path.abspath(os.path.expanduser(workspace_root))
    try:
        asyncio.run(serve(arguments, workspace_root))
    except InventoryError as error:
        print('[ORCA]: inventory error: %s' % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
