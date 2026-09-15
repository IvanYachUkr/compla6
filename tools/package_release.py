#!/usr/bin/env python3
"""Build one application-root release with an explicit public file selection.

Historical workspaces and input archives remain in the immutable previous release.
Refuses symlinks and existing output; never packages arbitrary host directories.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
INCLUDED={'.dockerignore','README.md','LICENSE','THIRD_PARTY_NOTICES.md','pyproject.toml','MANIFEST.in',
          'requirements-build.txt','requirements-build.lock','requirements-core.txt','requirements-mcp.lock',
          'src','tests','tools','schemas','examples','runtime','wheelhouse','docs','dependencies',
          # Immutable source inputs used by current policy/CRC replays and tests.
          'provenance/controller-before-policy.py','provenance/original-native/baseline.cpp',
          'provenance/research-completion-parent.json','provenance/completion-controls-parent.json','provenance/base-controller-cleanup-051-manifest.json','provenance/prime-integration-inputs.json'}
# Only fresh validation receipts describe 0.6.4.
RELEASE_RESULTS = {'results/current-release.json', 'results/final-wheel.json',
                   'results/terra-and-stream-red.txt', 'results/research-and-supervision-green.txt',
                   'results/budget-and-release-064.txt', 'results/release-tools-064.txt', 'results/release-tools-metadata-red-064.txt', 'results/wheel-build-064.txt'}
EXCLUDED_FILES={'dependencies/observed-runtime.json','dependencies/observed-runtime-release.json'}
PREVIOUS_RELEASE = {'schema_version':1, 'version':'0.6.3',
    'installed_manifest':'/opt/compression-lab-0.6.3/INSTALL_SOURCE_MANIFEST.json',
    'installed_manifest_sha256':'412e7c7e6689ab117ced67bf9358be6255baee1cd2fcedabf128cf3cd9047755',
    'source':'handover/native-rpc-recovery-2026-09-12/release-0.6.3/compression-lab-0.6.3',
    'boundary':'The predecessor and all campaign evidence are preserved. This release clarifies research-completion guidance, drains native RPC output between bounded controller polls and adds Terra budget rates. Scientific gates, size accounting and native compression implementations are unchanged.'}



# Working trees and generated evidence caches never become release inputs.
# ``native`` is excluded only at checkout root: the 0.4 source contract keeps
# ``src/compression_lab/data/native``.
EXCLUDED_ROOTS={'.venv','native','build','downloads','evidence'}
EXCLUDED_PARTS={'.venv','build','downloads','evidence'}


def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def selected_files(root,include_mcp_wheels=True):
    root=Path(root);selected=[]
    for name in sorted(INCLUDED|RELEASE_RESULTS):
        entry=root/name
        for parent in (entry,*entry.parents):
            if parent==root:break
            if parent.is_symlink():raise ValueError('Release input contains a symlink: '+str(parent))
        if name in RELEASE_RESULTS and not entry.is_file():raise ValueError('Missing release validation receipt: '+name)
        if not entry.exists() and not entry.is_symlink():continue
        for p in ([entry] if not entry.is_dir() or entry.is_symlink() else sorted(entry.rglob('*'))):
            parts=p.relative_to(root).parts
            if p.relative_to(root).as_posix() in EXCLUDED_FILES:continue
            if not include_mcp_wheels and parts[:2]==('wheelhouse','mcp-linux-x86_64-cp312'):continue
            if (parts and parts[0] in EXCLUDED_ROOTS) or any(x in EXCLUDED_PARTS for x in parts):continue
            if any(x in ('__pycache__','.git') or x.endswith('.egg-info') for x in parts) or p.suffix in ('.pyc','.pyo','.pid'):continue
            if p.is_symlink():raise ValueError('Release input contains a symlink: '+str(p))
            if p.is_file():selected.append(p)
    return sorted(selected)


def copy_file(source,target):
    target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    target.chmod(stat.S_IMODE(source.stat().st_mode))


def manifest(root,version):
    rows=[]
    for p in sorted(root.rglob('*')):
        if p.is_symlink():raise ValueError('Symlink in staged release')
        if p.is_file() and p!=root/'RELEASE_MANIFEST.json':
            rows.append({'path':p.relative_to(root).as_posix(),'bytes':p.stat().st_size,'sha256':digest(p),'mode':stat.S_IMODE(p.stat().st_mode)})
    return {'schema_version':1,'version':version,'files':rows}


def make_zip(stage,archive):
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(stage.rglob('*')):
            if not p.is_file():continue
            # A fixed ZIP epoch keeps filesystem timestamps out of the archive identity.
            info=zipfile.ZipInfo(stage.name+'/'+p.relative_to(stage).as_posix(),date_time=(1980,1,1,0,0,0))
            info.create_system=3;info.external_attr=(stat.S_IFREG|stat.S_IMODE(p.stat().st_mode))<<16
            info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,p.read_bytes())


def verify_zip(archive,prefix,record):
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise ValueError('ZIP CRC verification failed')
        expected={prefix+'/'+r['path'] for r in record['files']}|{prefix+'/RELEASE_MANIFEST.json'}
        if set(z.namelist())!=expected or len(z.namelist())!=len(expected):raise ValueError('ZIP inventory mismatch')
        for r in record['files']:
            name=prefix+'/'+r['path'];data=z.read(name)
            if len(data)!=r['bytes'] or hashlib.sha256(data).hexdigest()!=r['sha256']:raise ValueError('ZIP byte/hash mismatch: '+name)
            if (z.getinfo(name).external_attr>>16)&0o7777!=r['mode']:raise ValueError('ZIP mode mismatch: '+name)
    return {'verified_files':len(record['files']),'file_modes_preserved':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--without-mcp-wheels',action='store_true',help='Omit the optional CPython 3.12 Linux MCP wheelhouse')
    args=parser.parse_args();out=args.output.resolve()
    if out.exists():parser.error('Choose a NEW output directory; existing evidence is never overwritten')
    if out==ROOT or ROOT in out.parents:parser.error('Output must be outside the source checkout')
    version=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    wheel=ROOT/f'dist/compression_lab-{version}-py3-none-any.whl'
    if not wheel.is_file():parser.error('Build the matching wheel first')
    if wheel.is_symlink() or wheel.parent.is_symlink():parser.error('Release wheel path must not contain a symlink')
    sources=selected_files(ROOT,include_mcp_wheels=not args.without_mcp_wheels)
    out.mkdir(parents=True);stage=out/f'compression-lab-{version}';stage.mkdir()
    for source in [*sources,wheel]:copy_file(source,stage/source.relative_to(ROOT))
    provenance=stage/'provenance';provenance.mkdir(exist_ok=True)
    (provenance/'PREVIOUS_RELEASE.json').write_text(json.dumps(PREVIOUS_RELEASE,indent=2)+'\n')
    record=manifest(stage,version);(stage/'RELEASE_MANIFEST.json').write_text(json.dumps(record,indent=2)+'\n')
    subprocess.run([sys.executable,str(stage/'tools/verify_release.py'),str(stage)],check=True)
    archive=out/(stage.name+'.zip');make_zip(stage,archive)
    receipt={'status':'passed','version':version,'verified_at':datetime.now(timezone.utc).isoformat(),
             'archive':archive.name,'bytes':archive.stat().st_size,'sha256':digest(archive),
             **verify_zip(archive,stage.name,record),
             'mcp_wheels_included':(stage/'wheelhouse/mcp-linux-x86_64-cp312').is_dir()}
    (out/'release-receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    (out/(archive.name+'.sha256')).write_text(receipt['sha256']+'  '+archive.name+'\n')
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':main()
