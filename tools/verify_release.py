#!/usr/bin/env python3
"""Verify inventory, hashes, sizes and modes in an extracted Compression Lab release."""
import hashlib,json,stat,sys
from pathlib import Path
root=Path(sys.argv[1] if len(sys.argv)>1 else Path(__file__).resolve().parent.parent)
m=json.loads((root/'RELEASE_MANIFEST.json').read_text());expected={r['path'] for r in m['files']};actual=set();errors=[]
forbidden_roots={'.venv','native','build','downloads','evidence'}
forbidden_parts={'.venv','build','downloads','evidence'}
def forbidden_prefix(rel):
 parts=Path(rel).parts
 if parts and parts[0] in forbidden_roots:return parts[0]
 for i,part in enumerate(parts[1:],1):
  if part in forbidden_parts:return '/'.join(parts[:i+1])
 return None
forbidden=set()
for p in root.rglob('*'):
 rel=p.relative_to(root).as_posix()
 prefix=forbidden_prefix(rel)
 if prefix:forbidden.add(prefix)
 if p.is_symlink():errors.append({'path':rel,'reason':'symlink'})
 elif p.is_file() and rel!='RELEASE_MANIFEST.json' and '__pycache__' not in p.parts:actual.add(rel)
for path in sorted(forbidden):errors.append({'path':path,'reason':'forbidden_tree'})
if actual!=expected:errors.append({'reason':'inventory','missing':sorted(expected-actual),'extra':sorted(actual-expected)})
for r in m['files']:
 p=root/r['path']
 if not p.is_file() or p.stat().st_size!=r['bytes'] or hashlib.file_digest(p.open('rb'),'sha256').hexdigest()!=r['sha256']:errors.append({'path':r['path'],'reason':'hash_or_size'})
 if p.is_file() and stat.S_IMODE(p.stat().st_mode)!=r['mode']:errors.append({'path':r['path'],'reason':'mode','expected':r['mode'],'actual':stat.S_IMODE(p.stat().st_mode)})
print(json.dumps({'status':'passed' if not errors else 'failed','files':len(expected),'errors':errors},indent=2));sys.exit(bool(errors))
