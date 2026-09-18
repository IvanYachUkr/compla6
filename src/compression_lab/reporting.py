"""Small, evidence-preserving tables and Pareto plots for native benchmarks.

The CSV is the interchange format. Matplotlib is imported only for --plots.
Reporting does not run benchmarks or change their recorded evidence.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys
import textwrap


FIELDS = (
    'report_schema', 'dataset', 'workload', 'workload_id', 'machine_id', 'machine',
    'protocol', 'protocol_label', 'parameters_id', 'smoke', 'source', 'source_sha256',
    'supporting_sources',
    'result_id', 'method', 'label', 'original_bytes', 'archive_bytes',
    'custom_decoder_bytes', 'package_bytes', 'strict_deployment_bytes',
    'excluded_standard_codec_bytes', 'primary_size', 'encode_MB_s',
    'fresh_decode_MB_s', 'warm_decode_MB_s', 'selectivity_percent',
    'fresh_decode_Mrows_s', 'warm_decode_Mrows_s', 'fresh_decode_ms', 'warm_decode_ms',
    'checks', 'notes', 'size_basis', 'size_bytes', 'compression_factor',
    'decode_mode', 'decode_value', 'decode_unit', 'pareto',
)
INTEGER_FIELDS = {f for f in FIELDS if f.endswith('_bytes')} | {'report_schema'}
FLOAT_FIELDS = {f for f in FIELDS if f.endswith(('_MB_s', '_Mrows_s', '_ms'))} | {
    'selectivity_percent', 'compression_factor', 'decode_value'}
BOOL_FIELDS = {'smoke', 'pareto'}
GROUP_FIELDS = ('dataset', 'workload_id', 'workload', 'machine_id', 'protocol',
                'parameters_id', 'smoke', 'selectivity_percent', 'size_basis',
                'decode_mode', 'decode_unit')
PROTOCOLS = {
    'whole-ram-v1': 'Native RAM; fresh setup; median of 3 whole-corpus passes',
    'strings-v1': 'Native RAM; median of 7 summed-column passes after 1 warmup',
    'fsst-paper-selective-v1': 'FSST paper; geometric mean across columns; median of 3 replays',
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text())


def _support(*paths, existing=None):
    sources = json.loads(existing) if existing else {}
    sources.update({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()})
    return json.dumps(sources, sort_keys=True)


def _context(path, protocol):
    receipt = path.parent / 'run-metadata.json'
    if not receipt.exists() and path.parent.name == 'results':
        receipt = path.parent.parent / 'run-metadata.json'
    metadata = _json(receipt) if receipt.exists() else {}
    if metadata.get('protocol', protocol) != protocol:
        raise ValueError(f'{path}: metadata protocol differs from this result format')
    legacy = path.parent.parent.name == 'recorded' and path.parent.name in {'whole', 'dbtext', 'fsst-paper'}
    supports = [receipt]
    if legacy:
        family = 'whole/inputs.json' if path.parent.name == 'whole' else 'dbtext/columns.json'
        supports.append(path.parent.parent.parent / family)
    parameters = metadata.get('parameters', {})
    protocol_label = PROTOCOLS[protocol] if legacy else protocol + '; timing parameters not supplied'
    if metadata:
        count = parameters.get('replays' if protocol == 'fsst-paper-selective-v1' else 'trials')
        protocol_label = protocol + (f"; {count} measured pass{'es' if count != 1 else ''}" if count is not None else '')
        if parameters.get('aggregation'):
            protocol_label += '; ' + parameters['aggregation']
    machine = metadata.get('machine_label') or (
        'Recorded server evidence; hardware fingerprint unavailable' if legacy else
        'Machine unspecified; comparison limited to this input source')
    if parameters.get('cpu') is not None:
        machine += f"; CPU {parameters['cpu']}"
    context = dict(protocol=metadata.get('protocol', protocol),
                   protocol_label=metadata.get('protocol_label', protocol_label),
                   machine_id=metadata.get('machine_id') or 'source-local:' + _digest(str(path.parent if legacy else path)),
                   machine=machine, parameters_id=_digest(parameters),
                   smoke=metadata.get('smoke', False), source=str(path),
                   source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                   supporting_sources=_support(*supports))
    return context, metadata, legacy


def _row(context, **values):
    row = dict.fromkeys(FIELDS)
    row.update(context)
    row.update(report_schema=1, **values)
    return row


def _check_metadata(metadata, original, *, input_sha=None, columns=None):
    """A copied receipt must not make unrelated measured inputs comparable."""
    if original is not None and metadata.get('original_bytes') is not None and original != metadata['original_bytes']:
        raise ValueError('Result original byte count differs from metadata')
    pins = metadata.get('inputs', [])
    if pins and original is not None and original != sum(p['bytes'] for p in pins):
        raise ValueError('Result original byte count differs from metadata input pins')
    if input_sha and pins and (len(pins) != 1 or input_sha != pins[0]['sha256']):
        raise ValueError('Result input SHA-256 differs from metadata')
    if columns and pins:
        identity = lambda items: sorted((p['name'], p['bytes'], p['sha256']) for p in items)
        if identity(columns) != identity(pins):
            raise ValueError('Result columns differ from metadata input pins')


def _pins(path, family):
    """Read shipped input pins only for a recorded comparison, never a new run."""
    root = path.parent.parent.parent
    pins = root / family / ('inputs.json' if family == 'whole' else 'columns.json')
    return _json(pins) if pins.is_file() else None


def _whole(path, entries, header):
    context, metadata, legacy = _context(path, 'whole-ram-v1')
    context['smoke'] = metadata.get('smoke', header.get('smoke', False))
    pins = _pins(path, 'whole') if legacy else None
    rows = []
    for item in entries:
        dataset = item.get('dataset', header.get('dataset', metadata.get('dataset', 'whole')))
        trials = item.get('trials', [])
        pin = (pins or {}).get(dataset, {})
        original = item.get('original_bytes', header.get('original_bytes', metadata.get('original_bytes', pin.get('bytes'))))
        if trials:
            if any(t.get('exact') is not True for t in trials):
                raise ValueError(f"{path}: inexact or unchecked whole-corpus trial")
            if len({t['raw_bytes'] for t in trials}) != 1 or len({t['archive_bytes'] for t in trials}) != 1:
                raise ValueError(f"{path}: inconsistent trial sizes")
            if original is not None and int(original) != trials[0]['raw_bytes']:
                raise ValueError(f'{path}: input byte count differs from metadata')
            original = trials[0]['raw_bytes']
            if int(item['archive_bytes']) != trials[0]['archive_bytes']:
                raise ValueError(f"{path}: summary archive size differs from trials")
        _check_metadata(metadata, original, input_sha=header.get('input_sha256'))
        speeds = {phase: float(item[phase + '_MB_s']) for phase in ('encode', 'decode')}
        if trials:
            for phase in speeds:
                if any(not math.isfinite(t[phase + '_seconds']) or t[phase + '_seconds'] <= 0 for t in trials):
                    raise ValueError(f'{path}: invalid {phase} trial duration')
                measured = original / statistics.median(t[phase + '_seconds'] for t in trials) / 1e6
                if not math.isclose(speeds[phase], measured, rel_tol=1e-9):
                    raise ValueError(f"{path}: {phase} summary differs from trials")
        label = item.get('label', item['id'])
        notes = []
        if '†' in label:
            notes.append('Provisional Lab qualification; RAM checks do not complete the separate Lab gates.')
        if item['id'] == 'python-glm-fast' and legacy:
            notes.append('Recorded malformed-archive concern; RAM roundtrips are not full qualification.')
        rows.append(_row(context, dataset=dataset, workload='whole corpus',
            workload_id=metadata.get('workload_id') or header.get('input_sha256') or pin.get('sha256') or
                        'source-local:' + _digest([str(path), dataset]),
            result_id=item.get('result_id'), method=item['id'], label=label,
            original_bytes=original, archive_bytes=int(item['archive_bytes']),
            primary_size='archive', encode_MB_s=speeds['encode'], fresh_decode_MB_s=speeds['decode'],
            checks='byte-exact trials passed' if trials else 'see source evidence', notes=' '.join(notes) or None))
    return rows


def _native(path, entries, is_csv=False):
    context, metadata, legacy = _context(path, 'strings-v1')
    pins = _pins(path, 'dbtext') if legacy else None
    rows = []
    for item in entries:
        if item.get('quality_passed') is False or item.get('status') == 'failed':
            raise ValueError(f"{path}: failed result {item.get('result_id', '')}")
        variant = item['workload'].lower() if is_csv else item['variant']
        name = item['method'] if is_csv else item['name']
        accounting = item if is_csv else item['accounting']
        amounts = {key: int(accounting[key]) for key in (
            'original_bytes', 'archive_bytes', 'custom_decoder_bytes', 'package_bytes',
            'strict_deployment_bytes', 'excluded_standard_codec_bytes') if accounting.get(key) is not None}
        amounts['original_bytes'] = int(item['original_bytes'])
        _check_metadata(metadata, amounts['original_bytes'], columns=item.get('columns'))
        row = _row(context, **amounts, dataset=metadata.get('dataset', 'DBText' if legacy else 'strings'),
            workload=variant, workload_id=metadata.get('workload_id') or item.get('workload_digest') or
                (_digest(pins) if pins else 'source-local:' + _digest(str(path))),
            method=name, label=name, result_id=item.get('result_id'), primary_size='package',
            checks='byte-exact checks passed' if item.get('quality_passed') else 'see source evidence',
            notes='Charged package includes archive and custom decoder; identified standard codec libraries excluded.')
        row['smoke'] = bool(context['smoke'] or item.get('depth') == 'quick')
        if item.get('depth') == 'quick':
            row.update(protocol_label='Native RAM quick; 1 measured pass after 1 warmup',
                       parameters_id=_digest({**metadata.get('parameters', {}), 'trials': 1, 'warmups': 1}))
        for target, source in (('encode_MB_s', 'encode_seconds'), ('fresh_decode_MB_s', 'decode_seconds'),
                               ('warm_decode_MB_s', 'decode_warm_seconds')):
            row[target] = float(item[target]) if is_csv else item['timing'][source]['work_per_second'] / 1e6
        rows.append(row)
        for percent, timings in item.get('selective', {}).items():
            selected = dict(row, workload='selected rows', selectivity_percent=float(percent),
                            encode_MB_s=None, fresh_decode_MB_s=None, warm_decode_MB_s=None)
            for mode, source in (('fresh', 'cold'), ('warm', 'warm')):
                selected[mode + '_decode_Mrows_s'] = timings[source]['work_per_second'] / 1e6
                selected[mode + '_decode_ms'] = timings[source]['median_seconds'] * 1000
            rows.append(selected)
    return rows


def _paper(path, data):
    context, metadata, legacy = _context(path, 'fsst-paper-selective-v1')
    pins = _pins(path, 'dbtext') if legacy else None
    original = metadata.get('original_bytes')
    if original is None and pins:
        original = sum(p['bytes'] for p in pins)
    if isinstance(data, dict) and 'krows_per_second' in data:
        trials_path = path.parent / 'trials.json'
        if not trials_path.is_file():
            raise ValueError(f'{path}: paper summary needs adjacent trials.json for archive sizes')
        rows = _paper(trials_path, _json(trials_path))
        for row in rows:
            score = data['krows_per_second'][row['method']][str(int(row['selectivity_percent']))] / 1000
            if not math.isclose(score, row['warm_decode_Mrows_s'], rel_tol=1e-9):
                raise ValueError(f'{path}: paper summary differs from trials')
            row.update(source=str(path), source_sha256=context['source_sha256'],
                       supporting_sources=_support(trials_path, existing=row['supporting_sources']))
        return rows
    summaries = data
    if isinstance(data, list):
        summaries = {}
        for method in sorted({r['method'] for r in data}):
            trials = [r for r in data if r['method'] == method]
            if len({r['archive_bytes'] for r in trials}) != 1:
                raise ValueError(f'{path}: inconsistent paper archive sizes')
            measurements = [r.get('krows_per_second') or {
                p: m['krows_per_second'] for p, m in r['measurements'].items()} for r in trials]
            if any(set(m) != set(measurements[0]) for m in measurements):
                raise ValueError(f'{path}: inconsistent paper selectivities')
            if any(not math.isfinite(score) or score <= 0 for m in measurements for score in m.values()):
                raise ValueError(f'{path}: invalid paper throughput')
            summaries[method] = dict(archive_bytes=trials[0]['archive_bytes'],
                krows_per_second={p: statistics.median(m[p] for m in measurements) for p in measurements[0]})
    labels = {'fsst': 'FSST', 'lz4': 'LZ4 (1000-row blocks)', 'onpairplus': 'OnPair+', 'astra': 'Astra rows'}
    rows = []
    for method, item in summaries.items():
        for percent, score in item['krows_per_second'].items():
            rows.append(_row(context, dataset=metadata.get('dataset', 'DBText' if legacy else 'strings'),
                workload='selected rows', workload_id=metadata.get('workload_id') or
                    (_digest(pins) if pins else 'source-local:' + _digest(str(path))),
                method=method, label=labels.get(method, method), original_bytes=original,
                archive_bytes=item['archive_bytes'], primary_size='archive',
                selectivity_percent=float(percent), warm_decode_Mrows_s=score / 1000,
                checks='see source evidence', notes='Warm initialized decoder; paper framing/padding; no MB/s conversion.'))
    return rows


def load_results(path):
    """Read whole/native/paper JSON, their recorded CSV, or this module's CSV."""
    path = Path(path).resolve()
    if path.is_dir():
        for name in ('summary.json', 'native-results.csv', 'trials.json'):
            if (path / name).is_file():
                return load_results(path / name)
        raise ValueError(f'{path}: no summary.json, native-results.csv or trials.json')
    if path.suffix.lower() == '.csv':
        with path.open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        if rows and 'report_schema' in rows[0]:
            result = []
            for source in rows:
                row = {key: source.get(key) or None for key in FIELDS}
                for key in INTEGER_FIELDS:
                    if row[key] is not None:
                        row[key] = int(row[key])
                for key in FLOAT_FIELDS:
                    if row[key] is not None:
                        row[key] = float(row[key])
                for key in BOOL_FIELDS:
                    if row[key] not in (None, 'True', 'False'):
                        raise ValueError(f'{path}: invalid {key}')
                    row[key] = row[key] == 'True'
                if row['report_schema'] != 1:
                    raise ValueError(f'{path}: unsupported reporting schema')
                result.append(row)
            return result
        if rows and 'fresh_decode_MB_s' in rows[0]:
            return _native(path, rows, is_csv=True)
        if rows and 'decode_MB_s' in rows[0]:
            return _whole(path, rows, {})
        if rows and 'rows_selected_percent' in rows[0]:
            companion = path.parent / 'RESULT.json'
            if not companion.is_file():
                raise ValueError(f'{path}: paper CSV needs adjacent RESULT.json for measured archive sizes')
            result = load_results(companion)
            scores = {float(r['rows_selected_percent']): r for r in rows}
            for row in result:
                score = float(scores[row['selectivity_percent']][row['method'] + '_Mrows_s'])
                if not math.isclose(score, row['warm_decode_Mrows_s'], rel_tol=1e-9):
                    raise ValueError(f'{path}: paper CSV differs from adjacent size/score evidence')
                row.update(source=str(path), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                           supporting_sources=_support(companion, existing=row['supporting_sources']))
            return result
        raise ValueError(f'{path}: unsupported CSV columns')
    data = _json(path)
    if isinstance(data, list):
        if data and 'trials' in data[0]:
            return _whole(path, data, {})
        if data and 'replay' in data[0]:
            return _paper(path, data)
    if isinstance(data, dict):
        if 'accounting' in data and 'timing' in data:
            return _native(path, [data])
        entries = data.get('rows', data.get('results', []))
        if entries and 'accounting' in entries[0]:
            return _native(path, entries)
        if entries and 'decode_MB_s' in entries[0]:
            return _whole(path, entries, data)
        if 'krows_per_second' in data or data and all(
                isinstance(v, dict) and 'krows_per_second' in v for v in data.values()):
            return _paper(path, data)
    raise ValueError(f'{path}: unsupported or empty benchmark result')


def group_key(row):
    """Comparison boundaries, including size convention and decoder state."""
    return tuple(str(row.get(key) if row.get(key) is not None else '') for key in GROUP_FIELDS)


def groups(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(group_key(row), []).append(row)
    def order(item):
        key = list(item[0])
        index = GROUP_FIELDS.index('selectivity_percent')
        key[index] = float(key[index]) if key[index] else -1
        return tuple(key)
    return dict(sorted(grouped.items(), key=order))


def prepare(rows, size='auto', decode='auto'):
    """Select measured views and mark strict nondominance; equal points survive."""
    if size not in ('auto', 'archive', 'package') or decode not in ('auto', 'fresh', 'warm'):
        raise ValueError('Unknown size or decode view')
    result = []
    for source in rows:
        row = dict.fromkeys(FIELDS)
        row.update(source)
        row['report_schema'] = 1
        row['smoke'] = bool(row.get('smoke'))
        for key in INTEGER_FIELDS | FLOAT_FIELDS:
            value = row.get(key)
            if value is not None and (not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0):
                # Zero excluded-library bytes is a real measurement, not missing.
                if value == 0 and key in {'custom_decoder_bytes', 'excluded_standard_codec_bytes'}:
                    continue
                raise ValueError(f"{row.get('method')}: {key} must be positive and finite")
            if key in INTEGER_FIELDS and value is not None and int(value) != value:
                raise ValueError(f"{row.get('method')}: {key} must be an integer")
        if row['selectivity_percent'] is not None and row['selectivity_percent'] > 100:
            raise ValueError(f"{row['method']}: selectivity exceeds 100 percent")
        for larger, smaller in (('package_bytes', 'archive_bytes'), ('strict_deployment_bytes', 'package_bytes')):
            if row.get(larger) is not None and row.get(smaller) is not None and row[larger] < row[smaller]:
                raise ValueError(f"{row['method']}: {larger} is smaller than {smaller}")
        row['size_basis'] = row['primary_size'] if size == 'auto' else size
        row['size_bytes'] = row.get(row['size_basis'] + '_bytes')
        if row['size_bytes'] is None:
            raise ValueError(f"{row['method']}: {row['size_basis']} bytes are not measured")
        row['compression_factor'] = row['original_bytes'] / row['size_bytes'] if row['original_bytes'] else None
        unit, suffix = ('Mrows/s', 'Mrows_s') if row['selectivity_percent'] is not None else ('MB/s', 'MB_s')
        mode = decode
        if mode == 'auto':
            mode = 'fresh' if row.get('fresh_decode_' + suffix) is not None else 'warm'
        row.update(decode_mode=mode, decode_unit=unit, decode_value=row.get(mode + '_decode_' + suffix), pareto=False)
        if row['decode_value'] is None:
            raise ValueError(f"{row['method']}: {mode} decode {unit} is not measured")
        result.append(row)
    for points in groups(result).values():
        if len({p['original_bytes'] for p in points}) != 1:
            raise ValueError('Matching workload identity has inconsistent original byte counts')
        for row in points:
            row['pareto'] = row['compression_factor'] is not None and not any(
                other['compression_factor'] is not None and
                other['size_bytes'] <= row['size_bytes'] and
                other['decode_value'] >= row['decode_value'] and
                (other['size_bytes'] < row['size_bytes'] or other['decode_value'] > row['decode_value'])
                for other in points)
    return sorted(result, key=lambda r: (group_key(r), str(r['label']).casefold(), str(r['method']),
                                         str(r['result_id']), str(r['source'])))


def _text(value):
    return str(value or '').replace('|', '\\|').replace('\n', ' ')


def _number(value, digits=1):
    if value is None:
        return '—'
    return f'{value:.3g}' if 0 < abs(value) < 10**-digits else f'{value:,.{digits}f}'


def _title(row):
    selection = '' if row['selectivity_percent'] is None else f" · {row['selectivity_percent']:g}% selected"
    return f"{row['dataset']} · {row['workload']}{selection}"


def markdown(rows):
    lines = ['# Compression benchmark report', '',
        'MB = 1,000,000 bytes. Factor = original bytes / the stated size. Higher factor and throughput are better.',
        'Frontiers use the displayed size and decoder state within each matching workload, machine and protocol. '
        'They are descriptive; they do not establish significance or complete Lab qualification.', '',
        'Blank values (—) were not measured or supplied. Raw measurements are unchanged.', '']
    for index, points in enumerate(groups(rows).values(), 1):
        row = points[0]
        lines.extend([f"## {index}. {_text(_title(row))}", '',
                      _text(row['machine']) + '. ' + _text(row['protocol_label']) + '.', '',
                      f"Size: **{row['size_basis']}**. Plot decoder state: **{row['decode_mode']}**. "
                      f"Context `{_digest(group_key(row))[:10]}`. Original: {_number(row['original_bytes'], 0)} bytes.", ''])
        if row['smoke']:
            lines.extend(['**SMOKE RUN — wiring check, not a performance result.**', ''])
        is_rows = row['decode_unit'] == 'Mrows/s'
        unit = 'Mrows/s' if is_rows else 'MB/s'
        suffix = 'Mrows_s' if is_rows else 'MB_s'
        headers = ['Method', 'Archive MB']
        if any(p['package_bytes'] is not None for p in points):
            headers.append('Package MB')
        headers += [f"{row['size_basis'].title()} factor", 'Compress MB/s', f'Decompress fresh {unit}', f'Decompress warm {unit}']
        if any(p['fresh_decode_ms'] is not None for p in points):
            headers += ['Fresh ms', 'Warm ms']
        headers += ['Frontier']
        lines += ['| ' + ' | '.join(headers) + ' |', '|:---|' + '---:|' * (len(headers)-1)]
        for point in points:
            values = [_text(point['label']), _number(point['archive_bytes'] / 1e6, 3)]
            if 'Package MB' in headers:
                values.append(_number(point['package_bytes'] / 1e6 if point['package_bytes'] is not None else None, 3))
            values += [_number(point['compression_factor'], 3) + ('×' if point['compression_factor'] is not None else ''),
                       _number(point['encode_MB_s']), _number(point['fresh_decode_' + suffix]),
                       _number(point['warm_decode_' + suffix])]
            if 'Fresh ms' in headers:
                values += [_number(point['fresh_decode_ms'], 3), _number(point['warm_decode_ms'], 3)]
            values += ['yes' if point['pareto'] else '—']
            lines.append('| ' + ' | '.join(values) + ' |')
        lines += ['']
        lines.append('Archive includes framing and dictionaries; decoder code excluded.' if row['size_basis'] == 'archive' else
                     'Charged package includes custom reconstruction information; strict deployment bytes remain in the CSV.')
        lines.extend(_text(note) for note in sorted({p['notes'] for p in points if p['notes']}))
        lines += ['', 'Sources: ' + '; '.join(f'`{_text(source)}`' for source in sorted({p['source'] for p in points})), '']
    lines += ['Exact byte counts, both decoder states, strict deployment totals, source SHA-256 hashes and result IDs are retained in `results.csv`.', '']
    return '\n'.join(lines)


def plot(rows, out):
    """Render separate static panels; never put unlike protocols on one frontier."""
    if not any(p['compression_factor'] is not None for p in rows):
        return []
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
        from matplotlib.ticker import FuncFormatter
    except ImportError as error:
        raise ValueError('Plots require matplotlib: install compression-lab[plots]') from error
    outputs = []
    with plt.rc_context({'font.family': 'DejaVu Sans', 'font.size': 11, 'pdf.fonttype': 42}), \
            PdfPages(out / 'pareto.pdf', metadata={'Title': 'Compression benchmark Pareto plots'}) as pdf:
        for points in groups(rows).values():
            data = [p for p in points if p['compression_factor'] is not None]
            if not data:
                continue
            first = data[0]
            fig, ax = plt.subplots(figsize=(12.8, 8), dpi=160)
            fig.subplots_adjust(left=0.09, right=0.68, bottom=0.20, top=0.79)
            fig.text(0.09, 0.95, _title(first), fontsize=20, weight='bold')
            fig.text(0.09, 0.90, 'Compression factor vs. decompression throughput · higher is better', fontsize=13)
            fig.text(0.09, 0.86, first['protocol_label'], fontsize=10, color='#52606D')
            fig.text(0.09, 0.82, first['machine'], fontsize=10, color='#52606D')
            ax.set_facecolor('#FBFCFE')
            ax.grid(color='#E0E6EC', linewidth=0.7)
            ax.spines[['top', 'right']].set_visible(False)
            frontier = sorted({(p['compression_factor'], p['decode_value']) for p in data if p['pareto']})
            ax.plot([p[0] for p in frontier], [p[1] for p in frontier], color='#667888', linewidth=1.5, zorder=2)
            cmap = plt.get_cmap('tab10')
            for index, point in enumerate(data):
                provisional = '†' in point['label']
                ax.scatter(point['compression_factor'], point['decode_value'], s=130,
                           facecolor='white' if provisional else cmap(index % 10),
                           edgecolor=cmap(index % 10) if provisional else 'white', linewidth=1.6, zorder=3)
                ax.annotate(str(index + 1), (point['compression_factor'], point['decode_value']),
                            xytext=(6, 7 + 9 * sum(1 for p in data[:index] if
                            (p['compression_factor'], p['decode_value']) ==
                            (point['compression_factor'], point['decode_value']))), textcoords='offset points', fontsize=10)
                label = f"{index + 1}. {point['label']}" + (' *' if point['pareto'] else '')
                fig.text(0.715, 0.765 - index * min(0.064, 0.60 / len(data)),
                         textwrap.fill(label, 32), fontsize=10, va='top', color=cmap(index % 10))
            ax.set_ylim(0, max(p['decode_value'] for p in data) * 1.15)
            ax.margins(x=0.12)
            ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f'{value:g}×'))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f'{value:,.0f}'))
            ax.set_xlabel(f"Compression factor = original / {first['size_basis']} bytes", labelpad=12)
            ax.set_ylabel(f"Decompression {first['decode_mode']} ({first['decode_unit']})", labelpad=10)
            basis = ('Archive includes framing/dictionaries; decoder code excluded.' if first['size_basis'] == 'archive' else
                     'Charged package includes custom decoder; identified standard codec libraries excluded.')
            fig.text(0.09, 0.115, basis, fontsize=10, color='#52606D')
            fig.text(0.09, 0.077, '* Pareto frontier for these measured points and two metrics; ties retained.', fontsize=10, color='#52606D')
            footer = 'SMOKE RUN — wiring check only' if first['smoke'] else (
                '† Provisional Lab qualification; see report.md for limitations.' if any('†' in p['label'] for p in data) else
                'Source hashes, result IDs and comparison context are preserved in results.csv.')
            fig.text(0.09, 0.039, footer, fontsize=10, color='#52606D')
            slug = re.sub(r'[^a-z0-9]+', '-', _title(first).lower()).strip('-')
            name = f'{slug}-{_digest(group_key(first))[:8]}-pareto.png'
            fig.savefig(out / name, dpi=200)
            pdf.savefig(fig)
            plt.close(fig)
            outputs.append(name)
    if outputs:
        outputs.append('pareto.pdf')
    return outputs


def write_report(rows, out, *, size='auto', decode='auto', plots=False):
    """Write a fresh output directory. Existing evidence and reports are immutable."""
    prepared = prepare(rows, size=size, decode=decode)
    if not prepared:
        raise ValueError('No measured results supplied')
    out = Path(out)
    # Do not put generated checks into the shipped raw-evidence tree.
    if 'recorded' in out.resolve().parts and 'benchmarks' in out.resolve().parts:
        raise ValueError('Choose an output outside benchmarks/recorded')
    if plots:
        try:
            import matplotlib  # noqa: F401 -- fail before creating any output
        except ImportError as error:
            raise ValueError('Plots require matplotlib: install compression-lab[plots]') from error
    out.mkdir(parents=True, exist_ok=False)
    with (out / 'results.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(prepared)
    (out / 'report.md').write_text(markdown(prepared))
    images = plot(prepared, out) if plots else []
    return dict(output=str(out.resolve()), measurements=len(prepared),
                comparisons=len(groups(prepared)), plots=images)


def main(argv=None, *, default_inputs=(), default_out=None, plots=False):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inputs', nargs='*', type=Path, help='Benchmark JSON/CSV or run directory')
    parser.add_argument('--out', type=Path, default=default_out, required=default_out is None,
                        help='New report directory, outside recorded evidence')
    parser.add_argument('--size', choices=('auto', 'archive', 'package'), default='auto')
    parser.add_argument('--decode', choices=('auto', 'fresh', 'warm'), default='auto')
    parser.add_argument('--plots', action=argparse.BooleanOptionalAction, default=plots)
    args = parser.parse_args(argv)
    inputs = args.inputs or list(default_inputs)
    if not inputs:
        parser.error('Supply at least one benchmark input')
    try:
        rows = [row for path in inputs for row in load_results(path)]
        result = write_report(rows, args.out, size=args.size, decode=args.decode, plots=args.plots)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
