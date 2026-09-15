from pathlib import Path, PurePosixPath
import hashlib,json
root=Path(__file__).resolve().parent
manifest=json.loads((root/'BUNDLE_MANIFEST.json').read_text())
expected=set()
for row in manifest['files']:
 rel=PurePosixPath(row['path'])
 assert not rel.is_absolute() and '..' not in rel.parts
 p=root/rel
 assert p.is_file() and not p.is_symlink()
 assert len(p.read_bytes())==row['bytes']
 assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'],str(rel)
 expected.add(str(rel))
actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
assert actual==expected|{'BUNDLE_MANIFEST.json'},actual^expected
print('PASS',len(expected),'files; hashes and inventory verified')
