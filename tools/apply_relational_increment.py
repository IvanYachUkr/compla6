#!/usr/bin/env python3
"""Conflict-safe, idempotent application of a verified source-only increment."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe(root, name, *, file=False):
    if (not isinstance(name, str) or not name or '\\' in name or '\0' in name
            or PurePosixPath(name).is_absolute() or str(PurePosixPath(name)) != name
            or any(p in ('', '.', '..') for p in PurePosixPath(name).parts)):
        raise ValueError('unsafe relative path: ' + repr(name))
    p = root
    if p.is_symlink():
        raise ValueError('symlink root')
    for part in PurePosixPath(name).parts:
        p = p / part
        if p.is_symlink():
            raise ValueError('symlink path: ' + name)
    if p.exists() and (not p.is_file() or p.stat().st_nlink != 1):
        raise ValueError('nonregular target: ' + name)
    if file and not p.is_file():
        raise ValueError('missing file: ' + name)
    return p


def atomic(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        if os.name == 'posix':
            d = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(d)
            finally:
                os.close(d)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def apply(bundle, base, write=False):
    bundle, base = Path(bundle).absolute(), Path(base).absolute()
    if not base.is_dir() or base.is_symlink():
        raise ValueError('base must be an existing non-symlink source directory')
    manifest_path = safe(bundle, 'INCREMENT_MANIFEST.json', file=True)
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get('schema_version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('invalid increment manifest')
    states, conflicts = {}, []
    sources = {}
    for name, record in sorted(manifest['files'].items()):
        source = safe(bundle, 'overlay/' + name, file=True)
        target = safe(base, name)
        if source.stat().st_size != record['after_bytes'] or sha(source) != record['after_sha256']:
            raise ValueError('tampered overlay: ' + name)
        sources[name] = source
        current = sha(target) if target.exists() else None
        if current == record['after_sha256']:
            states[name] = 'already_applied'
        elif current == record['before_sha256']:
            states[name] = 'replace' if current is not None else 'add'
        else:
            conflicts.append({'path': name, 'actual_sha256': current, 'expected_before_sha256': record['before_sha256']})
    if conflicts:
        return {'status': 'conflict', 'conflicts': conflicts, 'written_files': [],
                'instruction': 'Merge the supplied patch/overlay with base-originals. No files were changed.'}, 2
    written = []
    if write:
        backup_name = '.relational-increment-backups/' + sha(manifest_path)[:16]
        for name, state in states.items():
            if state == 'already_applied':
                continue
            target = safe(base, name)
            record = manifest['files'][name]
            # Re-check immediately before a write; interrupted applications can
            # be resumed without replacing an independently edited target.
            current = sha(target) if target.exists() else None
            if current != record['before_sha256']:
                raise ValueError('target changed after preflight: ' + name)
            if state == 'replace':
                backup = safe(base, backup_name + '/' + name)
                if backup.exists() and sha(backup) != record['before_sha256']:
                    raise ValueError('conflicting preserved original: ' + name)
                if not backup.exists():
                    atomic(backup, target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
            atomic(target, sources[name].read_bytes(), record.get('mode', 0o644))
            written.append(name)
        receipt = safe(base, backup_name + '/APPLIED.json')
        atomic(receipt, (json.dumps({'extension': manifest.get('extension'), 'manifest_sha256': sha(manifest_path),
                                    'all_targets': sorted(states)}, sort_keys=True, indent=2) + '\n').encode(), 0o644)
    return {'status': 'applied' if write else 'compatible', 'written_files': written,
            'already_applied': sum(value == 'already_applied' for value in states.values()),
            'checked_files': len(states), 'originals_preserved': True}, 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', required=True)
    p.add_argument('--bundle', default=str(Path(__file__).resolve().parent))
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--apply', action='store_true')
    a = p.parse_args()
    try:
        result, code = apply(a.bundle, a.base, a.apply)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result, code = {'status': 'error', 'error': str(exc)}, 2
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
