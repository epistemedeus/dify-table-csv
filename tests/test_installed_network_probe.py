"""Focused TCP probe controls. Only loopback sockets are opened."""
import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "install" / "probe-tcp.py"

class TestTcpProbe(unittest.TestCase):
    def run_probe(self, host, port):
        return subprocess.run([sys.executable, str(HELPER), str(host), str(port)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=5, check=False)

    def test_open_port_succeeds_with_closed_stdin(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            result = self.run_probe("127.0.0.1", listener.getsockname()[1])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tcp_connected=1", result.stdout)

    def test_closed_port_is_not_a_positive(self):
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            result = self.run_probe("127.0.0.1", bound.getsockname()[1])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("tcp_connected=0", result.stdout)
        self.assertNotIn("tcp_connected=1", result.stdout)

    def test_same_destination_recovers_after_listener_starts(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            self.assertEqual(self.run_probe("127.0.0.1", port).returncode, 1)
            listener.listen()
            self.assertEqual(self.run_probe("127.0.0.1", port).returncode, 0)

    def test_timeout_is_not_a_positive(self):
        spec = importlib.util.spec_from_file_location("tcp_probe", HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        with patch.object(helper.socket, "create_connection", side_effect=socket.timeout):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = helper.probe("192.0.2.1", 5432)
        self.assertEqual(code, 1)
        self.assertIn("tcp_connected=0", output.getvalue())

    def test_nonliteral_or_invalid_destination_is_refused(self):
        for host, port in [("localhost", 5432), ("127.0.0.1", 0)]:
            with self.subTest(host=host, port=port):
                result = self.run_probe(host, port)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("tcp_connected=1", result.stdout)

if __name__ == "__main__":
    unittest.main()
