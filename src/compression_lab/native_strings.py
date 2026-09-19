"""Exact-dataset native qualification and exports using the strings-v1 driver.

This is an opt-in commission. Legacy file-interface commissions are unchanged.
Source builds have no dataset mount; all automatic fitting belongs in lab_encode.
"""
from __future__ import annotations
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

from . import candidate, runner, strings
from .util import Error, canonical, digest, fsync_dir, load, lock, rel, safe, save, sha


def init(workspace, dataset, inputs, *, row_framing='lf', implementation='from_scratch',
         cpu=None, standard_libraries=None, workload=None):
    """Provision a fresh native commission using this installed Lab package."""
    from . import __version__
    from .benchmark_run import write_metadata
    from .instructions import native_prompt
    from .native_workloads import variants as workload_variants
    root=Path(workspace).resolve()
    if root.exists() and any(root.iterdir()):raise Error('native_workspace_not_empty')
    if not isinstance(dataset,str) or not dataset.strip():raise Error('native_dataset_required')
    if implementation not in ('open','from_scratch'):raise Error('native_invalid_implementation')
    if row_framing not in strings.ROW_FRAMINGS:raise Error('strings_invalid_row_framing')
    variants=workload_variants(workload,row_framing)
    cpu=min(os.sched_getaffinity(0)) if cpu is None else cpu
    if cpu not in os.sched_getaffinity(0):raise Error('strings_cpu_unavailable')
    inputs=[Path(path).resolve() for path in inputs]
    if not inputs or len({path.name for path in inputs})!=len(inputs):raise Error('strings_invalid_columns')
    for path in inputs:
        if not path.is_file():raise Error('native_input_required',str(path))
    libraries={name:dict(path=str(Path(path).resolve()),sha256=sha(path),standard_codec=True)
               for name,path in (standard_libraries or {}).items()}
    if libraries and implementation!='open':raise Error('native_standard_libraries_require_open')
    compiler=shutil.which('g++')
    if not compiler:raise Error('blocked_toolchain','g++')
    root.mkdir(parents=True,exist_ok=True);runtime=root/'runtime';runtime.mkdir()
    sources={}
    for name in ('driver.cpp','codec.h'):
        target=runtime/name;shutil.copyfile(Path(__file__).parent/'data/strings'/name,target)
        target.chmod(0o444);sources[name]=target
    command=[compiler,'-std=c++17','-O3','-DNDEBUG',str(sources['driver.cpp']),'-ldl','-o',str(runtime/'driver')]
    started=time.monotonic()
    built=subprocess.run(command,capture_output=True,text=True,timeout=300)
    save(runtime/'build.json',dict(toolkit_version=__version__,argv=command,returncode=built.returncode,
         stdout=built.stdout,stderr=built.stderr,data_independent_seconds=time.monotonic()-started,
         compiler={'path':str(Path(compiler).resolve()),'sha256':sha(compiler)}),0o444)
    if built.returncode:raise Error('native_driver_build_failed',built.stderr[-2000:])
    (runtime/'driver').chmod(0o555)
    receipt=strings.init(root,[(path.name,path) for path in inputs],runtime/'driver',libraries,cpu,
                         row_framing=row_framing,driver_sources=sources)
    c=strings.config(root)
    commission=dict(contract='native-exact-dataset-v1',dataset=dataset,
        original_bytes=receipt['original_bytes'],columns=len(inputs),implementation=implementation,
        variants=variants,objectives=c['primary_objectives'],workbench=str(root/'workbench'),
        public_inputs=c['columns'],reference_docs=['INSTRUCTIONS.md','workbench/codec.h','references.json'])
    save(root/'commission.json',commission,0o444)
    save(root/'references.json',{'results':{},'bulk':[],'rows':[]},0o444)
    (root/'hypotheses').mkdir();(root/'native-evidence').mkdir()
    shutil.copyfile(sources['codec.h'],root/'workbench/codec.h')
    save(root/'workbench/manifest-template.json',template(variant=variants[0]))
    (root/'INSTRUCTIONS.md').write_text(native_prompt(c,commission));(root/'INSTRUCTIONS.md').chmod(0o444)
    write_metadata(root,protocol='strings-v1',inputs={path.name:path for path in inputs},cpu=cpu,dataset=dataset,
        parameters={key:c[key] for key in ('row_framing','trials','warmups','seed','memory_bytes','timeout_seconds',
                                          'selectivities','full_decode','row_decode')},
        artifacts=[runtime/'driver',*sources.values(),*(row['path'] for row in libraries.values())])
    return {**receipt,'commission':commission,'instructions':str(root/'INSTRUCTIONS.md')}


def template(name='my-codec', variant='bulk'):
    return dict(schema_version=1, name=name, variant=variant,
                source_paths=['encoder.cpp', 'decoder.cpp', 'codec.h', 'ALGORITHM.md'],
                encoder='encoder.so', decoder='decoder.so',
                build_commands=[['g++', '-std=c++20', '-O3', '-DNDEBUG', '-fPIC', '-shared',
                                 '{source}/'+role+'.cpp', '-o', '{build}/'+role+'.so']
                                for role in ('encoder', 'decoder')],
                hypothesis_id='replace-with-recorded-hypothesis-id',
                algorithm_spec='ALGORITHM.md',
                fitting='All automatic dataset-dependent work is inside lab_encode.')


def record_hypothesis(root, statement, expected_benefit, falsifier):
    row=dict(statement=statement, expected_benefit=expected_benefit,
             falsifier=falsifier, recorded_epoch=time.time())
    if any(not isinstance(x,str) or not x.strip() for x in (statement,expected_benefit,falsifier)):
        raise Error('native_hypothesis_required')
    identity='h-'+digest(row)
    save(Path(root)/'hypotheses'/(identity+'.json'),row,0o444)
    return {'hypothesis_id':identity}


def snapshot(root, manifest, output):
    m=load(manifest); expected=set(template())
    if set(m)!=expected or m['schema_version']!=1 or m['variant'] not in ('bulk','rows'):
        raise Error('native_invalid_manifest')
    if not isinstance(m['name'],str) or not m['name'].replace('-','').replace('_','').isalnum():
        raise Error('native_invalid_name')
    if not isinstance(m['source_paths'],list) or len(set(m['source_paths']))!=len(m['source_paths']):
        raise Error('native_invalid_sources')
    if m['algorithm_spec'] not in m['source_paths'] or not m['fitting'].strip():
        raise Error('native_algorithm_description_required')
    hypothesis=load(safe(Path(root)/'hypotheses',m['hypothesis_id']+'.json'))
    if m['hypothesis_id']!='h-'+digest(hypothesis):raise Error('native_hypothesis_changed')
    if m['encoder']==m['decoder']:raise Error('native_separate_decoder_required')
    for role in ('encoder','decoder'):rel(m[role])
    if not m['build_commands']:raise Error('native_build_required')
    for cmd in m['build_commands']:
        if (not isinstance(cmd,list) or not cmd or cmd[0] not in ('gcc','g++') or
            any(not isinstance(arg,str) or '\x00' in arg for arg in cmd)):
            raise Error('native_direct_compiler_commands_required')
    output=Path(output);output.mkdir(); inventory={}
    total=0
    for name in m['source_paths']:
        p=safe(Path(manifest).parent,name);total+=p.stat().st_size
        if total>128*1024**2:raise Error('native_source_too_large')
        target=output/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(p,target);target.chmod(0o444)
        inventory[name]={'sha256':sha(target),'bytes':target.stat().st_size}
    save(output/'NATIVE_MANIFEST.json',m,0o444)
    return m,dict(source_files=inventory,manifest=m,hypothesis=hypothesis)


def build(c, source, m, output, diagnostic=False):
    output=Path(output);output.mkdir();logs=[]
    libs=output.parent/('build-libraries-'+output.name);libs.mkdir()
    for name,row in c['libraries'].items():
        shutil.copyfile(row['path'],libs/name)
    for command in m['build_commands']:
        args=[a.replace('{source}','/source').replace('{build}','/output').replace('{libraries}','/candidate')
              for a in command]
        if diagnostic:args+=['-fsanitize=undefined','-fno-sanitize-recover=all']
        logs.append(runner.execute(args,output=output,readonly={'/source':source,'/candidate':libs},
            build=True,mode='required',timeout=300,memory=c['memory_bytes'],
            output_limit=512*1024**2,cpus=[c['cpu']]))
    binaries={role:safe(output,m[role]) for role in ('encoder','decoder')}
    for p in binaries.values():candidate.elf(p)
    if diagnostic:
        for p in binaries.values():
            if not any(x.startswith('libubsan.so') for x in candidate.elf(p)['needed']):
                raise Error('native_diagnostics_not_instrumented')
    return binaries,logs


def qualify(root, manifest, quick=False):
    root=Path(root);c=strings.config(root);work=root/'native-evidence'/uuid.uuid4().hex
    work.mkdir(parents=True);save(work/'ATTEMPT.json',{'manifest_path':str(manifest),'quick':quick})
    try:
        m,provenance=snapshot(root,manifest,work/'source')
        if (root/'commission.json').exists() and m['variant'] not in load(root/'commission.json')['variants']:
            raise Error('native_variant_not_commissioned')
        first,logs=build(c,work/'source',m,work/'build')
        identity={role:sha(p) for role,p in first.items()}
        provenance.update(build_logs=logs,compiler={name:dict(path=shutil.which(name),sha256=sha(Path(shutil.which(name))))
                          for name in {cmd[0] for cmd in m['build_commands']}},
                          binaries=identity,source_digest=digest(provenance['source_files']),
                          data_dependent_build_allowed=False)
        if not quick:
            second,rebuild_logs=build(c,work/'source',m,work/'rebuild')
            if {role:sha(p) for role,p in second.items()}!=identity:raise Error('native_build_not_reproducible')
            provenance.update(reproducible=True,rebuild_logs=rebuild_logs)
            instrumented,diagnostic_logs=build(c,work/'source',m,work/'diagnostic',diagnostic=True)
            diagnostic_root=work/'diagnostic-workload';diagnostic_root.mkdir()
            for name in ('evidence','results'):(diagnostic_root/name).mkdir()
            dc=copy.deepcopy(c)
            for name,p in candidate.libraries().items():
                if name.startswith('libubsan.so'):
                    dc['libraries'][name]={'path':str(p),'sha256':sha(p),'standard_codec':False}
            save(diagnostic_root/'strings.json',dc)
            dm=work/'diagnostic'/'candidate.json'
            save(dm,{k:m[k] for k in ('name','variant','encoder','decoder')})
            diagnostic=strings.evaluate(diagnostic_root,dm,quick=True)
            if not diagnostic['quality_passed']:raise Error('native_valid_data_diagnostic_failed',diagnostic.get('error',''))
            provenance.update(diagnostics={'kind':'valid-corpus-ubsan','result_id':diagnostic['result_id'],
                'quality_passed':True,'roundtrips':diagnostic['roundtrips'],
                'selective_checks':diagnostic['selective_checks'],'build_logs':diagnostic_logs})
        save(work/'PROVENANCE.json',provenance,0o444)
        native=work/'build'/'candidate.json'
        save(native,{k:m[k] for k in ('name','variant','encoder','decoder')})
        measured=strings.evaluate(root,native,quick)
        result={k:v for k,v in measured.items() if k!='result_id'}
        result.update(native_source=str(work/'source'),native_provenance=str(work/'PROVENANCE.json'),
                      native_provenance_sha256=sha(work/'PROVENANCE.json'),
                      source_digest=provenance['source_digest'],
                      measured_result_id=measured['result_id'],
                      eligible=bool(not quick and measured['quality_passed']),
                      research_certification='Native exact-dataset qualification; row-access mechanism and scientific claims require source review.',
                      row_access_review='source review required' if m['variant']=='rows' else 'not applicable')
        result['result_id']='s-'+digest(result)
        save(root/'results'/(result['result_id']+'.json'),result,0o444)
        save(work/'RESULT.json',{'result_id':result['result_id']},0o444)
        return result
    except Exception as exc:
        save(work/'FAILURE.json',{'error':str(exc),'reason_code':getattr(exc,'code',type(exc).__name__)})
        raise


def result(root,rid):
    strings.compare(root,[rid])
    row=load(Path(root)/'results'/(rid+'.json'))
    if row['workload_digest']!=digest(strings.config(root)):raise Error('native_workload_changed')
    if not row.get('eligible'):raise Error('native_full_qualified_result_required')
    provenance=Path(row['native_provenance'])
    if sha(provenance)!=row['native_provenance_sha256']:raise Error('native_provenance_changed')
    p=load(provenance);source=Path(row['native_source'])
    for name,pin in p['source_files'].items():
        if sha(safe(source,name))!=pin['sha256']:raise Error('native_source_changed')
    for role,h in row['candidate'].items():
        if sha(Path(row['evidence'])/(role+'.so'))!=h:raise Error('native_binary_changed')
    for col in row['columns']:
        archive=Path(row['evidence'])/'archives'/(col['sha256']+'.bin')
        records=[json.loads(x) for x in (Path(row['evidence'])/'trials.jsonl').read_text().splitlines()]
        expected=next(x['archive_sha256'] for x in records if x['column']==col['name'])
        if archive.stat().st_size!=col['archive_bytes'] or sha(archive)!=expected:raise Error('native_archive_changed')
    return row,p


def _verify_export(path, descriptor):
    """Check the bundle against the qualified evidence before returning it."""
    try:
        with zipfile.ZipFile(path) as archive:
            expected = [*descriptor['files'], 'EXPORT_MANIFEST.json']
            if sorted(archive.namelist()) != sorted(expected):
                raise Error('native_export_changed', 'Export members do not match qualified evidence')
            if archive.read('EXPORT_MANIFEST.json') != canonical(descriptor):
                raise Error('native_export_changed', 'Export manifest does not match qualified evidence')
            for name, pin in descriptor['files'].items():
                if archive.getinfo(name).file_size != pin['bytes']:
                    raise Error('native_export_changed', name)
                with archive.open(name) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != pin['sha256']:
                        raise Error('native_export_changed', name)
    except (zipfile.BadZipFile, OSError, KeyError, ValueError, RuntimeError, EOFError) as exc:
        raise Error('native_export_changed', str(exc)) from exc


def export(root,rid):
    root=Path(root);row,p=result(root,rid);c=strings.config(root)
    dest=root/'exports'/(rid+'.zip');dest.parent.mkdir(exist_ok=True)
    files={'result.json':root/'results'/(rid+'.json'),
           'PROVENANCE.json':Path(row['native_provenance']),
           'driver':Path(c['driver']['path']),
           'codec.h':Path(__file__).parent/'data/strings/codec.h',
           'driver.cpp':Path(__file__).parent/'data/strings/driver.cpp'}
    for name,source in c['driver'].get('sources',{}).items():files[name]=Path(source['path'])
    for file in Path(row['native_source']).rglob('*'):
        if file.is_file():files['source/'+str(file.relative_to(row['native_source']))]=file
    for file in Path(row['evidence']).rglob('*'):
        if file.is_file():files['measured/'+str(file.relative_to(row['evidence']))]=file
    for dep in row['dependencies']:
        if sha(Path(dep['path']))!=dep['sha256']:raise Error('native_dependency_changed')
        if not dep['platform']:files['decoder-libraries/'+dep['soname']]=Path(dep['path'])
    inventory={name:dict(sha256=sha(file),bytes=file.stat().st_size) for name,file in files.items()}
    descriptor={'result_id':rid,'workload_digest':row['workload_digest'],'files':inventory,
                'row_framing':c.get('row_framing','lf'),
                'accounting':row['accounting'],'decoder':'measured/decoder.so',
                'encoder':'measured/encoder.so','same_measured_native_variant':True,
                'platform_libraries':[x for x in row['dependencies'] if x['platform']],
                'columns':[{k:x[k] for k in ('name','sha256','bytes','rows')} for x in row['columns']],
                'command':'LD_LIBRARY_PATH=decoder-libraries ./driver measured/decoder.so decode measured/archives/<column-sha256>.bin output.bin <original-column-bytes-plus-32> unused',
                'row_access_review':row['row_access_review']}
    with lock(dest.with_suffix('.lock')):
        if dest.exists():
            _verify_export(dest,descriptor)
        else:
            # Publish only a complete, verified ZIP. Failed writes leave no final
            # archive that a subsequent export could mistake for success.
            with tempfile.TemporaryDirectory(prefix='.'+rid+'-',dir=dest.parent) as tmp:
                pending=Path(tmp)/dest.name
                with zipfile.ZipFile(pending,'x',compression=zipfile.ZIP_DEFLATED) as z:
                    for name,file in files.items():z.write(file,name)
                    z.writestr('EXPORT_MANIFEST.json',canonical(descriptor))
                _verify_export(pending,descriptor)
                pending.chmod(0o444)
                with pending.open('rb') as stream:os.fsync(stream.fileno())
                os.replace(pending,dest);fsync_dir(dest.parent)
    return {'result_id':rid,'path':str(dest),'sha256':sha(dest),'eligible':True}


def submit(root,manifest,quick=False):
    root=Path(root);jobs=root/'jobs';jobs.mkdir(exist_ok=True)
    with (jobs/'submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        active=jobs/'active.json'
        if active.exists():
            previous=strings.status(root,load(active)['job_id'])
            if previous['status'] in ('queued','running'):return {**previous,'already_running':True}
        job=uuid.uuid4().hex;folder=jobs/job;folder.mkdir()
        save(folder/'request.json',{'manifest':str(Path(manifest).resolve()),'quick':bool(quick)})
        with (folder/'worker.log').open('wb') as log:
            process=subprocess.Popen([sys.executable,'-m','compression_lab.native_strings',str(root),job],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
        state={'job_id':job,'pid':process.pid,'process_start':strings._process_identity(process.pid),'status':'queued'}
        save(folder/'state.json',state);save(active,{'job_id':job});return state


def work(root,job):
    root=Path(root);folder=root/'jobs'/job
    with (root/'jobs/submit.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        state=load(folder/'state.json');state['status']='running';save(folder/'state.json',state)
    request=load(folder/'request.json')
    try:
        row=qualify(root,request['manifest'],request['quick'])
        state.update(status=row['status'],result_id=row['result_id'],eligible=row['eligible'],
                     quality_passed=row['quality_passed'],reason_code=row.get('reason_code'))
    except Exception as exc:state.update(status='failed',reason_code=getattr(exc,'code',type(exc).__name__),error=str(exc))
    save(folder/'state.json',state)


if __name__=='__main__':work(sys.argv[1],sys.argv[2])
