#!/usr/bin/env python3
"""HTTP client: TOOL JSON uses the public MCP names and schemas unchanged."""
import asyncio
import json
import os
from pathlib import Path
import sys
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from lab_client import prepare_candidate, unpack


async def dispatch(session, tool, arguments, workspace):
    arguments = dict(arguments)
    if tool == 'register':
        arguments['candidate_path'] = prepare_candidate(workspace, arguments['candidate_path'])
    return unpack(await session.call_tool(tool, arguments))


async def main():
    tool = sys.argv[1]
    arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+os.environ['COMPRESSION_LAB_TOKEN']}, timeout=600) as client:
        async with streamable_http_client(os.environ['COMPRESSION_LAB_URL'], http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await dispatch(session, tool, arguments, Path(__file__).resolve().parent.parent)
                print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__': asyncio.run(main())
