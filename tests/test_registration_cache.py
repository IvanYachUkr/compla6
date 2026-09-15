import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import baselines, candidate
from compression_lab.util import Error, load, save


class RegistrationCache(unittest.TestCase):
    def test_exact_source_and_runtime_skip_build_but_edits_do_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'work').mkdir()
            baselines.create(root / 'source', 'stored')
            first = candidate.register(root / 'work', root / 'source')
            with patch.object(candidate, 'build', side_effect=AssertionError('unexpected rebuild')):
                again = candidate.register(root / 'work', root / 'source')
            self.assertEqual(first, again)
            source = root / 'source/codec.cpp'
            source.write_text(source.read_text() + '\n// New source version\n')
            with patch.object(candidate, 'build', wraps=candidate.build) as build:
                changed = candidate.register(root / 'work', root / 'source')
            build.assert_called_once()
            self.assertNotEqual(first['candidate_digest'], changed['candidate_digest'])

    def test_cache_does_not_bypass_training_split_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'work').mkdir()
            baselines.create(root / 'source', 'stored')
            manifest = load(root / 'source/candidate.json')
            manifest['training'] = {'kind': 'train-only', 'object_sha256': ['a' * 64]}
            save(root / 'source/candidate.json', manifest)
            candidate.register(root / 'work', root / 'source', train_hashes=['a' * 64])
            with self.assertRaises(Error) as caught:
                candidate.register(root / 'work', root / 'source', train_hashes=['b' * 64])
            self.assertEqual(caught.exception.code, 'training_split_violation')
