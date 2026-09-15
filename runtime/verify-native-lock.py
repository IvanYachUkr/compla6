#!/usr/bin/env python3
"""Check the exact actually installed native package versions and file hashes."""
import argparse,hashlib,json,subprocess,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--hashes',action='store_true');p.add_argument('--lock-dir',type=Path,default=Path(__file__).parent);a=p.parse_args();root=a.lock_dir;errors=[];count=0
for r in json.loads((root/'native-packages.lock.json').read_text())['packages']:
 v=subprocess.run(['dpkg-query','-W','-f=${Version}',r['package']],capture_output=True,text=True)
 if v.returncode or v.stdout!=r['version']:errors.append({'package':r['package'],'expected':r['version'],'found':v.stdout or 'missing'})
 count+=1
if a.hashes:
 for r in json.loads((root/'native-toolchain-files.lock.json').read_text())['files']:
  p=Path(r['path'])
  if not p.is_file() or hashlib.file_digest(p.open('rb'),'sha256').hexdigest()!=r['sha256']:errors.append({'path':str(p),'reason':'hash_mismatch_or_missing'})
  count+=1
print(json.dumps({'checked':count,'status':'passed' if not errors else 'blocked','errors':errors},indent=2));sys.exit(bool(errors))
