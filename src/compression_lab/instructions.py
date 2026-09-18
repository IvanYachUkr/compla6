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
    controller = engine._controller()
    protocol = controller.state()['protocol'] if controller else {}
    floor = card['objective']['encode_floor_bytes_per_second']
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
    if card.get('hypothesis_policy') == 'required':
        text.append('Before a substantive experiment, record a short hypothesis, expected benefit and falsifier with record_hypothesis; put its experiment_id in candidate.json under hypothesis. Separate later observations from the original hypothesis.')
    timing = card.get('timing_policy', {})
    if timing.get('operation') == 'offline-plus-online-v1':
        text.append('Offline fitting and preprocessing are permitted. Declare a reproducible offline stage for fitted artifacts, precomputed representations and generated code, including dataset-dependent compilation. Measure every dataset-dependent operation from raw input to the complete archive. Report offline, online and combined encoding time; the lab sums paired stage times before aggregation without amortization. Disclose data-independent builds separately.')
        if floor is not None:
            if timing['encoding_floor_scope'] == 'combined':
                text.append(f'Deliver at least one fast mode meeting the {floor/1e6:g} MB/s end-to-end encoding floor. This is a bare minimum: seek substantially higher speed and better compression throughout the available budget. Every dataset-dependent offline step counts, including preparation of a cached archive.')
                text.append('Also pursue maximum compression. If the densest approach misses the floor, retain and optimize it as a separate maximum-compression mode alongside the qualified fast mode. One mode can serve both objectives. Report each mode\'s size, encoding-stage times, decoding speed and memory with its immutable result ID and export.')
            else:
                text.append(f'This run retains its sealed legacy rule: the {floor/1e6:g} MB/s floor applies to online encoding only. Report combined encoding time with every declared preparation step included.')
    else:
        text.append('Encoding includes every per-input transformation from raw input to the completed archive. Disclose reusable fitting and software builds separately. Precomputing an archive in fitting does not establish per-input encoding speed.')
    if floor is None:
        text.append('Minimize complete package size under the declared primary size policy. Report measured encoding, decoding and memory tradeoffs.')
    text.append('Primary size: '+card.get('primary_size_policy', 'strict-deployment-v1')+'. '+
        ('Exclude only pinned standard codec library files in the decoder inventory; charge custom binaries and every required artifact. Also report the strict deployment total. Statically linked code has no automatic deduction.'
         if card.get('primary_size_policy') == 'standard-codec-available-v1' else
         'Use the card accounting policy and preserve its strict deployment total.'))
    text.append('Evaluation is asynchronous. Save the job_id and durable paths, then poll status(job_id=job_id) until terminal. Read the result and feedback before the next experiment. Keep one evaluation active at a time and retry temporary benchmark-lease contention; waiting does not guarantee an automatic wake-up.')
    text.append('Final handoff: '+('controller_finish' if controller else 'export_and_report')+
        '. Export selected full results and write RESULT.md and CHECKPOINT.md with their immutable IDs and paths. '+
        ('Submit the selected qualified result with finish and inspect its receipt.' if controller else
         'Report completion to the supervisor.'))
    if not protocol.get('stop_on_success', False):
        text.append('Continue promising, falsifiable improvements toward the configured objective while useful research and allowances remain. When one approach stalls, investigate different mechanisms using corpus observations and small experiments. Preserve negative evidence without repeating equivalent attempts. Before finishing, cite investigated alternatives and bottlenecks by result or experiment ID, and explain why further work is unlikely to help within the remaining allowance or which limit requires stopping. Qualification verifies acceptance gates; it does not establish that research is complete.')
    if protocol.get('schema_version') == 2:
        text.append('The host checks committed evidence after each research round. A failed submission or final answer must continue while the configured time, money and failure-round allowances remain; any exhausted limit can end the run. A continuation is an additional research round, not an evaluator call or provider capacity retry. Use experiment_brief for remaining allowances. Infrastructure stops require the host. Complete evaluation and handoff in the final allowed round.')
    if protocol.get('stop_on_success', False):
        objective = protocol['objective']
        text.append('Owner-enabled completion condition: finish when a full candidate '+
                    ('qualifies.' if objective == 'qualification' else 'improves on a compatible baseline.'))
    return text


def prompt(engine):
    from .util import canonical
    return ('# Compression software research\n\nRun configuration:\n```json\n'+
            canonical(configuration(engine)).decode()+'\n```\n\n'+
            '\n\n'.join(contract(engine))+
            '\n\nUse brief for current budgets and results. Consult CANDIDATE_ABI.md only as needed.\n')


def native_prompt(config, commission):
    """Generate a native run's instructions at provisioning, from its commission."""
    goals = {'package_bytes': 'complete package size',
             'decode_seconds': 'fresh decoding time (setup plus reconstruction)'}
    text = [f'Develop byte-exact lossless compression for {commission["dataset"]}: '
            f'{commission["original_bytes"]:,} supplied bytes. Inspect the complete inputs and preserve every byte and boundary.',
            'Reduce '+' and '.join(goals[goal] for goal in commission['objectives'])+
            ' in the same configuration. Preserve useful operating points and report remaining losses. '
            'Compression speed: '+config['compression_speed']+'.',
            ('Write C or C++ compression software from scratch. Implement the mechanism yourself; do not read, copy, import or link codec implementations or supplied recipes. Standard language/runtime facilities and reimplementations of known algorithms are allowed.'
             if commission['implementation'] == 'from_scratch' else
             'Write C or C++ compression software. Existing codec libraries, custom algorithms and hybrids are permitted. Explore improvements beyond reproducing a supplied baseline.'),
            {'lf': 'LF terminates each row and belongs to that row. Preserve a final unterminated row and every other byte.',
             'nul': 'NUL terminates each row and belongs to that row. Preserve a final unterminated row, embedded line breaks and every other byte.',
             'none': 'Treat each input as one complete byte sequence.'}[config['row_framing']]]
    if 'bulk' in commission['variants']:
        text.append('Bulk variant: reconstruct each complete input column with lab_decode.')
    if 'rows' in commission['variants']:
        text.append('Row-access variant: implement lab_rows to reconstruct requested complete rows directly from relevant encoded data, without decoding the entire column or scanning all preceding rows. Evaluation uses fixed nested selections of '+
                    ', '.join(str(value)+'%' for value in config['selectivities'])+' with sorted row IDs.')
    text += [
        'Use separate native encoder and decoder libraries implementing codec.h. Put all automatic fitting, dictionary construction and preprocessing inside lab_encode, so encoding measures raw input through the complete archive. Source builds are data-independent. Decode using only the archive and charged decoder artifacts.',
        f'The RAM workload uses one pinned CPU core, {config["memory_bytes"]/1024**2:g} MiB of memory, '
        f'{config["warmups"]} warmup and {config["trials"]} measured trials. Report fresh decoder setup plus reconstruction and warm operation separately. Compare size and speed from the same configuration and machine; inspect timing spread.',
        'Charge payloads, framing, indexes, dictionaries, learned information, embedded constants and custom decoder code. Exclude only separately pinned standard codec libraries and also report the strict deployment total. Static codec code has no automatic deduction. Report archive bytes, custom decoder bytes and complete package bytes separately.',
        'Start with brief, profile and manifest_template. Work under '+str(Path(commission['workbench'])/'agent')+
        '. Before substantive algorithm changes, use record_hypothesis(statement, expected_benefit, falsifier) and put the returned hypothesis_id in the manifest. Keep ALGORITHM.md precise about the format, steps, assumptions and failure cases; cite primary sources for unfamiliar methods.',
        'For example, submit(candidate_path="agent/my-codec/candidate.json", quick=True) starts a development job. Save its job_id and poll status(job_id=job_id) until terminal; inspect the result before the next experiment. Serialize official measurements and retry temporary benchmark-lease contention. Submit with quick=False for full qualification, which checks exact reconstruction, independent decoding, reproducible builds and valid-corpus UBSan diagnostics.',
        'Use compare(result_ids=[reference_id, candidate_id]) for matched full results and export(result_id=selected_id) to preserve the measured implementation. Continue substantive improvements while useful research and commissioned allowances remain; reserve time for full validation and handoff.',
        'Keep RESULT.md and CHECKPOINT.md current with result IDs, export paths, unsuccessful directions and unmet objectives. Finish with the selected results for every commissioned variant and the report path. Qualification alone does not establish a baseline win; scientific claims require evidence and source review.'
    ]
    return '\n\n'.join(text)+'\n'


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
