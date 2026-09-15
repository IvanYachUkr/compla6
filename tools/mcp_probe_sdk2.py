#!/usr/bin/env python3
"""Read-only MCP SDK 2 client probe inside the isolated Prime runtime. No model call."""
import asyncio
import argparse
import importlib.metadata
import json
import os
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--quick-candidate', help='Optional public source path: register and verify one quick worker job')
args = parser.parse_args()


async def main():
    report = {'model_calls': 0, 'uid': os.geteuid(), 'client_sdk': importlib.metadata.version('mcp')}
    token = os.environ['COMPRESSION_LAB_TOKEN']
    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}) as http:
        async with streamable_http_client('http://127.0.0.1:8766/mcp', http_client=http) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                report['initialize'] = initialized.model_dump()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert not names & {'freeze', 'evaluate_private', 'disclose'}
                report['tools'] = sorted(names)
                report['calls'] = {}
                for name in ('profile', 'status', 'resume'):
                    result = await session.call_tool(name, {})
                    record = result.model_dump()
                    assert not record['is_error'], record
                    value = record.get('structured_content') or json.loads(record['content'][0]['text'])
                    report['calls'][name] = {'status': value['status'], 'run_id': value['run_id']}
                    if name == 'profile':
                        assert value['metrics']['objects'] == 469, value
                        report['objects'] = value['metrics']['objects']
                if args.quick_candidate:
                    async def call(name, arguments):
                        result = (await session.call_tool(name, arguments)).model_dump()
                        assert not result['is_error'], result
                        return result.get('structured_content') or json.loads(result['content'][0]['text'])
                    registered = await call('register', {'candidate_path': args.quick_candidate})
                    queued = await call('evaluate', {'candidate_digest': registered['candidate_digest'], 'depth': 'quick'})
                    async with asyncio.timeout(90):
                        while True:
                            result = await call('status', {'job_id': queued['job_id']})
                            if result['status'] not in ('queued', 'running'):
                                break
                            await asyncio.sleep(.5)
                    assert result['metrics']['quality_passed'], result
                    report['quick_worker'] = {'status': result['status'], 'quality_passed': True,
                        'result_id': result['metrics']['result_id'], 'job_id': queued['job_id'],
                        'registration_cache_hit': registered['metrics']['registration_cache_hit']}
    report['private_access_denied'] = {}
    for name, path in {
        'historical_user_workspace': '/home/vanya/Documents/LLM-Text-Compression-Research/docs/RESEARCH_CONTRACT.md',
        'evaluator_credentials': '/var/lib/compression-lab/compression-evaluator/mcp-token',
        'public_evaluator_ledger': str(Path.cwd() / 'state/state.json'),
        'trusted_engine_source': '/opt/compression-lab-0.2.1/venv/lib/python3.12/site-packages/compression_lab/owner.py',
    }.items():
        try:
            with open(path, 'rb'):
                pass
        except (FileNotFoundError, PermissionError):
            report['private_access_denied'][name] = True
        else:
            raise AssertionError('Agent namespace exposed ' + name)
    report['status'] = 'passed'
    print(json.dumps(report), flush=True)


asyncio.run(main())
