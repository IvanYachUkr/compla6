#!/usr/bin/env python3
"""Complete native smoke benchmark plus finish checks against its real evidence.

No model calls. The native recipes are conventional controls, never agent work.
"""
import argparse
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from benchmark_redesign import run
from compression_lab.controller import Controller
from compression_lab.engine import Engine
from compression_lab.util import save


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--object-bytes', type=int, default=4 * 1024 * 1024)
    args = parser.parse_args()
    if not 65536 <= args.object_bytes <= 16 * 1024 * 1024:
        parser.error('object-bytes must be 64 KiB..16 MiB')
    output = args.output.absolute()
    code = run(output, args.object_bytes, do_ablation=True)
    if code:
        return code
    engine = Engine(output / 'workspace')
    controller = Controller.configure(engine, {
        'schema_version': 1, 'objective': 'improvement',
        'deadline_epoch': time.time() + 600,
        'max_children': 0, 'max_depth': 0, 'max_feedback_attempts': 0,
        'models': {'root': [{'model': 'manual/host', 'effort': 'none'}], 'worker': []},
        'refinement_mode': 'frozen',
    })
    # Choose already measured full evidence. Never reclassify its baseline track.
    result_id = next(rid for rid in engine.state()['results']
                     if engine.raw(rid).get('depth') == 'full'
                     and engine.raw(rid).get('quality_passed'))
    raw = engine.raw(result_id)
    rejected = controller.finish('baseline-is-not-agent', raw['candidate_digest'], result_id, 'success')
    assert not rejected['accepted'] and 'track_mismatch' in rejected['reason_codes'], rejected
    closed = controller.finish('honest-negative', outcome='negative')
    assert closed['accepted'] and not closed['candidate_qualified'], closed
    assert Controller(engine).finish('honest-negative', outcome='negative') == closed
    assert controller.brief()['lifecycle'] == 'closed'
    exported = engine.export(result_id, output / 'after-controller-close.zip')
    save(output / 'CONTROLLER_RECEIPTS.json', {
        'status': 'passed', 'evidence_kind': 'native-public-smoke-and-controller-integration',
        'model_calls': 0, 'agent_research_attempts': 0,
        'baseline_result_id': result_id, 'baseline_candidate_digest': raw['candidate_digest'],
        'rejected_baseline_finish': rejected, 'negative_finish': closed,
        'archival_export_after_close': exported,
        'claim_limit': 'Real native full evidence exercises the public finish boundary. No model, memory benefit, compression improvement or held-out claim.',
    })
    print('Native controller receipts passed:', output / 'CONTROLLER_RECEIPTS.json', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
