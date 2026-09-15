#!/usr/bin/env python3
"""Fixed readiness/acceptance events for an idle parent and idle implementation child."""
import argparse
import datetime as dt
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time

FLOOR = 100.0

def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def campaign_contract(campaign):
    card = read(Path(campaign['workspace'])/'dataset-card.json', {})
    return {**campaign, 'baseline_visibility':card.get('baseline_visibility',campaign.get('baseline_visibility','visible')),
            'timing_scope':card.get('timing_policy',{}).get('scope',campaign.get('timing_scope'))}


def save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(path)


def public_decision(ledger, results, timing_scope=None):
    """Pure policy, with immutable evaluator rows as the only score evidence."""
    used = ledger.get('budgets', {}).get('agent', {})
    limits = ledger.get('search_budget', {})
    if (used.get('evaluations', 0) >= limits.get('candidate_evaluations', float('inf'))
            or used.get('wall_seconds', 0) >= limits.get('wall_seconds', float('inf'))):
        return {'action': 'budget_exhausted'}
    rows = [r for r in results if r.get('track') == 'agent'
            and r.get('resource_profile') == r.get('primary_resource_profile')
            and r.get('timing_scope') in ('validation-only-v2','whole-dataset-v1')
            and (timing_scope is None or r.get('timing_scope') == timing_scope)]
    for r in rows:
        speed = r.get('timing', {}).get('encode', {}).get('median_decimal_MB_per_second', 0)
        if (r.get('depth') == 'full' and r.get('quality_passed') is True
                and r.get('eligible') is True and speed >= FLOOR):
            return {'action': 'qualified', 'result_id': r['result_id']}
    latest = rows[-1] if rows else {}
    return {'action': 'retry', 'result_id': latest.get('result_id'),
            'candidate_digest': latest.get('candidate_digest'),
            'quality_passed': latest.get('quality_passed'),
            'encode_MB_s': latest.get('timing', {}).get('encode', {}).get('median_decimal_MB_per_second'),
            'reason_codes': latest.get('reason_codes', ['no_full_agent_result'])}



BASE = Path('.')  # CLI supplies an explicit campaign; tests use isolated temporary roots
POLICY = 'prime-single-child-idle-acceptance-v4'
BENCHMARK_LOCK = Path('/run/compression-lab/benchmark.lock')

def jsonl(path):
    raw = Path(path).read_bytes()
    complete = raw[:raw.rfind(b'\n') + 1]
    return [json.loads(line) for line in complete.splitlines() if line]

def native_idle_receipt(entries):
    """An exact terminal message survives driver restarts and repeated polls."""
    session_id = None
    receipt = None
    for entry in entries:
        kind = entry.get('type')
        if kind == 'session':
            session_id = entry.get('id')
        elif kind == 'custom_message' and entry.get('customType') == 'agent_message':
            receipt = None
        elif kind == 'message':
            message = entry.get('message', {})
            receipt = ({'session_id': session_id, 'message_id': entry['id'], 'at': entry.get('timestamp')}
                       if session_id and entry.get('id') and message.get('role') == 'assistant'
                       and message.get('stopReason') == 'stop' else None)
    return receipt

def feedback_basis(status, decision):
    if decision.get('action') == 'evaluation_completed':
        # Deliver the completed result once, including across later idle turns
        # and restarts. A score is evidence, not a task-completion receipt.
        return copy.deepcopy({'schema_version': 3,
                              'session': status.get('verified_session', {}).get('session_id'),
                              'decision': {k: decision[k] for k in
                                           ('action', 'result_id', 'candidate_digest') if k in decision}})
    return copy.deepcopy({'schema_version': 2, 'session': status.get('verified_session', {}).get('session_id'),
            'decision': decision, 'parent_idle_receipt': status.get('parent_idle_receipt'),
            'child_idle_receipts': {key: child.get('native_idle_receipt')
                                    for key, child in sorted(status.get('children', {}).items())}})

def benchmark_lock_status(path=BENCHMARK_LOCK):
    """Observe availability without creating, stealing, or retaining the lease."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return 'unavailable'
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'busy'
        except OSError:
            return 'unavailable'
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 'free'
    finally:
        os.close(fd)

def child_active(child):
    return child.get('status') in ('queued', 'running') or bool(child.get('activity'))

def project_native_child(entries, child, expected_model='openai-codex/gpt-5.6-luna', expected_thinking='high'):
    """Project saved child lifecycle without relying on initial-run RPC events.

    Prime rehydrates retained children without the original rlm_child_update
    forwarder. A received task or nonterminal message keeps the child busy;
    only an explicit final assistant stop establishes an idle boundary.
    """
    observed = dict(child)
    active = None
    tool_count = 0
    for entry in entries:
        kind = entry.get('type')
        if kind == 'session':
            observed['native_session_id'] = entry.get('id')
        elif kind == 'model_change':
            observed['native_model'] = entry.get('provider', '') + '/' + entry.get('modelId', '')
        elif kind == 'thinking_level_change':
            observed['native_thinking'] = entry.get('thinkingLevel')
        elif kind == 'custom_message' and entry.get('customType') == 'agent_message':
            active = True
        elif kind == 'message':
            message = entry.get('message', {})
            role = message.get('role')
            if role == 'assistant':
                tool_count += sum(c.get('type') == 'toolCall' for c in message.get('content', []))
                active = message.get('stopReason') != 'stop'
            elif role in ('user', 'toolResult'):
                active = True
        if kind in ('message', 'custom_message'):
            observed['native_last_message_at'] = entry.get('timestamp')
    observed['toolUseCount'] = tool_count
    observed['native_idle_receipt'] = native_idle_receipt(entries)
    observed['activity_source'] = 'native_child_transcript'
    observed['activity'] = {'kind': 'writing' if active else 'waiting'} if active is not False else None
    if (observed.get('native_model') != expected_model
            or observed.get('native_thinking') != expected_thinking):
        observed['activity'] = {'kind': 'waiting'}
        observed['native_observation_error'] = 'Unexpected child model or effort; suppress automatic prompts'
    return observed

def refresh_native_children(campaign, status):
    """Read only this pilot's known session files; never expose transcript text."""
    result = {**status, 'children': {}, 'parent_idle_receipt': None}
    native_home = Path(campaign['native_home'])
    profile = Path(campaign['profile'])
    def owned_path(native_path):
        path = (profile / Path(native_path).relative_to(native_home)).resolve(strict=True)
        if not path.is_relative_to(profile.resolve(strict=True)):
            raise ValueError('Native metadata escapes the owned profile')
        return path
    try:
        parent = status['verified_session']
        session_file = owned_path(parent['session_file'])
        receipt = native_idle_receipt(jsonl(session_file))
        if receipt and receipt['session_id'] == parent['session_id']:
            result['parent_idle_receipt'] = receipt
    except (OSError, ValueError, KeyError, TypeError):
        result['native_observation_error'] = 'Native parent state unreadable; suppress automatic prompts'
    for child_id, child in status.get('children', {}).items():
        try:
            session_dir = owned_path(child['sessionDir'])
            display = read(session_dir / 'rlm-subagent.json')
            if not display or display.get('childId') != child_id:
                raise ValueError('Child registry unavailable or mismatched')
            session_file = owned_path(display['sessionFile'])
            entries = jsonl(session_file)
            observed = project_native_child(entries, child,
                campaign.get('child_model','openai-codex/gpt-5.6-luna'), campaign.get('child_thinking','high'))
        except (OSError, ValueError, KeyError, TypeError):
            observed = {**child, 'activity': {'kind': 'waiting'},
                        'native_observation_error': 'Native child state unreadable; suppress automatic prompts'}
        result['children'][child_id] = observed
    return result

def decide(campaign, status, ledger, baseline_complete, rows):
    if campaign.get('status') != 'research_running':
        return {'action': 'not_research_running'}
    if status.get('status') != 'idle':
        return {'action': 'parent_active_or_stopped'}
    if any(child_active(c) for c in status.get('children', {}).values()):
        return {'action': 'child_active'}
    if not status.get('parent_idle_receipt') or any(
            not child.get('native_idle_receipt') for child in status.get('children', {}).values()):
        return {'action': 'native_idle_unconfirmed'}
    if ledger.get('stage') not in ('public_search', 'public_ready') or ledger.get('active_job'):
        return {'action': 'evaluation_active_or_terminal'}
    if campaign.get('baseline_visibility','visible') != 'hidden' and not baseline_complete:
        return {'action': 'baselines_pending'}
    timing_scope = campaign.get('timing_scope')
    decision = public_decision(ledger, rows, timing_scope)
    if decision['action'] in ('qualified', 'budget_exhausted'):
        # Completion delivery also covers public scaling checks. Qualification
        # remains primary-profile-only in public_decision; do not conflate them.
        full = [r for r in rows if r.get('track') == 'agent' and r.get('depth') == 'full'
                and r.get('timing_scope') in ('validation-only-v2','whole-dataset-v1')
                and (timing_scope is None or r.get('timing_scope') == timing_scope)]
        if full:
            return {'action': 'evaluation_completed', 'result_id': full[-1]['result_id'],
                    'candidate_digest': full[-1].get('candidate_digest'), 'public_acceptance': decision}
    if decision['action'] == 'retry' and not decision.get('result_id'):
        return {'action': 'baseline_ready'}
    return decision

def public_rows(workspace, ledger):
    # Two snapshots coexist during the final send guard. Retain policy facts,
    # not each result's full trial/audit payload, so memory stays bounded.
    rows = []
    fields = ('track', 'resource_profile', 'primary_resource_profile', 'timing_scope',
              'depth', 'quality_passed', 'eligible', 'candidate_digest', 'reason_codes', 'timing')
    for rid in ledger.get('results', []):
        raw = read(workspace / 'results' / rid / 'raw.json')
        if raw:
            rows.append({**{key: raw[key] for key in fields if key in raw}, 'result_id': rid})
    return rows

def continuation_message(ledger, rows, timing_scope=None):
    full = [r for r in rows if r.get('track') == 'agent' and r.get('depth') == 'full']
    facts = {'stage': ledger.get('stage'), 'active_job': ledger.get('active_job'),
             'full_agent_results': len(full), 'latest_full_agent_result_id': full[-1]['result_id'] if full else None,
             'public_acceptance': public_decision(ledger, rows, timing_scope),
             'agent_budget': ledger.get('budgets', {}).get('agent', {}),
             'search_budget': ledger.get('search_budget')}
    completion = (
        'The per-run evaluation budget is exhausted. Do not start further experiments. '
        'Review the last completed result and finish workbench/agent/RESULT.md and CHECKPOINT.md, '
        'the selected full-result receipts and exports. Preserve the best correct alternatives '
        'and report the actual terminal outcome; exhaustion does not erase a qualifying final trial. '
        if facts['public_acceptance']['action'] == 'budget_exhausted' else
        'A completed full evaluation is available even if your last turn ended with evaluation pending. '
        'Review the recorded result and feedback. Inspect the implementation deeply and pursue concrete '
        'performance improvements under the original objective and remaining authorization, then update '
        'workbench/agent/RESULT.md and CHECKPOINT.md with the selected result and export the strongest '
        'fully gated eligible candidate. Preserve the correct best-size alternative if too slow. '
        'If these deliverables are already complete, report their existing paths and result IDs instead of repeating them. '
        'Do not rerun an unchanged candidate for a more favorable timing or claim a private-test result. '
        if facts['public_acceptance']['action'] == 'qualified' else '')
    return ('Automatic infrastructure continuation (' + POLICY + '). Current evaluator status: '
            + json.dumps(facts, sort_keys=True) + '\n\n'
            + completion
            +
            'A busy response from the shared benchmark lease is temporary contention, not a completed evaluation. '
            'The observer has now observed that lease free; another authorized worker may subsequently acquire it. '
            'Continue or finalize the original task with the same parent and retained implementation child at their saved model and effort. '
            'If the child is idle, resume that existing child through native agent_message. Do not create a child or override saved model/effort. '
            'Follow the sealed run configuration and current supervisor-authorized time window. '
            'Preserve prior work and use current evaluator evidence. This message contains no codec or parameter advice.')

def tick(campaign_path=None):
    campaign_path = Path(campaign_path) if campaign_path else BASE/'campaign.json'
    base = campaign_path.parent
    out = base / 'observer'
    out.mkdir(mode=0o700, exist_ok=True)
    saved = read(out / 'state.json', {})
    deliveries = saved.setdefault('deliveries', {})
    campaign = campaign_contract(read(campaign_path, {}))
    deadline = (dt.datetime.fromisoformat(campaign['deadline_utc']).timestamp()
                if campaign['deadline_utc'] is not None else float('inf'))
    now = time.time()
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    workspace = Path(campaign['workspace'])
    driver = Path(campaign['driver_output'])
    status = refresh_native_children(campaign, read(driver / 'status.json', {}))
    ledger = read(workspace / 'state/state.json', {})
    rows = public_rows(workspace, ledger)
    decision = decide(campaign, status, ledger,
                      (workspace / 'baseline-campaign/complete.json').exists(), rows)
    commands = driver / 'commands.jsonl'
    prior = {row.get('id'): row for row in jsonl(commands)}
    responses = {row.get('id'): row for row in jsonl(driver / 'events.jsonl')
                 if row.get('type') == 'response' and row.get('command') == 'prompt'}
    transitions = []

    def reconcile_delivery(receipt):
        key = receipt['id']
        response = responses.get(key)
        old = receipt['status']
        if response and old not in ('acknowledged', 'rejected'):
            receipt.update(status='acknowledged' if response.get('success') is True else 'rejected',
                           acknowledgment_observed_at=stamp,
                           native_receipt={'id': key, 'command': 'prompt', 'success': response.get('success')})
        elif key in prior and old == 'pending':
            receipt.update(status='queued', queue_recovered_at=stamp)
        if receipt['status'] != old:
            transitions.append(dict(receipt))

    for receipt in deliveries.values():
        reconcile_delivery(receipt)
    delivery = deliveries.get(saved.get('latest_delivery'))
    command = None
    lock_status = None
    if now >= deadline:
        decision = {'action': 'deadline'}
    elif not (driver / 'status.json').exists() or now - (driver / 'status.json').stat().st_mtime > 30:
        decision = {'action': 'stale_driver'}
    elif decision['action'] in ('retry', 'baseline_ready', 'evaluation_completed'):
        basis = feedback_basis(status, decision)
        key = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
        if key not in deliveries:
            deliveries[key] = {'id': key, 'status': 'pending', 'created_at': stamp, 'basis': basis}
            transitions.append(dict(deliveries[key]))
        delivery = deliveries[key]
        saved['latest_delivery'] = key
        reconcile_delivery(delivery)
        if delivery['status'] == 'acknowledged':
            decision = {'action': 'event_already_acknowledged', 'basis': decision}
        elif delivery['status'] == 'rejected':
            decision = {'action': 'native_prompt_rejected', 'basis': decision}
        elif delivery['status'] == 'queued':
            decision = {'action': 'awaiting_native_ack', 'basis': decision}
        elif any(row['status'] == 'queued' for row in deliveries.values()):
            decision = {'action': 'another_wake_awaiting_ack', 'basis': decision}
        elif now - saved.setdefault('idle_since', now) < 90:
            decision = {'action': 'confirming_idle'}
        elif commands.stat().st_mtime > (driver / 'status.json').stat().st_mtime:
            decision = {'action': 'command_pending'}
        else:
            current = campaign_contract(read(campaign_path, {}))
            latest = refresh_native_children(current, read(driver / 'status.json', {}))
            current_ledger = read(workspace / 'state/state.json', {})
            current_rows = public_rows(workspace, current_ledger)
            current_decision = decide(current, latest, current_ledger,
                                      (workspace / 'baseline-campaign/complete.json').exists(), current_rows)
            if time.time() >= deadline or feedback_basis(latest, current_decision) != basis:
                decision = {'action': 'state_changed_before_send'}
            else:
                lock_status = benchmark_lock_status()
                if lock_status != 'free':
                    decision = {'action': 'benchmark_busy' if lock_status == 'busy' else 'benchmark_lock_unavailable',
                                'basis': decision, 'pending_id': key}
                else:
                    command = {'id': key, 'type': 'prompt', 'message': continuation_message(current_ledger, current_rows, campaign.get('timing_scope'))}
                    # Persist intent before the queue side effect; a restart can
                    # reconcile its stable ID with the queue and native response.
                    save(out / 'state.json', saved)
                    with commands.open('a') as stream:
                        stream.write(json.dumps(command) + '\n'); stream.flush(); os.fsync(stream.fileno())
                    delivery.update(status='queued', queued_at=stamp,
                                    queue_evidence='driver/commands.jsonl; native acceptance not yet observed')
                    transitions.append(dict(delivery))
                    saved.pop('idle_since', None)
    else:
        saved.pop('idle_since', None)
    record = {'at_utc': stamp, 'policy': POLICY, 'decision': decision,
              'sent': command is not None, 'acknowledged': bool(delivery and delivery['status'] == 'acknowledged'),
              'delivery': delivery, 'benchmark_lock_observed': lock_status,
              'children': status.get('children', {}), 'parent_idle_receipt': status.get('parent_idle_receipt')}
    if transitions or command:
        with (out / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps({**record, 'delivery_transitions': transitions, 'command': command}) + '\n')
            stream.flush(); os.fsync(stream.fileno())
    save(out / 'state.json', saved)
    save(out / 'status.json', record)
    return deadline

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, required=True)
    args = parser.parse_args()
    while True:
        deadline = tick(args.campaign)
        if time.time() >= deadline:
            break
        time.sleep(min(45, deadline - time.time()))
