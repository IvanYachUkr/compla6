import asyncio
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_engine import data


@unittest.skipUnless(importlib.util.find_spec('mcp'), 'Official MCP SDK unavailable')
class HTTPMCP(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_loopback_handshake_and_rejections(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from compression_lab.engine import Engine
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            engine = Engine.init(root / 'work', data(root / 'data'))
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            token = secrets.token_urlsafe(32)
            url = f'http://127.0.0.1:{port}/mcp'
            with (root / 'server.log').open('wb') as log:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'compression_lab.mcp_server', '--workspace', str(engine.root),
                     '--http-port', str(port), '--bearer-token-env', 'LAB_TEST_TOKEN'],
                    env={**os.environ, 'LAB_TEST_TOKEN': token}, stdout=log, stderr=log)
                try:
                    async with httpx.AsyncClient(timeout=2) as client:
                        for _ in range(100):
                            if process.poll() is not None:
                                self.fail((root / 'server.log').read_text())
                            try:
                                response = await client.post(url, json={})
                                break
                            except httpx.ConnectError:
                                await asyncio.sleep(.05)
                        else:
                            self.fail('MCP server did not start')
                        self.assertEqual(response.status_code, 401)
                        wrong = await client.post(url, json={}, headers={'Authorization': 'Bearer wrong'})
                        self.assertEqual(wrong.status_code, 401)
                        origin = await client.post(url, json={}, headers={
                            'Authorization': 'Bearer ' + token, 'Origin': 'https://untrusted.invalid'})
                        self.assertEqual(origin.status_code, 403)
                    async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + token}) as client, \
                         streamable_http_client(url, http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            self.assertNotIn('freeze', {tool.name for tool in tools.tools})
                            result = await session.call_tool('profile', {})
                            self.assertFalse(result.isError)
                            payload = result.structuredContent or json.loads(result.content[0].text)
                            self.assertEqual(payload['metrics']['objects'], 2)
                finally:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 10)
