"""Bounded, evidence-linked search feedback. No hidden planner or model calls."""
from __future__ import annotations
from . import inventory, dataset
from .util import Error


def brief(engine):
    from .instructions import configuration, contract
    card, rows = engine.metadata()
    state = engine.state()
    caps = inventory.inspect()
    table = engine.baseline_table()['metrics']
    supplied = table.get('supplied_comparison_available', True)
    raw = [engine.raw(rid) for rid in state['results']]
    measured = set()
    for result in raw:
        if result.get('track') != 'baseline':
            continue
        from .workloads.bridge import verify_candidate
        manifest, _ = verify_candidate(engine.root/'candidates'/result['candidate_digest'])
        # Discovery only; qualification still requires full exact-context evidence.
        name = manifest['candidate_id']
        for family in caps['families']:
            if name == family or name.startswith(family+'-') or manifest.get('hypothesis',{}).get('family')==family:
                measured.add(family)
    static = card.get('workload', 'independent_objects') != 'mutable_store'
    from_scratch = card.get('implementation_policy') == 'from_scratch'
    report = {'schema_version': 1, 'dataset_id': card['dataset_id'],
        'baseline_visibility': card.get('baseline_visibility','visible'),
        'supplied_comparison_available': supplied,
        'composition_source': 'Sealed card/manifest metadata, not a payload-integrity or compression result',
        'workload': card.get('workload', 'independent_objects'), 'card_digest': state['card_digest'],
        'runtime_digest': state['runtime_digest'],'evaluation_mode':card.get('evaluation_mode','split'),
        'composition': {split: {'objects': sum(r['split']==split for r in rows),
              'canonical_bytes': sum(r['canonical_bytes'] for r in rows if r['split']==split),
              'groups': len({r['group'] for r in rows if r['split']==split})} for split in dataset.public_partitions(card)},
        'promotion_policy': {'full_required': True,
            'encoding_floor_bytes_per_second': card['objective']['encode_floor_bytes_per_second'],
            'encoding_floor_scope': card.get('timing_policy',{}).get('encoding_floor_scope','encode'),
            'timing_role': card['timing_policy'].get('role', 'smoke'),
            'rank_by': card['objective']['rank_by'],
            'deployment_horizon_bytes': card['objective']['deployment_canonical_bytes'],
            'primary_scenario': card.get('primary_size_policy',card.get('accounting_policy','standalone-v1')) if static else 'existing-mutable-policy',
            'timing_scope': card.get('timing_policy',{}).get('scope','train-plus-development-v1'),
            'screen_can_promote': False, 'quick_can_promote': False},
        'limits': card['limits'], 'resource_profiles':card.get('resource_profiles'), 'search_budget_per_track': state['search_budget'],
        'consumed_budgets': state['budgets'], 'active_job': state['active_job'],
        'unmeasured_families': sorted(set(caps['families'])-measured) if static and supplied else [],
        'available_families': {k: {'available': v['available'], 'installed_version': v['installed_version'],
                                  'static_codec_available': v['static_codec_available']} for k,v in caps['families'].items()},
        'recipe_ids': [] if from_scratch else list(caps['recipes']) if static else ['mutable-reference'],
        'qualified_controls': {'best_size':table.get('best_size'),'best_eligible':table.get('best_eligible'),'pareto_frontier':table.get('pareto_frontier',[])[:12],'frontier_count':len(table.get('pareto_frontier',[]))},
        'next_action': 'wait_for_active_job' if state['active_job'] else
                       'screen_unmeasured_controls' if static and supplied and set(caps['families'])-measured else 'one_focused_experiment',
        'run_configuration': configuration(engine), 'search_contract': contract(engine)}
    if from_scratch:
        report['available_families'] = {}
        report.pop('unmeasured_families', None)
        report['next_action'] = 'wait_for_active_job' if state['active_job'] else 'one_focused_experiment'
    if not supplied:
        report.pop('qualified_controls')
        report.pop('unmeasured_families', None)
    return report


def feedback(engine, result_id):
    from .screening import summary as screen_summary
    raw = engine.raw(result_id)
    from .policy import PublicPolicy
    policy = PublicPolicy(engine)
    floor = policy.card['objective']['encode_floor_bytes_per_second']
    fixed = raw.get('fixed_costs', {})
    controller=engine._controller()
    qualification_error=None
    try: qualification=policy.assess(result_id,raw['candidate_digest'])
    except (OSError,ValueError,TypeError,KeyError,AttributeError) as error:
        if controller is None: raise
        qualification=None; qualification_error=getattr(error,'code','invalid_qualification_evidence')
    report = {'schema_version': 1, 'result_id': result_id, 'candidate_digest': raw['candidate_digest'],
              'depth': raw['depth'], 'quality_passed': raw.get('quality_passed', False),
              'eligible': raw.get('eligible', False), 'reason_codes': raw.get('reason_codes', []),
              'fixed_cost_components': {key: fixed.get(key) for key in
                  ('fixed_bytes','config_bytes','packed_source_bytes','binary_bytes','nonplatform_dependency_bytes')},
              'raw_evidence': {'result_id': result_id, 'artifact': 'raw.json'},
              'advice_is_nonbinding': True, 'qualification': qualification,
              'evaluation_mode':raw.get('evaluation_mode','split'),'scored_partition':dataset.scored_partition(raw)}
    if qualification_error: report['qualification_error']=qualification_error
    if raw['status'] in ('failed', 'cancelled'):
        report.update(next_action='fix_first_failing_gate', first_error=raw.get('error'),
                      completed_gates=list(raw.get('gates', {})))
    elif raw['depth'] == 'screen':
        report.update(next_action='full_evaluation_required', screening=screen_summary(raw),
                      caution='Fitting-sample signal only. Small objects include process/namespace/I/O overhead; do not compare with full throughput.')
    elif raw.get('workload') == 'mutable_store':
        from .workloads.bridge import result_metrics
        report.update(next_action='inspect_mutable_latency_and_storage', mutable_metrics=result_metrics(raw))
    elif raw['depth'] != 'full':
        report.update(next_action='full_evaluation_required')
    else:
        part=dataset.scored_partition(raw)
        dev = raw.get('decoder_accounting' if raw.get('accounting_policy')=='supervisor-decoder-v1' else 'accounting', {}).get(part, {})
        timing = raw.get('timing', {})
        speed = timing.get(dataset.encoding_timing_key(policy.card), {}).get('median_bytes_per_second')
        report.update(next_action='optimize_measured_bottleneck' if speed is not None and speed<floor
                      else 'compare_paired_full_results', **{part:dev},
                      encode_floor_headroom_MBps=(speed-floor)/1e6 if speed is not None else None,
                      encoding_floor_scope=policy.card.get('timing_policy',{}).get('encoding_floor_scope','encode'),
                      timing_scope=raw.get('timing_scope','train-plus-development-v1')+'; seven complete fresh-process trials; bootstrap and I/O included; no fsync.',
                      resource_profile=raw.get('resource_profile'),decoder_costs=fixed.get('decoder'),
                      deployment_views=raw.get('deployment_views'))
        report['primary_size_policy'] = raw.get('primary_size_policy','strict-deployment-v1')
        if raw.get('reported_accounting'):
            report['strict_decoder_accounting'] = dev
            report[part] = raw['reported_accounting'][part]
        report['timing_operation'] = raw.get('timing_operation','legacy-fit-excluded-v1')
    if controller:
        report['diagnostic_action']=report['next_action']
        report['next_action']=controller.brief()['next_action']
    if raw.get('timing_operation') == 'offline-plus-online-v1':
        from .workloads.bridge import result_metrics
        report['encoding_stages'] = result_metrics(raw)['encoding_stages']
    return report
