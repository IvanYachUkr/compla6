#!/usr/bin/env python3
"""Fetch approved native sources to a NEW directory; never run a build or installer.

Network is needed ONLY for this operator step, never for a compressor runtime.
Git sources are pinned by full commit OID; XZ by official archive SHA-256.
A fresh SHA-256 inventory is written for every fetched tree. No @latest resolve.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

ROOT=Path(__file__).resolve().parents[1]
MAX_DOWNLOAD=512*1024*1024


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def verify(path, expected):
    actual=digest(path)
    if actual!=expected:raise ValueError(f'Hash mismatch: expected {expected}, got {actual}')
    return actual


def extract_regular_tar(archive, output):
    """Fail closed on links/special files/path tricks; do not trust tar permissions."""
    output=Path(output);output.mkdir()
    with tarfile.open(archive,'r:*') as tar:
        members=tar.getmembers()
        if len(members)>100000 or sum(m.size for m in members)>2*1024**3:raise ValueError('Oversized source archive')
        seen=set()
        for m in members:
            rel=PurePosixPath(m.name)
            if rel.is_absolute() or '..' in rel.parts or '\\' in m.name or not rel.parts:
                raise ValueError('Unsafe archive path')
            if not (m.isfile() or m.isdir()):raise ValueError('Source archive links/special files are not accepted')
            path=output.joinpath(*rel.parts)
            if path in seen:raise ValueError('Duplicate archive path')
            seen.add(path)
        for m in members:
            path=output.joinpath(*PurePosixPath(m.name).parts)
            if m.isdir():path.mkdir(parents=True,exist_ok=True);continue
            path.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(m) as src,path.open('xb') as dst:shutil.copyfileobj(src,dst)
            path.chmod(0o755 if m.mode&0o111 else 0o644)


def inventory(root):
    rows=[]
    for path in sorted(root.rglob('*')):
        if '.git' in path.relative_to(root).parts:continue
        if path.is_symlink():
            rows.append({'path':path.relative_to(root).as_posix(),'kind':'symlink','target':os.readlink(path)})
        elif path.is_file():
            rows.append({'path':path.relative_to(root).as_posix(),'bytes':path.stat().st_size,'sha256':digest(path)})
    return rows


def fetch(package, output, lock_path=ROOT/'dependencies/upstream-lock.json'):
    output=Path(output).absolute()
    if output.exists():raise ValueError('Destination exists; choose a new path')
    spec=json.loads(Path(lock_path).read_text())['packages'][package];source=spec['source']
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.lab-fetch-',dir=output.parent) as td:
        pending=Path(td);receipt={'schema_version':1,'package':package,'version':spec['version'],
            'verified_lock_sha256':digest(lock_path),'source':source,'license_expression':spec['license_expression'],
            'license_url':spec['license_url'],'fetched_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'build_executed':False}
        url=source['url']
        if not url.startswith('https://github.com/'):raise ValueError('Only approved HTTPS GitHub native sources are supported')
        if source['kind']=='git-commit':
            tree=pending/'source';tree.mkdir()
            env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull}
            base=['git','-c','core.hooksPath=/dev/null','-C',str(tree)]
            for args in (['init','--quiet'],['fetch','--depth=1','--no-tags',url,source['commit_sha1']],['checkout','--detach','--quiet','FETCH_HEAD']):
                subprocess.run(base+args,env=env,check=True,timeout=180,capture_output=True)
            actual=subprocess.run(base+['rev-parse','HEAD'],env=env,check=True,timeout=10,capture_output=True,text=True).stdout.strip()
            if actual!=source['commit_sha1']:raise ValueError('Git commit mismatch')
            receipt['verified_commit_sha1']=actual
            # Keep no git metadata, hooks or remote credentials in the source capsule.
            shutil.rmtree(tree/'.git')
        elif source['kind']=='tar.gz':
            archive=pending/'source.tar.gz';total=0
            with urllib.request.urlopen(url,timeout=45) as response,archive.open('xb') as dst:
                if not response.geturl().startswith('https://'):raise ValueError('Insecure redirect')
                while block:=response.read(1024*1024):
                    total+=len(block)
                    if total>MAX_DOWNLOAD:raise ValueError('Download exceeded byte bound')
                    dst.write(block)
            receipt['verified_archive_sha256']=verify(archive,source['sha256'])
            expanded=pending/'expanded';extract_regular_tar(archive,expanded)
            children=list(expanded.iterdir())
            if len(children)!=1 or not children[0].is_dir():raise ValueError('Expected one source root')
            tree=children[0]
        else:raise ValueError('This helper fetches native sources only')
        rows=inventory(tree)
        receipt['files']=rows;receipt['tree_manifest_sha256']=hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        # Receipt is beside source, not an uncharged runtime artifact.
        capsule=pending/'capsule';capsule.mkdir();shutil.move(tree,capsule/'source')
        (capsule/'SOURCE_RECEIPT.json').write_text(json.dumps(receipt,indent=2)+'\n')
        capsule.rename(output)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--package',choices=['zstd','lz4','brotli','xz'],required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    try:
        result=fetch(args.package,args.output)
        print(json.dumps({'status':'fetched-and-hash-verified','package':result['package'],'version':result['version'],'output':str(args.output)}))
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc),'nothing_built':True}));return 1
    return 0

if __name__=='__main__':raise SystemExit(main())
