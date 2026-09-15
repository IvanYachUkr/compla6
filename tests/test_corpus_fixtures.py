import json
import unittest

from compression_lab.gates import corpus_fixtures
from compression_lab.stream import StreamRecord


class CorpusFixtures(unittest.TestCase):
    def test_variants_exercise_lexical_fidelity_inside_a_json_schema(self):
        line = b'{"fields":{"a":"one","b":"two"},"number":1.00,"text":"a\\u0062"}\n'
        variants = corpus_fixtures('bytes', [StreamRecord('sample', line)])
        payloads = [row.payload for row in variants]
        self.assertIn(line, payloads)
        self.assertIn(line[:-1], payloads)
        self.assertTrue(any(b' : ' in payload or b'": ' in payload for payload in payloads))
        self.assertTrue(any(payload.find(b'"b"') < payload.find(b'"a"')
                            for payload in payloads if b'"a"' in payload and b'"b"' in payload))
        self.assertTrue(all(json.loads(payload) == json.loads(line) for payload in payloads))
        self.assertEqual(len({row.alias for row in variants}), len(variants))
        self.assertEqual(list(variants), sorted(variants, key=lambda row: row.alias))

    def test_work_is_bounded_and_non_byte_adapter_contracts_are_preserved(self):
        records = [StreamRecord(f'object-{i:03}', b'x' * 1_000_000) for i in range(8)]
        variants = corpus_fixtures('bytes', records)
        self.assertLessEqual(sum(len(row.payload) for row in variants), 256 * 1024)
        for adapter in ('hcb1', 'relational_bundle'):
            self.assertEqual(corpus_fixtures(adapter, records), ())

    def test_binary_and_deep_inputs_do_not_require_a_json_parser_success(self):
        for payload in (b'\xff\x00\n', b'[' * 1200 + b'0' + b']' * 1200 + b'\n'):
            variants = corpus_fixtures('bytes', [StreamRecord('sample', payload)])
            self.assertTrue(variants)
            self.assertTrue(all(isinstance(row.payload, bytes) for row in variants))


if __name__ == '__main__':
    unittest.main()
