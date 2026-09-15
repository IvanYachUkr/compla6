"""Exercise the launcher command boundary without running a model or namespace."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_profile_is_separate_and_only_agent_workbench_is_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root/'profile'; profile.mkdir(mode=0o700)
            token = profile/'mcp-token'; token.write_text('a'*48+'\n'); token.chmod(0o600)
            workspace = root/'workspace'; (workspace/'workbench/agent').mkdir(parents=True)
            (workspace/'dataset-card.json').write_text('{}')
            prime = root/'prime'; prime.mkdir()
            for name in ('prime', 'kernel-python'):
                path = prime/name; path.write_text('#!/bin/sh\nexit 0\n'); path.chmod(0o755)
            lab = root/'lab'; (lab/'venv/bin').mkdir(parents=True)
            native = root/'native'; native.mkdir()
            # Record the actual argv emitted by the shell; real bubblewrap is
            # separately checked during the commissioned no-model launch.
            binpath = root/'bin'; binpath.mkdir()
            fake = binpath/'bwrap'
            fake.write_text('#!/usr/bin/python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o755)
            env = {**os.environ, 'PATH': str(binpath)+':'+os.environ['PATH'],
                   'COMPRESSION_LAB_PRIME_ROOT': str(prime),
                   'COMPRESSION_LAB_PRIME_EXECUTABLE': str(prime/'prime'),
                   'PRIME_AGENT_KERNEL_PYTHON': str(prime/'kernel-python'),
                   'COMPRESSION_LAB_PRIME_PROFILE': str(profile),
                   'COMPRESSION_LAB_RELEASE_ROOT': str(lab),
                   'COMPRESSION_LAB_NATIVE_PREFIX': str(native)}
            script = Path(__file__).resolve().parents[1]/'tools/prime_launch_linux.sh'
            result = subprocess.run(['/bin/bash', str(script), str(workspace), '--command', '/bin/true'],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = json.loads(result.stdout)
            binds = [args[i+1:i+3] for i, value in enumerate(args) if value == '--bind']
            readonly = [args[i+1:i+3] for i, value in enumerate(args) if value == '--ro-bind']
            self.assertIn([str(workspace/'workbench/agent'), str(workspace/'workbench/agent')], binds)
            self.assertFalse(any(row[0] == str(workspace/'workbench') for row in binds))
            self.assertTrue(any(row[0] == str(profile) for row in binds))
            self.assertIn([str(profile/'research-tmp'), '/tmp'], binds)
            self.assertIn([str(lab), str(lab)], readonly)
            self.assertIn([str(native), str(native)], readonly)
            # Hidden runs never mount the complete evaluator workspace.
            (workspace/'dataset-card.json').write_text('{"baseline_visibility":"hidden"}')
            for name in ('public', 'researcher-exports', 'results', 'exports', 'baseline-campaign'):
                (workspace/name).mkdir()
            hidden = subprocess.run(['/bin/bash', str(script), str(workspace), '--command', '/bin/true'],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(hidden.returncode, 0, hidden.stderr)
            args = json.loads(hidden.stdout)
            readonly = [args[i+1:i+3] for i, value in enumerate(args) if value == '--ro-bind']
            self.assertNotIn([str(workspace), str(workspace)], readonly)
            for name in ('dataset-card.json', 'public', 'workbench', 'researcher-exports'):
                self.assertIn([str(workspace/name), str(workspace/name)], readonly)
            for name in ('results', 'exports', 'baseline-campaign', 'state', 'candidates'):
                self.assertFalse(any(row[0] == str(workspace/name) for row in readonly))
            self.assertIn(str(workspace), [args[i+1] for i, value in enumerate(args) if value == '--tmpfs'])
            (workspace/'researcher-exports').rmdir()
            missing = subprocess.run(['/bin/bash', str(script), str(workspace), '--command', '/bin/true'],
                                     env=env, text=True, capture_output=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn('Missing or redirected public mount: researcher-exports', missing.stderr)


if __name__ == '__main__':
    unittest.main()
