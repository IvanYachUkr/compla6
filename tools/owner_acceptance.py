#!/usr/bin/env python3
"""Model-free owner lifecycle check with generated, disposable synthetic data.

Run as a non-root owner, with a separate unprivileged agent UID and an
administrator-owned host lock. This is infrastructure evidence, not a new
held-out research result. It must run without other benchmark jobs.
"""
import argparse
import hashlib
import json
import os
import random
from pathlib import Path

from compression_lab import baselines, dataset, owner
from compression_lab.engine import Engine, doctor
from compression_lab.util import Error, Ledger, load, save


def objects(directory, seed, private=False):
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    rows = []
    for i in range(4):
        block = f'owner-lifecycle-fixture {seed} {i}\r\n'.encode() + rng.randbytes(4096)
        size = 8 * 1024 * 1024
        payload = (block * (size // len(block) + 1))[:size]
        name = f'object-{i}.bin'
        (directory / name).write_bytes(payload)
        rows.append({'alias': f'object-{i}', 'source_group': f'group-{i}',
                     'split': 'development' if private or i >= 2 else 'train',
                     'path': name, 'canonical_bytes': size,
                     'canonical_sha256': hashlib.sha256(payload).hexdigest()})
    save(directory / 'manifest.json', {'schema_version': 1, 'objects': rows})
    if private:
        card = {'schema_version': 1, 'adapter': 'bytes', 'private_manifest': 'manifest.json'}
    else:
        card = dataset.example_card('synthetic-owner-lifecycle')
        card['scope'] = 'Generated infrastructure acceptance; no held-out research claim'
        card['timing_policy']['role'] = 'benchmark'
    save(directory / 'card.json', card)
    return directory / 'card.json'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--agent-uid', type=int, required=True)
    parser.add_argument('--benchmark-host', action='store_true', required=True)
    parser.add_argument('--workload', choices=['independent_objects', 'mutable_store'], default='independent_objects')
    args = parser.parse_args()
    root = args.output.absolute()
    root.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'model_calls': 0,
              'scope': 'Synthetic owner lifecycle, not held-out research validation'}

    def record(stage, value):
        save(root / (stage + '.json'), value)
        print(json.dumps({'stage': stage, 'status': value.get('status')}), flush=True)

    try:
        if os.geteuid() == 0 or args.agent_uid in (0, os.geteuid()):
            raise Error('separate_nonroot_identities_required')
        private = root / 'owner'
        private.mkdir(mode=0o700)
        if args.workload == 'mutable_store':
            from compression_lab.workloads import registry, relational
            from compression_lab.workloads.fixtures import create_dataset, movie_schema
            public_card = create_dataset(root / 'input-public', 'mutable_store')
            card = load(public_card)
            card['timing_policy']['role'] = 'benchmark'
            card['mutable_policy']['metric_trials'] = 3
            card['scope'] = 'Synthetic mutable owner lifecycle; not held-out research'
            save(public_card, card)
            public = Engine.init(root / 'public', public_card)
            data = private / 'input-private'
            data.mkdir()
            payload = relational.pack(json.dumps(movie_schema()).encode(), [(t['name'], b'') for t in movie_schema()['tables']])
            (data / 'empty.rlb').write_bytes(payload)
            save(data / 'manifest.json', {'schema_version': 1, 'objects': [
                {'alias': 'private-empty', 'source_group': 'private-empty-group', 'path': 'empty.rlb',
                 'canonical_bytes': len(payload), 'canonical_sha256': hashlib.sha256(payload).hexdigest()}]})
            private_card = data / 'card.json'
            save(private_card, {'schema_version': 1, 'adapter': 'relational_bundle', 'private_manifest': 'manifest.json'})
            registry.create_reference(root / 'candidate')
            training_hash = next(r['canonical_sha256'] for r in public.data()[1] if r['split'] == 'train')
            (root / 'candidate' / 'training-proof.bin').write_text(training_hash)
            manifest = load(root / 'candidate' / registry.MANIFEST)
            manifest.update(artifact_paths=['training-proof.bin'], training={'kind': 'train-only', 'object_sha256': [training_hash]})
            save(root / 'candidate' / registry.MANIFEST, manifest)
        else:
            public = Engine.init(root / 'public', objects(root / 'input-public', 2026090601))
            private_card = objects(private / 'input-private', 2026090602, True)
            baselines.create(root / 'candidate', 'zstd', 1)
        record('owner-init', owner.init(private, args.agent_uid, private_card,
                                       public.root, args.benchmark_host))
        check = doctor(True, private)
        record('release-doctor', check)
        if check['status'] != 'ok':
            raise Error('owner_preflight_failed')
        registration = public.register(root / 'candidate')
        record('register', registration)
        cid = registration['candidate_digest']
        result = None
        if args.workload != 'mutable_store':
            result = public.evaluate(cid, 'full', wait=True)
            record('public-result', result)
            if not result['metrics']['eligible']:
                raise Error('public_control_ineligible')
        frozen = owner.freeze(private, cid)
        record('freeze', frozen)
        state = Ledger(private / 'state').read()
        if state['stage'] != 'frozen' or state['private_attempts'] != 0 or (private / 'private-snapshot').exists():
            raise Error('freeze_boundary_failed')
        record('private-result', owner.evaluate(private))
        state = Ledger(private / 'state').read()
        if state['stage'] != 'complete' or state['private_attempts'] != 1:
            raise Error('private_lifecycle_failed')
        try:
            owner.evaluate(private)
        except Error as error:
            if error.code != 'private_test_consumed_or_not_frozen':
                raise
        else:
            raise Error('private_terminal_latch_failed')
        disclosed = owner.disclose(private)
        record('disclosure', disclosed)
        report.update(status='complete', candidate_digest=cid,
                      workload=args.workload, public_result_id=result['metrics']['result_id'] if result else None,
                      public_recheck_path=frozen['metrics'].get('public_recheck_path'),
                      private_attempts=1, repeat_evaluation_denied=True,
                      private_eligible=disclosed['metrics']['eligible'])
    except BaseException as error:
        report.update(status='failed', reason_code=getattr(error, 'code', type(error).__name__),
                      error=str(error)[:2000])
        raise
    finally:
        save(root / 'summary.json', report)
        print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
