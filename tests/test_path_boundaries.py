import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import baselines, candidate, owner
from compression_lab.util import Error, save
from compression_lab.workloads import bridge, registry


class PathBoundaries(unittest.TestCase):
    def test_registration_rejects_symlink_ancestor_before_source_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / 'outside'
            outside.mkdir()
            baselines.create(outside / 'native', 'zstd', 1)
            registry.create_reference(outside / 'mutable')
            (root / 'linked').symlink_to(outside, target_is_directory=True)
            for kind, register in [('native', candidate.register), ('mutable', registry.register)]:
                with self.subTest(kind=kind), patch.object(candidate, 'build', side_effect=AssertionError('source reached builder')):
                    with self.assertRaises((Error, OSError)):
                        register(root, root / 'linked' / kind)
            for kind, card in [('native', {'adapter': 'bytes'}),
                               ('mutable', {'adapter': 'relational_bundle', 'workload': 'mutable_store'})]:
                with self.subTest(bridge=kind), patch.object(candidate, 'register') as native, patch.object(registry, 'register') as store:
                    with self.assertRaises((Error, OSError)):
                        bridge.register_candidate(root, root / 'linked' / kind, 'required', None, (), card)
                    native.assert_not_called()
                    store.assert_not_called()

    def test_owner_init_rejects_unsafe_children_before_policy_or_private_reads(self):
        for name in ('private-snapshot', 'private-attempts', 'results', 'public-rechecks'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                parent = Path(td)
                root = parent / 'owner'
                root.mkdir(mode=0o700)
                outside = parent / 'outside'
                outside.mkdir()
                (root / name).symlink_to(outside, target_is_directory=True)
                save(root / 'private.json', {})
                with patch('compression_lab.engine.Engine') as engine:
                    engine.return_value.state.return_value = {'card_digest': 'c' * 64}
                    engine.return_value.root = parent / 'public'
                    with self.assertRaises(Error):
                        owner.init(root, 997, root / 'private.json', parent / 'public')
                self.assertFalse((root / 'owner-policy.json').exists())
                self.assertEqual(list(outside.iterdir()), [])

    def test_owner_boundary_rejects_hardlink_and_late_symlink(self):
        for kind in ('hardlink', 'symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                parent = Path(td)
                root = parent / 'owner'
                root.mkdir(mode=0o700)
                save(root / 'owner-policy.json', {'owner_uid': os.geteuid(), 'agent_uid': 997})
                (parent / 'shared').write_text('public bytes')
                if kind == 'hardlink':
                    os.link(parent / 'shared', root / 'input')
                else:
                    (root / 'results').symlink_to(parent, target_is_directory=True)
                with self.assertRaises(Error):
                    owner.boundary(root, False)


if __name__ == '__main__':
    unittest.main()
