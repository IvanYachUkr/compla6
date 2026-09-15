"""Research instructions resolved from the sealed card, shared by every adapter."""
from pathlib import Path
from .util import Error, digest, ident, load, now, safe, save


def configuration(engine):
    card, _ = engine.metadata()
    controller = engine._controller()
    value = {key: card.get(key, default) for key, default in (
        ('evaluation_mode', 'split'), ('baseline_visibility', 'visible'),
        ('implementation_policy', 'open'), ('primary_size_policy', 'strict-deployment-v1'),
        ('hypothesis_policy', 'optional'), ('accounting_policy', 'standalone-v1'))}
    value.update(dataset_id=card['dataset_id'], card_digest=engine.state()['card_digest'], objective=card['objective'],
                 limits=card['limits'], resource_profiles=card.get('resource_profiles'),
                 timing_policy=card.get('timing_policy', {}), search_budget=card['search_budget'],
                 completion_method='controller_finish' if controller else 'export_and_report')
    if controller:
        state = controller.state()
        value['controller_protocol_digest'] = state['protocol_digest']
        if state['protocol']['schema_version']==2:
            value['completion_limits'] = {key:state['protocol'].get(key) for key in
                ('deadline_epoch','max_cost_nano','max_failure_continuations')}
        if state['protocol'].get('stop_on_success', False): value['stop_on_success'] = True
    return {**value, 'configuration_digest': digest(value)}


def contract(engine):
    card, _ = engine.metadata()
    text = [
        ('Build byte-exact lossless compression software for the complete supplied corpus, with a working encoder, independent decoder, reproducible build and documented command-line interface.'
         if card.get('evaluation_mode') == 'whole_dataset' else
         'Build byte-exact lossless compression software. Fit artifacts on training objects only; development is public validation. Private testing belongs to the owner.'),
        ('Write the compression software from scratch in C++ or supported stable Rust, implementing the compression mechanism yourself. Do not read, copy, import or link codec implementations or supplied recipes. Standard language/runtime facilities are allowed; known algorithms may be reimplemented.'
         if card.get('implementation_policy') == 'from_scratch' else
         'Use C++ or supported stable Rust. Established libraries, custom algorithms and hybrids are permitted; choose what best serves the objective.'),
        ('Supplied comparisons are hidden. Evaluate your own candidates and use their result IDs.'
         if card.get('baseline_visibility') == 'hidden' else
         'Use the supplied full baseline table when available; only compare results with matching card and resource identities.'),
    ]
    text += (Path(__file__).parent/'data/RESEARCH_GUIDANCE.md').read_text().strip().split('\n\n')
    timing = card.get('timing_policy', {})
    if timing.get('operation') == 'offline-plus-online-v1':
        text.append('Offline fitting and preprocessing are permitted, including slow preparation. Declare a reproducible offline stage for fitted artifacts, precomputed representations or generated code; include required dataset-dependent compilation in that stage. Measure every dataset-dependent operation from raw input to the complete archive. Report offline, online and combined encoding time. The lab sums paired stage times before aggregation, with no hidden amortization; data-independent software builds are disclosed separately.')
        if timing['encoding_floor_scope'] == 'combined':
            text.append('Deliver at least one fast mode that compresses the supplied raw data at the highest speed you can achieve while also improving compression. The 100 MB/s end-to-end encoding floor is a bare minimum for acceptance, not sufficient performance or a reason to stop optimizing. All dataset-dependent offline work counts toward that floor; a cached archive lookup cannot omit the work that created its archive. Seek substantially higher speed and better compression throughout the available budget.')
            text.append('Also seek the highest compression possible on this dataset. If the densest approach misses the end-to-end floor, retain and optimize it as a separate maximum-compression mode, and develop a fast mode with the best measured combination of compression and speed. A slower correct mode is a useful result but cannot replace the required qualified fast mode. Explain the measured size, end-to-end speed, online speed, offline preparation, decode speed and memory tradeoffs, using immutable result IDs and exports for each mode. One mode can serve both objectives when the evidence supports it.')
        else:
            text.append('This run retains its sealed legacy rule: the 100 MB/s floor applies to online encoding only. Combined encoding time is still reported, with every declared preparation step included.')
    else:
        text.append('Follow this run\'s legacy timing policy: encoding includes every per-input transformation and compression operation from raw input to the completed archive. Reusable fitting and software build costs are disclosed separately. Precomputing an archive in fitting does not establish per-input encoding speed.')
    text.append('Primary size: '+card.get('primary_size_policy', 'strict-deployment-v1')+'. '+
        ('Exclude only pinned standard codec library files in the decoder inventory; charge custom binaries and every required artifact. Also report the strict deployment total. Statically linked code has no automatic deduction.'
         if card.get('primary_size_policy') == 'standard-codec-available-v1' else
         'Use the card accounting policy and preserve its strict deployment total.'))
    text.append('Evaluation runs asynchronously. Retain its job_id and poll status(job=job_id) until it is terminal; then read the result and feedback before deciding the next step. Queued or running is not completion, and no automatic wake-up is guaranteed. Keep one evaluation active at a time; temporary benchmark-lease contention is a reason to retry the pending operation, not to abandon the task. Record durable paths and job IDs before any waiting turn.')
    text.append('Final handoff procedure, after the research is complete: use '+configuration(engine)['completion_method']+': '+
        ('export the selected modes, write RESULT.md and CHECKPOINT.md with their immutable result IDs and paths, then submit the selected qualified fast result and inspect the acceptance receipt.' if engine._controller() else
         'export the selected full result, write RESULT.md and CHECKPOINT.md with its IDs and paths, then report completion to the supervisor.'))
    controller = engine._controller()
    if not controller or not controller.state()['protocol'].get('stop_on_success', False):
        text.append('A qualifying result establishes a working reference point; it does not establish that the research is complete. Continue investigating promising changes to both compression and speed while useful research and configured allowances remain. If progress within one approach stalls, use corpus observations and small experiments to consider materially different mechanisms. Negative experiments are useful evidence; avoid repeating equivalent attempts merely to spend budget. Before choosing to finish, state which alternatives and bottlenecks you investigated, cite their result or experiment IDs, and explain why the remaining directions are unlikely to justify further work within the available allowance, or identify the limit that requires stopping. A successful finish receipt verifies acceptance gates, not the strength or completeness of the research.')
    if controller and controller.state()['protocol']['schema_version']==2:
        text.append('The host checks committed lab evidence after each research round. A failed submission or final answer cannot end the run while the configured time, money and continuation allowances remain. Each failure continuation is one additional research round, not an evaluator call or provider capacity retry. Any configured limit can end the run. Use experiment_brief for remaining allowances; infrastructure stops require the host. In your final allowed round, complete the research and evaluations before submitting finish and a durable handoff.')
    if controller and controller.state()['protocol'].get('stop_on_success', False):
        objective = controller.state()['protocol']['objective']
        text.append('Owner-enabled completion condition: finish when a full candidate '+
                    ('qualifies.' if objective == 'qualification' else 'improves on a compatible baseline.'))
    return text


def prompt(engine):
    from .util import canonical
    return ('# Compression software research\n\nRun configuration:\n```json\n'+
            canonical(configuration(engine)).decode()+'\n```\n\n'+
            '\n\n'.join(contract(engine))+
            '\n\nUse brief for current budgets and results. Consult CANDIDATE_ABI.md only as needed.\n')


def record_hypothesis(engine, statement, expected_benefit, falsifier):
    engine._guard_research()
    state = engine.state()
    if state['stage'] not in ('public_search', 'public_ready'):
        raise Error('terminal_latch')
    fields = dict(statement=statement, expected_benefit=expected_benefit, falsifier=falsifier)
    if any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in fields.values()):
        raise Error('invalid_hypothesis', 'Supply three short, nonempty statements')
    value = dict(schema_version=1, run_id=state['run_id'], card_digest=state['card_digest'],
                 created_at=now(), **fields)
    value['experiment_id'] = 'h-'+digest(value)
    save(engine.root/'hypotheses'/(value['experiment_id']+'.json'), value, 0o444)
    return value


def verify_hypothesis(engine, manifest):
    experiment = manifest.get('hypothesis', {}).get('experiment_id')
    required = engine.metadata()[0].get('hypothesis_policy') == 'required'
    if experiment is None:
        if required: raise Error('hypothesis_required', 'Record the experiment before registration and include hypothesis.experiment_id')
        return None
    ident(experiment)
    value = load(safe(engine.root/'hypotheses', experiment+'.json'))
    state = engine.state()
    if (value.get('experiment_id') != experiment or 'h-'+digest({k:v for k,v in value.items() if k!='experiment_id'}) != experiment
            or value.get('run_id') != state['run_id'] or value.get('card_digest') != state['card_digest']):
        raise Error('stale_hypothesis')
    return value
