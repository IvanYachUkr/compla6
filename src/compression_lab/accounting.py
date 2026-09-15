"""Shared arithmetic: decimal throughput, source/deployment views, paired groups."""
import math,random,statistics
from .dataset import scored_partition, encoding_timing_key
from .util import Error

def timing(trials,phase):
 if len(trials)!=7 or any(t.get('elapsed_ns',0)<=0 or t.get('canonical_bytes',-1)<0 for t in trials):raise Error('incomplete_timing','Exactly seven complete trials required')
 speeds=[t['canonical_bytes']*1e9/t['elapsed_ns'] for t in trials];med=statistics.median(speeds);mad=statistics.median(abs(x-med) for x in speeds)
 return {'median_bytes_per_second':med,'median_decimal_MB_per_second':med/1e6,'median_seconds':statistics.median(t['elapsed_ns']/1e9 for t in trials),'relative_MAD':mad/med if med else 0,'relative_range':(max(speeds)-min(speeds))/med if med else 0,'min_bytes_per_second':min(speeds),'max_bytes_per_second':max(speeds),'peak_rss_bytes':max(t['peak_rss_bytes'] for t in trials),'trials':7,'phase':phase}

def combine_encoding_trials(offline,online):
 """Pair complete preparation and encoding measurements before aggregation."""
 if len(online)!=7 or (offline and len(offline)!=7):raise Error('incomplete_encoding_pipeline')
 combined=[]
 for i,encode in enumerate(online,1):
  fit=offline[i-1] if offline else {'trial':i,'elapsed_ns':0,'peak_rss_bytes':0}
  if encode['trial']!=i or fit['trial']!=i or encode['elapsed_ns']<=0 or fit['elapsed_ns']<0:raise Error('unpaired_encoding_pipeline')
  combined.append({'trial':i,'canonical_bytes':encode['canonical_bytes'],
   'elapsed_ns':fit['elapsed_ns']+encode['elapsed_ns'],
   'offline_elapsed_ns':fit['elapsed_ns'],'online_elapsed_ns':encode['elapsed_ns'],
   'peak_rss_bytes':max(fit['peak_rss_bytes'],encode['peak_rss_bytes']),
   'derivation':'sum of measured stages from this trial; evaluator staging/checks excluded'})
 return combined

def costs(n,a,fixed,horizon):
 def totals(x):return {'archive_bytes':x,'historical_primary_bytes':x+fixed['fixed_bytes']+fixed['packed_source_bytes'],'deployment_total_bytes':x+fixed['fixed_bytes']+fixed['config_bytes']+fixed['binary_bytes']+fixed['nonplatform_dependency_bytes'],'historical_additive_source_binary_bytes':x+fixed['fixed_bytes']+fixed['packed_source_bytes']+fixed['binary_bytes']+fixed['nonplatform_dependency_bytes']}
 rate=a/n if n else None
 return {'canonical_bytes':n,'archive_ratio':rate,'bits_per_canonical_byte':rate*8 if rate is not None else None,'compression_ratio':n/a if a else None,'actual':totals(a),'deployment_horizon_bytes':horizon,'projection':totals(math.ceil(rate*horizon)) if rate is not None else None,'horizon_sensitivity':{str(h):totals(math.ceil(rate*h)) for h in sorted({horizon//10,horizon,horizon*10})} if rate is not None else {},'projection_assumption':'Observed byte-weighted archive ratio; constant object-size distribution; fixed package charged once. Not a prediction of unseen-data compression.'}

def decoder_costs(n,a,fixed,horizon):
 """Supervisor score from the independently staged physical decoder inventory."""
 decoder=fixed['decoder'];b=decoder['compiled_decoder_bytes'];artifacts=decoder['required_decoder_artifact_bytes'];d=decoder['nonplatform_decoder_dependency_bytes']
 def totals(v):
  return {'archive_bytes':v,'compiled_decoder_bytes':b,'required_decoder_artifact_bytes':artifacts,'nonplatform_decoder_dependency_bytes':d,'compressed_plus_decoder_bytes':v+b,'deployment_total_bytes':v+b+artifacts+d}
 rate=a/n if n else None
 return {'policy':'supervisor-decoder-v1','canonical_bytes':n,'archive_ratio':rate,'bits_per_canonical_byte':rate*8 if rate is not None else None,'compression_ratio':n/a if a else None,'actual':totals(a),'deployment_horizon_bytes':horizon,'projection':totals(math.ceil(rate*horizon)) if rate is not None else None,'rank_scope':'actual fixed validation workload; projection is supplementary','source_distribution_bytes_reported_separately':fixed['packed_source_bytes'],'encoder_only_binary_bytes_reported_separately':fixed.get('encoder_only_binary_bytes_reported_separately',0),'platform_assumption':fixed['installed_runtime_policy']}

def compare(rows,reference,seed=20260906,replicates=1000):
 a={r['alias']:r for r in rows};b={r['alias']:r for r in reference}
 if set(a)!=set(b):raise Error('unpaired_comparison')
 groups={}
 for k,r in a.items():
  if r['canonical_bytes']!=b[k]['canonical_bytes'] or r['group']!=b[k]['group']:raise Error('unpaired_comparison')
  g=groups.setdefault(r['group'],[0,0]);g[0]+=r['archive_bytes'];g[1]+=b[k]['archive_bytes']
 vals=list(groups.values());rng=random.Random(seed);samples=[]
 for _ in range(replicates):
  draw=[rng.choice(vals) for __ in vals];x=sum(x[0] for x in draw);y=sum(x[1] for x in draw);samples.append((x-y)/y if y else 0)
 samples.sort()
 return {'groups':groups,'seed':seed,'replicates':replicates,'paired_group_bootstrap_relative_delta_95_interval':[samples[int(.025*replicates)],samples[min(replicates-1,int(.975*replicates))]],'tail_regressions':sorted([{'alias':k,'delta_bytes':a[k]['archive_bytes']-b[k]['archive_bytes']} for k in a],key=lambda r:r['delta_bytes'],reverse=True)[:5],'caveat':'Descriptive group bootstrap; tiny public smoke groups do not support generalization claims.'}


def paired_diagnostics(reference, candidate):
    """Bounded descriptive feedback; both arguments must be verified static results."""
    for raw in (reference, candidate):
        if (raw.get('depth') != 'full' or not raw.get('quality_passed')
                or raw.get('workload', 'independent_objects') == 'mutable_store'):
            raise Error('diagnostics_require_full_static_results')
    part = scored_partition(candidate)
    if part != scored_partition(reference):
        raise Error('incomparable_evaluation_modes')
    a = sorted((r for r in candidate.get('objects', []) if r['split'] == part), key=lambda r: r['alias'])
    b = sorted((r for r in reference.get('objects', []) if r['split'] == part), key=lambda r: r['alias'])
    if not a or not b or any(len({r['alias'] for r in rows}) != len(rows) for rows in (a, b)):
        raise Error('unpaired_comparison')
    paired = compare(a, b)
    ca, ra = (raw['accounting'][part] for raw in (candidate, reference))
    if ca['deployment_horizon_bytes'] != ra['deployment_horizon_bytes']:
        raise Error('incomparable_deployment_horizons')
    if part == 'corpus':
        dc, dr = (raw['decoder_accounting'][part]['actual'] for raw in (candidate, reference))
        components = {key: dc[key] - dr[key] for key in ('archive_bytes', 'compiled_decoder_bytes',
                      'required_decoder_artifact_bytes', 'nonplatform_decoder_dependency_bytes')}
        delta = dc['deployment_total_bytes'] - dr['deployment_total_bytes']
    else:
        components = {'projected_archive_bytes': ca['projection']['archive_bytes'] - ra['projection']['archive_bytes']}
        for label, key in [('artifact_and_runtime_bytes', 'fixed_bytes'), ('config_bytes', 'config_bytes'),
                           ('binary_bytes', 'binary_bytes'), ('nonplatform_dependency_bytes', 'nonplatform_dependency_bytes')]:
            components[label] = candidate['fixed_costs'][key] - reference['fixed_costs'][key]
        delta = ca['projection']['deployment_total_bytes'] - ra['projection']['deployment_total_bytes']
    if sum(components.values()) != delta:
        raise Error('inconsistent_deployment_accounting')
    primary = candidate.get('primary_size_policy','strict-deployment-v1')
    if primary != reference.get('primary_size_policy','strict-deployment-v1'):
        raise Error('incomparable_primary_size_policies')
    strict_delta = delta
    if primary == 'standard-codec-available-v1':
        dc, dr = (raw['reported_accounting'][part] for raw in (candidate, reference))
        # Rebuild from actual decoder terms for both split and whole-corpus cards.
        cs, rs = (raw['decoder_accounting'][part]['actual'] for raw in (candidate, reference))
        components = {key:cs[key]-rs[key] for key in ('archive_bytes','compiled_decoder_bytes',
                       'required_decoder_artifact_bytes','nonplatform_decoder_dependency_bytes')}
        strict_delta = cs['deployment_total_bytes']-rs['deployment_total_bytes']
        components['standard_codec_availability_adjustment'] = dr['excluded_standard_codec_bytes']-dc['excluded_standard_codec_bytes']
        delta = dc['actual']['deployment_total_bytes']-dr['actual']['deployment_total_bytes']
        if sum(components.values()) != delta: raise Error('inconsistent_primary_accounting')
    group_bytes = {}
    for r in a:
        group_bytes[r['group']] = group_bytes.get(r['group'], 0) + r['canonical_bytes']
    groups = [{'group': g, 'archive_delta_bytes': x - y, 'candidate_archive_bytes': x,
               'reference_archive_bytes': y, 'canonical_bytes': group_bytes[g]}
              for g, (x, y) in paired['groups'].items()]
    groups.sort(key=lambda r: (r['archive_delta_bytes'], r['group']))

    def timing_view(raw):
        encode, decode = raw['timing'][encoding_timing_key(raw)], raw['timing']['decode']
        speed = encode['median_bytes_per_second']
        return {'encode_bytes_per_second': speed, 'decode_bytes_per_second': decode['median_bytes_per_second'],
                'encode_floor_bytes_per_second': 100_000_000,
                'encoding_floor_scope': raw.get('encoding_floor_scope', 'encode'),
                'encode_floor_headroom_bytes_per_second': speed - 100_000_000,
                'encode_relative_MAD': encode['relative_MAD'], 'decode_relative_MAD': decode['relative_MAD']}

    n = sum(group_bytes.values())
    return {'schema_version': 1, 'direction': 'candidate_minus_reference', 'split': part,
            'deployment_delta_scope': 'actual scored partition; standard codec libraries available' if primary == 'standard-codec-available-v1' else 'actual corpus V+B+A+D' if part == 'corpus' else 'legacy projected standalone package',
            'primary_size_policy':primary, 'strict_deployment_delta_bytes':strict_delta,
            'deployment_delta_bytes': delta, 'deployment_components_delta_bytes': components,
            'packed_source_delta_bytes_reported_separately': candidate['fixed_costs']['packed_source_bytes'] - reference['fixed_costs']['packed_source_bytes'],
            part + '_archive_delta_bytes': ca['actual']['archive_bytes'] - ra['actual']['archive_bytes'],
            'deployment_horizon_bytes': ca['deployment_horizon_bytes'], 'objects': len(a), 'groups': len(groups),
            'largest_group_canonical_byte_share': max(group_bytes.values()) / n if n else None,
            'best_groups': [r for r in groups if r['archive_delta_bytes'] < 0][:5],
            'worst_groups': [r for r in reversed(groups) if r['archive_delta_bytes'] > 0][:5],
            'worst_object_regressions': [r for r in paired['tail_regressions'] if r['delta_bytes'] > 0],
            'paired_group_bootstrap_relative_delta_95_interval': paired['paired_group_bootstrap_relative_delta_95_interval'],
            'bootstrap_seed': paired['seed'], 'bootstrap_replicates': paired['replicates'],
            'reference_timing': timing_view(reference), 'candidate_timing': timing_view(candidate),
            'timing_scope': candidate.get('timing_scope', 'train_plus_development') + '; seven fresh processes; startup and I/O included; warm cache; no fsync',
            'caveat': ('Entire supplied corpus is visible and may be overfit; descriptive group bootstrap excludes fixed costs and shared batch header. No held-out generalization claim.' if part == 'corpus' else 'Reused public development evidence; descriptive group bootstrap excludes fixed costs and shared batch header. Few or dominant groups limit interpretation. No independent-test or automatic-winner claim.')}
