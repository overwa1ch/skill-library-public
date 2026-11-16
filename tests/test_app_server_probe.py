"""Probe failures are bounded and always release their child process and pipes."""
import contextlib
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import app_server_probe as probe


class FakeProcess:
    def __init__(self, responses='', ignore_terminate=False, eof=False):
        self.stdin = io.StringIO()
        read_fd, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, 'r')
        os.write(self.write_fd, responses.encode())
        if eof:
            self.close_writer()
        self.ignore_terminate = ignore_terminate
        self.terminated = self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True
        self.close_writer()

    def close_writer(self):
        if self.write_fd is not None:
            os.close(self.write_fd)
            self.write_fd = None

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        if self.ignore_terminate and not self.killed:
            raise subprocess.TimeoutExpired('fixture-app-server', timeout)
        return 0


class AppServerProbeTests(unittest.TestCase):
    def run_probe(self, proc):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / 'result.json'
        with patch.object(probe.subprocess, 'Popen', return_value=proc), \
                patch.object(sys, 'argv', ['probe', '--output', str(output)]), \
                contextlib.redirect_stdout(io.StringIO()):
            probe.main()
        return json.loads(output.read_text())

    def responses(self):
        # Non-object JSON and logs are not responses to RPC calls.
        return '\n'.join(('not JSON', '[]',
                          json.dumps({'id': 1, 'result': {'version': 'fixture'}}),
                          json.dumps({'id': 2, 'result': {'data': [{'skills': [{'enabled': True}]}]}}))) + '\n'

    def assert_released(self, proc):
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.stdin.closed)
        self.assertTrue(proc.stdout.closed)

    def test_success_ignores_logs_and_closes_resources(self):
        proc = FakeProcess(self.responses())
        self.assertEqual(self.run_probe(proc)['surface'], 'standalone-app-server-not-desktop')
        self.assert_released(proc)

    def test_eof_fails_immediately_and_closes_resources(self):
        proc = FakeProcess(eof=True)
        with self.assertRaisesRegex(RuntimeError, 'closed stdout.*initialize'):
            self.run_probe(proc)
        self.assert_released(proc)

    def test_timeout_is_named_and_closes_resources(self):
        proc = FakeProcess()
        with patch.object(probe.queue.Queue, 'get', side_effect=queue.Empty):
            with self.assertRaisesRegex(TimeoutError, 'initialize'):
                self.run_probe(proc)
        self.assert_released(proc)

    def test_unresponsive_child_is_killed_and_reaped(self):
        proc = FakeProcess(self.responses(), ignore_terminate=True)
        self.run_probe(proc)
        self.assertTrue(proc.killed)
        self.assert_released(proc)

    def test_descendant_holding_stdout_does_not_block_cleanup(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        pid_file = Path(temporary.name) / 'descendant.pid'
        script = '''import json, os, pathlib, sys, time
child = os.fork()
if child == 0:
    time.sleep(30)
    os._exit(0)
pathlib.Path(sys.argv[1]).write_text(str(child))
print(json.dumps({'id': 1, 'result': {}}), flush=True)
print(json.dumps({'id': 2, 'result': {'data': [{'skills': []}]}}), flush=True)
time.sleep(30)
'''
        proc = subprocess.Popen([sys.executable, '-u', '-c', script, str(pid_file)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        started = time.monotonic()
        try:
            self.run_probe(proc)
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNotNone(proc.poll())
            self.assertTrue(proc.stdout.closed)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), 9)
                except ProcessLookupError:
                    pass

    def test_broken_pipe_during_stdin_close_still_closes_stdout_and_bad_shape_writes_nothing(self):
        class BrokenStdin:
            closed = False
            def write(self, data):
                return len(data)
            def flush(self):
                raise BrokenPipeError('child exited')
            def close(self):
                self.closed = True
                raise BrokenPipeError('buffered flush failed on close')

        proc = FakeProcess()
        proc.stdin = BrokenStdin()
        with self.assertRaises(BrokenPipeError):
            self.run_probe(proc)
        self.assertTrue(proc.stdout.closed)

        bad_responses = '\n'.join((
            json.dumps({'id': 1, 'result': {'version': 'fixture'}}),
            json.dumps({'id': 2, 'result': {'data': []}}),
        )) + '\n'
        proc2 = FakeProcess(bad_responses)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / 'bad.json'
        with patch.object(probe.subprocess, 'Popen', return_value=proc2), \
                patch.object(sys, 'argv', ['probe', '--output', str(output)]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(IndexError):
                probe.main()
        self.assertFalse(output.exists())
        self.assert_released(proc2)


if __name__ == '__main__':
    unittest.main()
