"""Reporting preserves evidence boundaries and uses strict Pareto dominance."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from compression_lab import reporting

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT / 'benchmarks/recorded'


def point(method, archive=50, speed=100, **fields):
    return dict(dataset='fixture', workload='whole', workload_id='input-a',
                machine_id='machine-a', machine='fixture CPU', protocol='whole-ram-v1',
                parameters_id='parameters-a', original_bytes=100, archive_bytes=archive,
                method=method, label=method, fresh_decode_MB_s=speed,
                primary_size='archive', source='fixture', source_sha256='a' * 64, **fields)


class ReportingTests(unittest.TestCase):
    def test_strict_dominance_retains_ties_and_is_order_independent(self):
        points = [point('tie-b'), point('slower', speed=90), point('tie-a'),
                  point('smaller', archive=40, speed=80), point('larger', archive=60)]
        report = reporting.prepare(points)
        self.assertEqual({r['method'] for r in report if r['pareto']},
                         {'tie-a', 'tie-b', 'smaller'})
        self.assertEqual(report, reporting.prepare(list(reversed(points))))
        huge = reporting.prepare([point('larger', archive=10**18 + 1), point('smaller', archive=10**18)])
        self.assertEqual([r['method'] for r in huge if r['pareto']], ['smaller'])

    def test_incompatible_machine_workload_protocol_and_parameters_are_separate(self):
        base = point('base')
        variants = []
        for field in ('machine_id', 'workload_id', 'workload', 'protocol', 'parameters_id'):
            variant = point(field, archive=60, speed=90)
            variant[field] = 'different'
            variants.append(variant)
        report = reporting.prepare([base, *variants])
        self.assertEqual(len(reporting.groups(report)), 6)
        self.assertTrue(all(r['pareto'] for r in report))

    def test_size_and_decoder_choices_are_explicit_and_missing_is_not_zero(self):
        rows = [point('a', package_bytes=80, strict_deployment_bytes=90,
                      warm_decode_MB_s=200)]
        row = reporting.prepare(rows, size='package', decode='warm')[0]
        self.assertEqual((row['size_bytes'], row['compression_factor'], row['decode_value']),
                         (80, 1.25, 200))
        self.assertEqual(row['decode_mode'], 'warm')
        self.assertEqual(row['strict_deployment_bytes'], 90)
        with self.assertRaisesRegex(ValueError, 'package'):
            reporting.prepare([point('missing')], size='package')
        with self.assertRaisesRegex(ValueError, 'warm'):
            reporting.prepare([point('missing')], decode='warm')
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reporting.prepare([point('bad', speed=value)])

    def test_existing_whole_and_native_values_are_preserved(self):
        whole = reporting.load_results(RECORDED / 'whole/trials.json')
        csv_whole = reporting.load_results(RECORDED / 'whole/comparison.csv')
        self.assertEqual(len(whole), 17)
        self.assertEqual([(r['method'], r['archive_bytes'], r['fresh_decode_MB_s']) for r in whole],
                         [(r['method'], r['archive_bytes'], r['fresh_decode_MB_s']) for r in csv_whole])
        self.assertEqual(sum('†' in r['label'] for r in whole), 2)
        yelp = next(r for r in whole if r['method'] == 'yelp-zstd-19')
        self.assertEqual(yelp['original_bytes'], 100000488)
        native = reporting.load_results(RECORDED / 'dbtext/native-results.csv')
        self.assertEqual(len(native), 7)
        astra = next(r for r in native if r['label'] == 'Astra row access')
        self.assertEqual((astra['archive_bytes'], astra['package_bytes']), (17827664, 17855968))
        self.assertEqual(len(reporting.groups(reporting.prepare(native))), 2)
        rows = reporting.load_results(RECORDED / 'dbtext/rows.json')
        selected = [r for r in rows if r.get('selectivity_percent') == 1]
        self.assertEqual(len(selected), 3)
        self.assertTrue(all(r.get('fresh_decode_MB_s') is None for r in selected))
        self.assertTrue(all(r['fresh_decode_ms'] > 0 for r in selected))

    def test_paper_units_are_never_converted_to_bytes(self):
        rows = reporting.load_results(RECORDED / 'fsst-paper/trials.json')
        summary = reporting.load_results(RECORDED / 'fsst-paper/RESULT.json')
        self.assertEqual(len(rows), 20)
        for loaded in (rows, summary):
            report = reporting.prepare(loaded)
            astra = next(r for r in report if r['method'] == 'astra' and r['selectivity_percent'] == 1)
            self.assertAlmostEqual(astra['decode_value'], 99.0802)
            self.assertEqual(astra['decode_unit'], 'Mrows/s')
            self.assertEqual(astra['decode_mode'], 'warm')
            self.assertEqual(astra['original_bytes'], 39841347)
            self.assertIsNone(astra.get('fresh_decode_MB_s'))

    def test_metadata_groups_matched_runs_but_not_missing_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = {'dataset': 'tiny', 'rows': [{'id': 'codec', 'archive_bytes': 50,
                      'encode_MB_s': 100, 'decode_MB_s': 150, 'trials': [
                          {'raw_bytes': 100, 'archive_bytes': 50, 'encode_seconds': 0.000001,
                           'decode_seconds': 100/150/1e6, 'exact': True}]}]}
            sources = []
            for name in ('a', 'b'):
                directory = root / name
                directory.mkdir()
                source = directory / 'summary.json'
                source.write_text(json.dumps(result))
                sources.append(source)
            missing = [reporting.load_results(p)[0] for p in sources]
            self.assertNotEqual(missing[0]['machine_id'], missing[1]['machine_id'])
            metadata = {'machine_id': 'same-host', 'protocol': 'whole-ram-v1',
                        'workload_id': 'same-input', 'parameters': {'cpu': 8}}
            for source in sources:
                (source.parent / 'run-metadata.json').write_text(json.dumps(metadata))
            same = [reporting.load_results(p)[0] for p in sources]
            self.assertEqual(reporting.group_key(reporting.prepare([same[0]])[0]),
                             reporting.group_key(reporting.prepare([same[1]])[0]))
            metadata['parameters']['cpu'] = 9
            (sources[1].parent / 'run-metadata.json').write_text(json.dumps(metadata))
            different = reporting.load_results(sources[1])[0]
            self.assertNotEqual(same[0]['parameters_id'], different['parameters_id'])

    def test_copied_metadata_cannot_relabel_a_different_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'summary.json'
            metadata = {'protocol': 'whole-ram-v1', 'workload_id': 'copied', 'original_bytes': 100,
                        'inputs': [{'name': 'tiny', 'bytes': 100, 'sha256': 'expected'}]}
            (root / 'run-metadata.json').write_text(json.dumps(metadata))
            result = {'dataset': 'tiny', 'input_sha256': 'different', 'rows': [
                {'id': 'codec', 'archive_bytes': 50, 'encode_MB_s': 100, 'decode_MB_s': 100}]}
            source.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                reporting.load_results(source)
            result.update(input_sha256='expected', original_bytes=200)
            source.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, 'byte count'):
                reporting.load_results(source)
            metadata['protocol'] = 'strings-v1'
            (root / 'run-metadata.json').write_text(json.dumps(metadata))
            result = json.loads((RECORDED / 'dbtext/bulk.json').read_text())
            metadata['original_bytes'] = result['rows'][0]['original_bytes']
            metadata['inputs'] = [{k: c[k] for k in ('name', 'bytes', 'sha256')}
                                  for c in result['rows'][0]['columns']]
            metadata['inputs'][0]['sha256'] = 'different'
            (root / 'run-metadata.json').write_text(json.dumps(metadata))
            source.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, 'columns'):
                reporting.load_results(source)

    def test_native_quick_depth_overrides_workspace_pass_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'summary.json'
            item = json.loads((RECORDED / 'dbtext/rows.json').read_text())['rows'][0]
            source.write_text(json.dumps({'results': [{**item, 'depth': 'quick'}, {**item, 'depth': 'full'}]}))
            metadata = {'protocol': 'strings-v1', 'machine_id': 'fixture-host',
                        'workload_id': 'fixture-input', 'parameters': {'cpu': 8, 'trials': 7, 'warmups': 1}}
            receipt = root / 'run-metadata.json'
            receipt.write_text(json.dumps(metadata))
            rows = reporting.load_results(source)
            quick = [r for r in rows if r['smoke']]
            full = [r for r in rows if not r['smoke']]
            self.assertEqual(len(quick), 6)  # Full-column and five selective measurements.
            self.assertTrue(all(r['protocol_label'] == 'Native RAM quick; 1 measured pass after 1 warmup'
                                for r in quick))
            self.assertNotEqual(quick[0]['parameters_id'], full[0]['parameters_id'])
            metadata['parameters']['trials'] = 1
            receipt.write_text(json.dumps(metadata))
            self.assertEqual(quick[0]['parameters_id'], reporting.load_results(source)[0]['parameters_id'])

    def test_canonical_csv_roundtrip_and_output_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'report'
            source = RECORDED / 'whole/trials.json'
            rows = reporting.load_results(source)
            before = source.read_bytes()
            reporting.write_report(rows, out)
            loaded = reporting.load_results(out / 'results.csv')
            self.assertEqual(reporting.prepare(rows), reporting.prepare(loaded))
            table = (out / 'report.md').read_text()
            self.assertIn('Archive MB', table)
            self.assertIn('Decompress fresh MB/s', table)
            self.assertIn('Provisional', table)
            self.assertEqual(source.read_bytes(), before)
            with self.assertRaises(FileExistsError):
                reporting.write_report(rows, out)
            with (out / 'results.csv').open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 17)


if __name__ == '__main__':
    unittest.main()
