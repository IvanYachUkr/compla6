import unittest

from compression_lab.util import Error
from compression_lab.workloads import protocol


class BoundedStoreProtocolTests(unittest.TestCase):
    def test_strict_json_response_and_ids(self):
        good = b'{"version":1,"id":1,"ok":true,"result":{}}\n'
        self.assertTrue(protocol.response(good, 1)['ok'])
        for raw in (good[:-1], good.replace(b'"id":1', b'"id":true'),
                    good.replace(b'"id":1', b'"id":2'), good.replace(b'"version":1', b'"version":2'),
                    good.replace(b'{}', b'{"a":1,"a":2}'), good.replace(b'{}', b'{"x":NaN}'),
                    good.replace(b'{}', b'[]'), good + b'{}\n',
                    good.replace(b'"result":{}', b'"result":{},"unrecognized":1')):
            with self.subTest(raw=raw), self.assertRaises(Error):
                protocol.response(raw, 1)

    def test_request_limits_and_base64_canonical_spelling(self):
        for n in (True, 0, -1, 1 << 63):
            with self.subTest(n=n), self.assertRaises(Error):
                protocol.request_bytes(n, 'hello', {})
        with self.assertRaises(Error):
            protocol.request_bytes(1, 'build', {'blob': 'x' * protocol.MAX_REQUEST_BYTES})
        for text in ('???', 'YQ', 'YQ==\n', 'YR=='):
            with self.subTest(text=text), self.assertRaises(Error):
                protocol.decode_blob(text)
        self.assertEqual(protocol.decode_blob('YQ=='), b'a')
        self.assertEqual(protocol.decode_blob(''), b'')

    def test_failure_contract_bounded_and_typed(self):
        good = b'{"version":1,"id":1,"ok":false,"error":{"code":"recovery_required","recovery_required":true}}\n'
        self.assertTrue(protocol.response(good, 1)['error']['recovery_required'])
        for raw in (good.replace(b'"recovery_required":true', b'"recovery_required":1'),
                    good.replace(b'"code":"recovery_required"', b'"code":false'),
                    good.replace(b'"code":"recovery_required"', b'"code":""')):
            with self.subTest(raw=raw), self.assertRaises(Error):
                protocol.response(raw, 1)


if __name__ == '__main__':
    unittest.main()
