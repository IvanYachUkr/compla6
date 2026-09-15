#!/usr/bin/env python3
"""Reproducible synthetic SMOKE, all full gates, unchanged scoring, no model calls.

Run from an installed checkout: python tools/benchmark_redesign.py --output /new/path
The default order, data, policies and CRC ablation are fixed before measurements.
Every result, including failures, is retained. This is NOT private-test evidence.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import statistics
import struct
import tempfile
import time
from pathlib import Path

from compression_lab import baselines, candidate, dataset, runner
from compression_lab.engine import Engine, doctor
from compression_lab.stream import ORIGINAL_MAGIC, pack_stream
from compression_lab.util import Error, load, lock, now, save, sha

SEED = 'compression-lab-redesign-smoke-v1-20260907'
SCREENS = ['stored','zstd--3','zstd-1','zstd-3','zstd-9','zstd-1-dict','zstd-3-dict','lz4-0','brotli-4','xz-3']
FULL = ['reference-crc-0-2-2','zstd-1','stored','lz4-0','brotli-4','xz-3','zstd-1-shuffle4','lz4-0-shuffle4','zstd-1-static']


def generate(root: Path, object_bytes: int):
    root.mkdir(); records=[]
    for split_index, split in enumerate(('train','development')):
        for kind in ('logs','integers','opaque'):
            salt = split_index + 1
            if kind == 'opaque':
                payload = hashlib.shake_256((SEED+split).encode()).digest(object_bytes)
            elif kind == 'integers':
                b=bytearray()
                for i in range((object_bytes+3)//4):b.extend(struct.pack('<I',(0xffff0000 + 17*i + salt*911)&0xffffffff))
                payload=bytes(b[:object_bytes])
            else:
                b=bytearray(); i=0
                while len(b)<object_bytes:
                    b.extend((f'2026-01-{salt:02}T12:{i%60:02}:00Z host=node-{i%17} request={i+salt*987654} '
                              f'status={200 if i%11 else 404} latency={i%307:06}.00 path="/item/{i%101}"\r\n').encode())
                    i+=1
                payload=bytes(b[:object_bytes])
            alias=split+'-'+kind; filename=alias+'.bin'; (root/filename).write_bytes(payload)
            records.append(dict(alias=alias,source_group=alias,split=split,path=filename,
                canonical_bytes=len(payload),canonical_sha256=hashlib.sha256(payload).hexdigest()))
    manifest={'schema_version':1,'objects':records,'provenance':{'generator':SEED,'role':'synthetic-public-smoke','source_groups':'one independent generated object per group, never shared across splits'}}
    save(root/'manifest.json',manifest)
    card=dataset.example_card('redesign-smoke-v1')
    card['scope']='Synthetic mixed logs, little-endian uint32 and deterministic opaque bytes. Public smoke, no independent test.'
    card['search_budget']={'candidate_evaluations':40,'wall_seconds':10800}
    card['screening_policy']={'schema_version':1,'max_objects':3,'max_canonical_bytes':3*object_bytes,'seed':20260907}
    zstd=candidate.libraries().get('libzstd.so.1')
    if zstd:
        card['runtime_scenarios']={'schema_version':1,'profiles':[{'id':'preinstalled-zstd-v1','libraries':[{'soname':'libzstd.so.1','sha256':sha(zstd)}]}]}
    save(root/'card.json',card)
    return root/'card.json'


def reference_source(root: Path):
    old=Path(__file__).resolve().parents[1]/'provenance/original-native/baseline.cpp'
    if not old.is_file():raise Error('missing_original_template',str(old))
    source=baselines.create(root,'zstd',1)
    shutil.copyfile(old,source/'codec.cpp')
    manifest=load(source/'candidate.json')
    for name in ('lab_crc.hpp','lab_transforms.hpp'):
        (source/name).unlink();manifest['source_paths'].remove(name)
    manifest['candidate_id']='zstd-1-crc-0-2-2'
    manifest['hypothesis']={'kind':'historical-implementation-control','statement':'Verbatim original 0.2.2 C++ template; evaluated by the current full engine and current complete accounting. Not a historical score.',
        'original_cpp_sha256':sha(old),'changed_variable':'CRC implementation and its helper overhead; codec API/level/envelope remain identity Zstd level 1.'}
    save(source/'candidate.json',manifest)
    return source


def ablation(engine: Engine, output: Path, reference: dict, improved: dict, trials=7):
    """Balanced external native measurements, NEVER substituted for full scores."""
    output.mkdir(); card,rows=engine.data()
    data=pack_stream(ORIGINAL_MAGIC,[(r['alias'],r['source'].read_bytes()) for r in sorted(rows,key=lambda r:r['alias'])])
    canonical=sum(r['canonical_bytes'] for r in rows)
    identities={}; all_trials=[]
    start=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='compression-lab-ablation-') as td:
        tmp=Path(td); inp=tmp/'original.hbi';inp.write_bytes(data)
        controls={}; sequence=0
        for label,result in [('old',reference),('new',improved)]:
            root=engine.root/'candidates'/result['candidate_digest'];m,reg=candidate.verify(root)
            controls[label]=(root,m,reg);identities[label]={'candidate_digest':result['candidate_digest'],'result_id':result['metrics']['result_id'],'binary_sha256':sha(root/'runtime/codec')}
        with lock(runner.HOST_LOCK,host=True,nonblocking=True):
            def invoke(label,operation,source):
                nonlocal sequence
                sequence+=1;root,m,reg=controls[label];call=tmp/('call-'+str(sequence));call.mkdir();out=call/'stdout'
                cmd=[s.replace('{runtime}','/candidate') for s in m['commands'][operation]]
                measurement=runner.execute(cmd,output=call/'out',readonly={'/candidate':root/'runtime'},
                    runtime_files=candidate.runtime_mounts(reg['dependencies']),stdin=source,stdout=out,
                    timeout=card['limits']['timeout_seconds'],memory=card['limits']['memory_bytes'],mode='required')
                return out,measurement,call
            # Check wire equality first; the same archive is then used by both decoders.
            old,_,oldcall=invoke('old','encode_stream',inp);archive=tmp/'archive.hba';shutil.copyfile(old,archive);shutil.rmtree(oldcall)
            new,_,newcall=invoke('new','encode_stream',inp)
            if old_hash:=sha(archive):
                if old_hash!=sha(new):raise Error('identity_wire_changed')
            archive_sha=sha(new);shutil.rmtree(newcall)
            for phase,source,expected in [('encode',inp,archive_sha),('decode',archive,sha(inp))]:
                for trial in range(trials):
                    order=('old','new') if trial%2==0 else ('new','old')
                    for label in order:
                        out,measurement,call=invoke(label,phase+'_stream',source)
                        actual=sha(out)
                        if actual!=expected:raise Error('ablation_output_mismatch')
                        all_trials.append(dict(control=label,phase=phase,trial=trial+1,sequence=sequence,
                            canonical_bytes=canonical,output_sha256=actual,**measurement))
                        shutil.rmtree(call)
                        save(output/'trials.json',all_trials)
    summary={}
    for phase in ('encode','decode'):
        summary[phase]={}
        for label in ('old','new'):
            speeds=[canonical*1000/t['elapsed_ns'] for t in all_trials if t['control']==label and t['phase']==phase]
            # runner's measured wall includes native startup, sandbox and I/O.
            summary[phase][label]={'median_MBps':statistics.median(speeds),'trial_MBps':speeds}
        summary[phase]['new_over_old_median_speed']=summary[phase]['new']['median_MBps']/summary[phase]['old']['median_MBps']
    report={'schema_version':1,'identities':identities,'wire_identical':True,'archive_sha256':archive_sha,
        'trials_per_control_per_direction':trials,'order':'Alternating old/new versus new/old, fixed in advance',
        'timing_scope':'Required namespace sandbox, fresh native process and I/O; no fsync; all trials retained. Supplemental diagnostic, NOT a scoring replacement.',
        'summary':summary,'diagnostic_wall_seconds':time.monotonic()-start,
        'budget_scope':'Separate smoke-harness diagnostic cost, recorded here; not a search-track evaluation or model charge.'}
    save(output/'summary.json',report)
    return report


def run(output: Path, object_bytes: int, do_ablation: bool):
    if output.exists():raise Error('output_exists','Use a NEW directory; old evidence is never overwritten')
    output.mkdir(parents=True);started=time.monotonic()
    card=generate(output/'inputs',object_bytes)
    plan={'schema_version':1,'created_at':now(),'generator':SEED,'object_bytes':object_bytes,
        'screens':SCREENS,'full':FULL,'ablation_trials_per_control_per_direction':7 if do_ablation else 0,
        'primary_scoring_unchanged':True,'role':'smoke','model_calls':0,'private_objects':0}
    save(output/'PLAN.json',plan,0o444)
    save(output/'doctor.json',doctor());save(output/'release-preflight.json',doctor(True))
    engine=Engine.init(output/'workspace',card);save(output/'inventory.json',engine.inventory())
    save(output/'initial-brief.json',engine.brief())
    attempts=[];full_results={}
    for depth,names in [('screen',SCREENS),('full',FULL)]:
        for name in names:
            print(json.dumps({'starting':name,'depth':depth}),flush=True)
            try:
                if name=='reference-crc-0-2-2':
                    reg=engine.register(reference_source(output/'reference-source'),'baseline')
                    result=engine.evaluate(reg['candidate_digest'],'full','baseline',True)
                else:result=engine.baseline_trial(name,depth,wait=True)
                attempts.append({'recipe_id':name,'depth':depth,'result':result})
                if depth=='full':full_results[name]=result
            except Exception as exc:
                attempts.append({'recipe_id':name,'depth':depth,'error':str(exc),'reason_code':getattr(exc,'code',type(exc).__name__)})
            save(output/'attempts.json',attempts)
    table=[]
    for name,result in full_results.items():
        raw=engine.raw(result['metrics']['result_id']);fixed=raw.get('fixed_costs',{});dev=raw.get('accounting',{}).get('development',{})
        table.append({'recipe_id':name,'candidate_digest':raw['candidate_digest'],'result_id':result['metrics']['result_id'],
            'quality_passed':raw.get('quality_passed',False),'eligible':raw.get('eligible',False),'reason_codes':raw.get('reason_codes'),
            'development_archive_ratio':dev.get('archive_ratio'),'development_actual':dev.get('actual'),'development_projection':dev.get('projection'),
            'fixed_costs':{key:fixed.get(key) for key in ('fixed_bytes','config_bytes','packed_source_bytes','binary_bytes','nonplatform_dependency_bytes')},
            'encode':raw.get('timing',{}).get('encode'),'decode':raw.get('timing',{}).get('decode')})
    ablation_report=None
    if do_ablation and all(name in full_results and full_results[name]['metrics']['quality_passed'] for name in ('reference-crc-0-2-2','zstd-1')):
        ablation_report=ablation(engine,output/'crc-ablation',full_results['reference-crc-0-2-2'],full_results['zstd-1'])
    export_receipts=[]
    for name in ('zstd-1','zstd-1-shuffle4','zstd-1-static'):
        if name in full_results and full_results[name]['metrics']['quality_passed']:
            export_receipts.append(engine.export(full_results[name]['metrics']['result_id'],output/(name+'-export.zip')))
    save(output/'exports.json',export_receipts);save(output/'final-brief.json',engine.brief())
    failures=[x for x in attempts if 'error' in x or not (x['result']['metrics'].get('screen_passed') if x['depth']=='screen' else x['result']['metrics'].get('quality_passed'))]
    result={'schema_version':1,'status':'passed' if not failures else 'completed-with-failures','scope':plan,
        'canonical_bytes':6*object_bytes,'rows':table,'full_quality_passes':sum(r['quality_passed'] for r in table),
        'full_attempts':len(FULL),'screen_attempts':len(SCREENS),'failures':failures,'ablation':ablation_report,
        'wall_seconds':time.monotonic()-started,'search_budgets':engine.state()['budgets'],
        'claim_limit':'Synthetic public smoke only; no eligible/independent-test/best-compressor claim. Distro codecs not rebuilt at upstream latest. Host/model integrations not exercised.'}
    save(output/'SUMMARY.json',result,0o444)
    print(json.dumps({'status':result['status'],'summary':str(output/'SUMMARY.json'),'full_quality_passes':result['full_quality_passes']}),flush=True)
    return 0 if not failures else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--object-bytes',type=int,default=4*1024*1024);parser.add_argument('--no-ablation',action='store_true')
    args=parser.parse_args()
    if not 65536<=args.object_bytes<=16*1024*1024:parser.error('object-bytes must be 64 KiB..16 MiB')
    return run(args.output.absolute(),args.object_bytes,not args.no_ablation)

if __name__=='__main__':raise SystemExit(main())
