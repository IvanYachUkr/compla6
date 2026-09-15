#!/usr/bin/env python3
"""Exercise deployed client helpers from the isolated agent UID, without a model."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import zipfile
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
sys.path.insert(0, str(Path.cwd()/'workbench'))
from lab_client import LabClient

async def main():
    expected='12f7aa54dadcb172d1fa6370f73d7cae71d8cea1ee611b23bfa1fc85952a7f5e'
    report={'model_calls':0,'uid':os.geteuid()}
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+os.environ['COMPRESSION_LAB_TOKEN']}) as http:
        async with streamable_http_client('http://127.0.0.1:8766/mcp',http_client=http) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                class Bridge:
                    async def call_tool(self,server,tool,arguments):return await session.call_tool(tool,arguments)
                lab=LabClient(Bridge())
                short=await lab.call('baselines')
                report['baseline_response_bytes']={'full':len(json.dumps(lab.last)),'display':len(json.dumps(short))}
                reg=await lab.call('register',candidate_path='workbench/agent/helper-probe')
                assert reg['candidate_digest']==expected,reg
                assert lab.last['metrics']['registration_cache_hit'],reg
                report['cross_uid_registration']={'passed':True,'cache_hit':True,'candidate_digest':expected}
                result='r-004f4e6da05ebb5e85b8d896aee3eb1f22edd08ae995ebbae552431f90d5dc3d'
                exported=await lab.call('export',result_id=result,filename='sol-prefix-helpers-check.zip')
                path=Path(lab.last['metrics']['path']);assert path.stat().st_mode&0o777==0o644
                with zipfile.ZipFile(path) as archive:
                    manifest=json.loads(archive.read('EXPORT_MANIFEST.json'))
                    assert set(archive.namelist())==set(manifest['files'])|{'EXPORT_MANIFEST.json'}
                    for name,record in manifest['files'].items():
                        data=archive.read(name)
                        assert len(data)==record['bytes'] and hashlib.sha256(data).hexdigest()==record['sha256']
                report['agent_can_read_verified_export']={'passed':True,'files_verified':len(manifest['files']),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
                await lab.call('resume')
                report['resume']=lab.last
    denied={}
    for name,path in {'evaluator_ledger':Path.cwd()/'state/state.json','provider_other_uid':Path('/var/lib/compression-lab/compression-evaluator/mcp-token'),'historical_research':Path('/home/vanya/Documents/LLM-Text-Compression-Research/docs/RESEARCH_CONTRACT.md')}.items():
        try:path.read_bytes()
        except (PermissionError,FileNotFoundError):denied[name]=True
        else:raise AssertionError('Unexpected access: '+name)
    report['private_access_denied']=denied;report['status']='passed'
    print(json.dumps(report))

asyncio.run(main())
