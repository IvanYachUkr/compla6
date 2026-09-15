import base64
import copy
import hashlib
import struct
import unittest

from compression_lab.workloads import relational as rel
from compression_lab.workloads.fixtures import movie_bundle, movie_schema
from compression_lab.util import Error


class RelationalBundleTests(unittest.TestCase):
    def test_exact_frame_roundtrip_and_non_id_order(self):
        raw = movie_bundle()
        bundle = rel.unpack(raw)
        self.assertEqual(rel.pack(bundle.schema_bytes, bundle.tables), raw)
        self.assertEqual([r.values[0] for r in bundle.rows['titles']], [7, 2, 9])
        self.assertEqual([n for n, _ in bundle.tables], ['titles', 'entities', 'episodes', 'relationships'])
        self.assertIn(b'\r\n', bundle.tables[0][1])
        self.assertNotEqual(bundle.schema_bytes, rel.json_bytes(bundle.schema))

    def test_lexical_null_empty_quotes_crlf_and_boundaries(self):
        raw = b'007,"a""b,\r\nc",\\N,"\\N",,"",-9223372036854775808,+0000,18446744073709551615\r\n'
        records = rel.csv_records(raw)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.raw, raw)
        self.assertEqual(r.terminator, b'\r\n')
        self.assertEqual(r.cells[:6], (b'007', b'a"b,\r\nc', None, b'\\N', b'', b''))
        self.assertEqual(rel.csv_records(b'a\rb\nc\r\nd')[-1].terminator, b'')
        self.assertEqual(b''.join(x.raw for x in rel.csv_records(b'a\rb\nc\r\nd')), b'a\rb\nc\r\nd')

    def test_empty_tables_and_schema_spelling(self):
        schema = {'version': 1, 'dialect': 'csv-lexical-v1', 'tables': [
            {'name': 'table\nname', 'columns': [{'name': 'x', 'type': 'text', 'nullable': True}]}]}
        spelling = ('  ' + __import__('json').dumps(schema, indent=1) + '\n').encode()
        raw = rel.pack(spelling, [('table\nname', b'')])
        self.assertEqual(rel.unpack(raw).schema_bytes, spelling)
        self.assertEqual(rel.unpack(raw).rows['table\nname'], ())

    def test_malformed_frame_and_duplicate_names_fail(self):
        raw = movie_bundle()
        for bad in (b'', raw[:-1], raw + b'x', b'BAD!' + raw[4:], raw[:20] + b'x' + raw[21:]):
            with self.subTest(kind=len(bad)), self.assertRaises(Error):
                rel.unpack(bad)
        b = rel.unpack(raw)
        with self.assertRaises(Error):
            rel.pack(b.schema_bytes, [*b.tables, b.tables[0]])

    def test_null_is_not_empty_and_signed_spelling_preserved(self):
        b = rel.unpack(movie_bundle())
        self.assertEqual(b.rows['titles'][1].values[2], '')
        self.assertIsNone(b.rows['titles'][2].values[2])
        self.assertIn(b'+0000', b.rows['titles'][0].raw)
        self.assertEqual(b.rows['titles'][1].values[3], -(1 << 63))
        self.assertEqual(b.rows['titles'][2].values[3], (1 << 63) - 1)

    def test_integer_overflow_derived_field_and_fk_fail(self):
        b = rel.unpack(movie_bundle())
        for old, new in ((b'-9223372036854775808', b'-9223372036854775809'),
                         (b'tt0000007', b'tt0000008'),
                         (b'001,7,3', b'001,999,3')):
            tables = [(n, data.replace(old, new)) for n, data in b.tables]
            with self.subTest(new=new), self.assertRaises(Error):
                rel.pack(b.schema_bytes, tables)

    def test_bad_csv_is_not_silently_normalized(self):
        for raw in (b'"unterminated', b'"a"x,b\n', b'a"b,c\n'):
            with self.subTest(raw=raw), self.assertRaises(Error):
                rel.csv_records(raw)

    def test_uint64_max_and_noncanonical_lexical_zero(self):
        s = {'version': 1, 'dialect': 'csv-lexical-v1', 'tables': [
            {'name': 'n', 'columns': [{'name': 'id', 'type': 'uint64', 'nullable': False}]}]}
        b = rel.unpack(rel.pack(rel.json_bytes(s), [('n', b'18446744073709551615\n+0000\n-0')]))
        self.assertEqual([r.values[0] for r in b.rows['n']], [(1 << 64) - 1, 0, 0])


if __name__ == '__main__':
    unittest.main()
