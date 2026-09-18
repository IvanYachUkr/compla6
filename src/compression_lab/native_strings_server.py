"""MCP entrypoint for commissioned native-only exact-dataset string research."""
import argparse
import hmac
import os
from pathlib import Path

from . import native_strings as native, native_remote, strings
from .util import Error, load, save, sha


def make_server(root, workbench, port, token):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.auth.provider import AccessToken
    from mcp.server.auth.settings import AuthSettings
    root=Path(root).resolve();workbench=Path(workbench).resolve()
    if len(token)<32:raise Error('http_bearer_token_required')
    c=strings.config(root)
    commission=load(root/'commission.json')
    if commission['contract']!='native-exact-dataset-v1' or c['encode_floor_bytes_per_second'] is not None:
        raise Error('native_commission_mismatch')
    variants=set(commission['variants'])
    if (not variants or not variants <= {'bulk','rows'} or
        (c.get('row_framing','lf')=='none' and 'rows' in variants)):
        raise Error('native_commission_mismatch')
    class Verifier:
        async def verify_token(self,value):
            if hmac.compare_digest(value,token):return AccessToken(token=value,client_id='researcher',scopes=['public'])
            return None
    url=f'http://127.0.0.1:{port}'
    server=FastMCP('Compression Lab native strings',host='127.0.0.1',port=port,
        stateless_http=True,json_response=True,token_verifier=Verifier(),
        auth=AuthSettings(issuer_url=url,resource_server_url=url+'/mcp',required_scopes=['public']))

    @server.tool()
    def brief()->dict:
        """Authoritative exact-dataset commission and native completion rules."""
        fields=('contract','dataset','original_bytes','columns','implementation','variants',
                'objectives','workbench','public_inputs','reference_docs')
        return {'commission':{k:commission[k] for k in fields},'instructions':(root/'INSTRUCTIONS.md').read_text(),
                'completion':load(root/'FINISH.json') if (root/'FINISH.json').exists() else None}

    @server.tool()
    def profile()->dict:
        """Pinned columns, RAM timing boundaries, row workloads, ABI and baselines."""
        return {'protocol':{k:v for k,v in c.items() if v is not None},'references':load(root/'references.json'),
                'api_header':(Path(__file__).parent/'data/strings/codec.h').read_text(),
                'server_benchmark':load(root/'server.json') if (root/'server.json').exists() else None,
                'qualification':'Native full measurements of the exact supplied corpus, independent decoding, clean reproducible builds and valid-corpus UBSan.'}

    @server.tool()
    def manifest_template(name:str='my-codec',variant:str='bulk')->dict:
        """Native C/C++ source manifest; compiler commands produce encoder.so and decoder.so."""
        if variant not in variants:raise Error('native_variant_not_commissioned')
        return native.template(name,variant)

    @server.tool()
    def record_hypothesis(statement:str,expected_benefit:str,falsifier:str)->dict:
        """Record a concise hypothesis before a substantive algorithm change."""
        return native.record_hypothesis(root,statement,expected_benefit,falsifier)

    @server.tool()
    def submit(candidate_path:str,quick:bool=False)->dict:
        """Build and evaluate the exact native source. Full runs can qualify and export directly. Returns an async job ID."""
        if (root/'FINISH.json').exists():raise Error('native_research_closed')
        manifest=(workbench/candidate_path).resolve()
        if not manifest.is_relative_to(workbench):raise Error('candidate_outside_workbench')
        return native.submit(root,manifest,quick)

    @server.tool()
    def status(job_id:str)->dict:
        """Read job progress, errors and its immutable native result ID."""
        return strings.status(root,job_id)

    @server.tool()
    def compare(result_ids:list[str],include_columns:bool=False)->dict:
        """Matched full results: charged size, encoding, fresh/warm decoding and selected rows."""
        comparison=strings.compare(root,result_ids,include_columns)
        comparison['claim_rule']='Compare size and decoding speed from the same configuration and machine; report per-column losses, timing spread and compression-speed tradeoffs.'
        return comparison

    @server.tool()
    def server_benchmark(result_id:str)->dict:
        """Transfer a qualified full-corpus native result and rerun it with matched baselines on the server. Returns an async job ID."""
        if (root/'FINISH.json').exists():raise Error('native_research_closed')
        return native_remote.submit(root,result_id)

    @server.tool()
    def server_status(job_id:str,include_columns:bool=False)->dict:
        """Server progress or matched package, encoding, fresh/warm decoding and selected-row results."""
        return native_remote.status(root,job_id,include_columns)

    @server.tool()
    def export(result_id:str)->dict:
        """Export the actual qualified native libraries, archives, source and measured evidence."""
        return native.export(root,result_id)

    @server.tool()
    def finish(bulk_result:str|None,rows_result:str|None,report_path:str,outcome:str='tradeoff')->dict:
        """Close research with measured native exports and an honest report; qualification alone is not a baseline win."""
        if outcome not in ('tradeoff','negative','claimed_improvement'):raise Error('native_invalid_outcome')
        if (root/'FINISH.json').exists():return load(root/'FINISH.json')
        if (root/'jobs/active.json').exists():
            state=strings.status(root,load(root/'jobs/active.json')['job_id'])
            if state['status'] in ('queued','running'):raise Error('native_job_still_running')
        if (root/'server-jobs/active.json').exists():
            state=native_remote.status(root,load(root/'server-jobs/active.json')['job_id'])
            if state['status'] in ('queued','running','connection_lost'):raise Error('server_job_still_running')
        report=(workbench/report_path).resolve()
        if not report.is_relative_to(workbench) or not report.is_file() or report.stat().st_size==0:
            raise Error('native_report_required')
        selected={'bulk':bulk_result,'rows':rows_result}
        if any(rid and variant not in variants for variant,rid in selected.items()):
            raise Error('native_variant_not_commissioned')
        if outcome!='negative' and any(not selected[variant] for variant in variants):
            raise Error('native_commissioned_variants_required')
        exports=[]
        for variant,rid in (('bulk',bulk_result),('rows',rows_result)):
            if rid:
                row,_=native.result(root,rid)
                if row['variant']!=variant:raise Error('native_variant_mismatch')
                exports.append(native.export(root,rid))
        receipt={'accepted':True,'outcome':outcome,'native_exports':exports,
                 'report_sha256':sha(report),'owner_review_pending':True,
                 'scientific_success':'not certified by completion; evaluate the size/decode tradeoffs and review row-access source'}
        (root/'FINAL_REPORT.md').write_bytes(report.read_bytes())
        save(root/'FINISH.json',receipt,0o444)
        return receipt
    return server


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--workbench',type=Path,required=True)
    parser.add_argument('--port',type=int,required=True)
    a=parser.parse_args()
    make_server(a.workspace,a.workbench,a.port,os.environ.get('COMPRESSION_LAB_TOKEN','')).run(transport='streamable-http')


if __name__=='__main__':main()
