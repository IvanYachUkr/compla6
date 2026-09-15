#!/usr/bin/env python3
"""Portable official-SDK v1 protocol probe. Read-only unless --screen-stored.

No host model/account call. Tokens are read only from the named environment
variable, restricted to loopback HTTP, and are never written to the report.
"""
import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit


def loopback_url(value):
    parsed=urlsplit(value)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.username or parsed.password or parsed.path!='/mcp' or parsed.query or parsed.fragment:
        raise ValueError('Use an explicit http://127.0.0.1:PORT/mcp endpoint; no credentials in URLs')
    if not parsed.port or not 1<=parsed.port<=65535:raise ValueError('A valid explicit loopback port is required')
    return value


def unpack(response):
    if response.isError:raise RuntimeError('MCP tool error: '+str(response.content)[:1200])
    return response.structuredContent or json.loads(response.content[0].text)


async def inspect_session(session,args):
    initialized=await session.initialize();names={t.name for t in (await session.list_tools()).tools}
    needed={'brief','inventory','feedback','baseline_trial','recipe_create','register','evaluate','status','cancel','export'}
    if not needed<=names or names&{'freeze','disclose','evaluate_private'}:raise RuntimeError('Unexpected public tool boundary')
    report={'schema_version':1,'status':'passed','model_calls':0,'uid':os.geteuid(),'client_sdk':importlib.metadata.version('mcp'),
            'protocol_version':initialized.protocolVersion,'tools':sorted(names),'calls':{}}
    for name in ('brief','inventory','status'):
        result=unpack(await session.call_tool(name,{}))
        report['calls'][name]={'status':result['status'],'run_id':result['run_id']}
        if name=='brief':report['calls'][name]['workload']=result['metrics']['workload']
    if args.screen_stored:
        result=unpack(await session.call_tool('baseline_trial',{'recipe_id':'stored','depth':'screen'}))
        job=result.get('job_id')
        try:
            async with asyncio.timeout(args.timeout):
                while result['status'] in ('queued','running'):
                    await asyncio.sleep(.2);result=unpack(await session.call_tool('status',{'job_id':job}))
        except TimeoutError:
            if job:await session.call_tool('cancel',{'job_id':job})
            raise RuntimeError('Screen probe timed out; cancellation requested for '+str(job))
        if not result['metrics'].get('screen_passed') or result['metrics'].get('eligible'):raise RuntimeError('Screen did not satisfy its non-promoting contract: '+json.dumps(result))
        report['screen']={'job_id':job,'result_id':result['metrics']['result_id'],'eligible':False,'screen_passed':True}
    return report


async def run(args):
    from mcp import ClientSession,StdioServerParameters
    if importlib.metadata.version('mcp').split('.')[0]!='1':raise RuntimeError('This probe targets official MCP v1; install the pinned 1.29.1 closure, not the current v2 API')
    if args.workspace:
        from mcp.client.stdio import stdio_client
        params=StdioServerParameters(command=sys.executable,args=['-m','compression_lab.mcp_server','--workspace',str(args.workspace.absolute())])
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:return await inspect_session(session,args)
    from mcp.client.streamable_http import streamablehttp_client
    url=loopback_url(args.http_url);token=os.environ.get(args.token_env,'')
    if len(token)<32:raise ValueError('Missing/short local bearer token in named environment variable')
    async with streamablehttp_client(url,headers={'Authorization':'Bearer '+token}) as (read,write,_):
        async with ClientSession(read,write) as session:return await inspect_session(session,args)


def main():
    p=argparse.ArgumentParser(description=__doc__);group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--workspace',type=Path);group.add_argument('--http-url');p.add_argument('--token-env',default='COMPRESSION_LAB_TOKEN')
    p.add_argument('--screen-stored',action='store_true');p.add_argument('--timeout',type=float,default=120);p.add_argument('--output',type=Path)
    a=p.parse_args()
    if a.output and a.output.exists():p.error('Output exists; choose a new receipt path')
    try:result=asyncio.run(run(a))
    except Exception as exc:result={'status':'failed','error':str(exc),'model_calls':0}
    text=json.dumps(result,indent=2)+'\n'
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text)
    print(text,end='');return 0 if result['status']=='passed' else 1

if __name__=='__main__':raise SystemExit(main())
