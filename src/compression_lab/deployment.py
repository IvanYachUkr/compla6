"""Versioned, supplementary deployment assumptions. Standalone ranking never changes."""
from __future__ import annotations
import copy
import re
from .util import Error

EMPTY = {'schema_version': 1, 'profiles': []}
STANDARD_CODECS = {'libzstd.so.1', 'liblz4.so.1', 'libbrotlienc.so.1', 'libbrotlidec.so.1',
                   'libbrotlicommon.so.1', 'liblzma.so.5', 'libz.so.1', 'libbz2.so.1.0', 'libxxhash.so.0'}


def standard_codec_scenario():
    from .candidate import libraries
    from .util import sha
    return {'id': 'standard-codec-available-v1',
            'libraries': [{'soname': name, 'sha256': sha(path)}
                          for name, path in sorted(libraries().items()) if name in STANDARD_CODECS]}


def decoder_views(fixed, accounting, scenarios):
    """Subtract only exact standard library files from the decoder closure."""
    profiles = validate_scenarios(scenarios)['profiles']
    profile = next((p for p in profiles if p['id'] == 'standard-codec-available-v1'), None)
    if profile is None or any(x['soname'] not in STANDARD_CODECS for x in profile['libraries']):
        raise Error('missing_standard_codec_scenario')
    declared = {(x['soname'], x['sha256']) for x in profile['libraries']}
    decoder = fixed['decoder']
    matched = [x for x in decoder['dependency_inventory']['libraries']
               if not x['platform'] and (x['soname'], x['sha256']) in declared]
    deduction = sum({x['sha256']: x['bytes'] for x in matched}.values())
    if deduction > decoder['nonplatform_decoder_dependency_bytes']:
        raise Error('inconsistent_deployment_inventory')
    out = copy.deepcopy(accounting)
    for value in out.values():
        strict = value['actual']['deployment_total_bytes']
        value.update(policy='standard-codec-available-v1', strict_deployment_total_bytes=strict,
                     excluded_standard_codec_bytes=deduction,
                     matched_libraries=[{k:x[k] for k in ('soname','sha256','bytes')} for x in matched])
        for total in (value['actual'], value.get('projection')):
            if total is not None:
                total['deployment_total_bytes'] -= deduction
                total['excluded_standard_codec_bytes'] = deduction
        n = value.get('canonical_bytes', 0)
        total = value['actual']['deployment_total_bytes']
        value['total_bits_per_canonical_byte'] = 8*total/n if n else None
        value['total_compression_ratio'] = n/total if total else None
    return out


def validate_scenarios(value):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'profiles'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['profiles'], list) or len(value['profiles']) > 16):
        raise Error('invalid_runtime_scenarios')
    ids = set()
    for profile in value['profiles']:
        if not isinstance(profile, dict) or set(profile) != {'id', 'libraries'}:
            raise Error('invalid_runtime_scenario')
        name, libraries = profile['id'], profile['libraries']
        if (not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name)
                or name == 'standalone-v1' or name in ids or not isinstance(libraries, list)
                or len(libraries) > 128):
            raise Error('invalid_runtime_scenario')
        ids.add(name)
        sonames = set()
        for lib in libraries:
            if (not isinstance(lib, dict) or set(lib) != {'soname', 'sha256'}
                    or not isinstance(lib['soname'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_+.-]{1,128}', lib['soname'])
                    or lib['soname'] in ('.', '..') or lib['soname'] in sonames
                    or not isinstance(lib['sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', lib['sha256'])):
                raise Error('invalid_runtime_library')
            sonames.add(lib['soname'])
    return value


def views(fixed, accounting, scenarios=None):
    """Match actual closure by SONAME AND hash; never deduct source or license files."""
    scenarios = validate_scenarios(EMPTY if scenarios is None else scenarios)
    out = {'schema_version': 1, 'primary_scenario': 'standalone-v1', 'ranking_unchanged': True,
           'standalone': copy.deepcopy(accounting), 'installed': {},
           'scope': 'Supplementary incremental installation cost; complete export still ships its dependency closure.'}
    inventory = fixed.get('dependency_inventory', {}).get('libraries', [])
    for profile in scenarios['profiles']:
        declared = {(x['soname'], x['sha256']) for x in profile['libraries']}
        matching = [x for x in inventory if not x.get('platform', False)
                    and (x['soname'], x['sha256']) in declared]
        unique = {x['sha256']: x['bytes'] for x in matching}
        deduction = sum(unique.values())
        if deduction > fixed['nonplatform_dependency_bytes']:
            raise Error('inconsistent_deployment_inventory')
        partitions = {}
        for split, costs in accounting.items():
            partitions[split] = {
                'actual_total_bytes': costs['actual']['deployment_total_bytes'] - deduction,
                'projected_total_bytes': (costs['projection']['deployment_total_bytes'] - deduction
                                           if costs.get('projection') is not None else None),
                'horizon_sensitivity': {h: v['deployment_total_bytes'] - deduction
                                        for h, v in costs.get('horizon_sensitivity', {}).items()}}
        out['installed'][profile['id']] = {
            'assumed_installed_bytes': deduction,
            'matched_dependencies': [{k: x[k] for k in ('soname', 'sha256', 'bytes')} for x in matching],
            'unmatched_declarations': [x for x in profile['libraries']
                if (x['soname'], x['sha256']) not in {(m['soname'], m['sha256']) for m in matching}],
            'partitions': partitions}
    return out
