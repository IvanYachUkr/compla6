import contextlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from compression_lab import connections
from compression_lab.util import lock


class ConnectionSerialization(unittest.TestCase):
    def test_connection_preserves_configuration_edited_while_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            config = project / '.codex/config.toml'
            config.parent.mkdir()
            config.write_text('model = "first"\n')
            waiting = threading.Event()
            failures = []

            @contextlib.contextmanager
            def observed_lock(path, *args, **kwargs):
                waiting.set()
                with lock(path, *args, **kwargs):
                    yield

            def connect():
                try:
                    connections.connect('codex', project, True)
                except BaseException as error:
                    failures.append(error)

            with patch.object(connections, 'lock', observed_lock):
                with lock(project / '.compression-lab-connect.lock'):
                    worker = threading.Thread(target=connect)
                    worker.start()
                    acquired = waiting.wait(3)
                    config.write_text('model = "updated"\n# concurrent edit\n')
                worker.join(3)
            self.assertTrue(acquired)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertIn('model = "updated"\n# concurrent edit\n', config.read_text())
            self.assertIn('[mcp_servers.compression-lab]', config.read_text())
