import json
import importlib.machinery
import importlib.util
from unittest import mock
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

CLI = str(Path(__file__).resolve().parents[1] / 'test-queue')
loader = importlib.machinery.SourceFileLoader('queue_cli', CLI)
spec = importlib.util.spec_from_loader(loader.name, loader)
queue_cli = importlib.util.module_from_spec(spec)
loader.exec_module(queue_cli)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.env = dict(os.environ, TEST_QUEUE_STATE_DIR=str(self.root / 'state'), TEST_QUEUE_MEMORY_WAIT_SECONDS='0')
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        self.temp.cleanup()

    def start(self, code, cwd=None):
        p = subprocess.Popen([CLI, 'run', '--', sys.executable, '-c', code], env=self.env,
                             cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(p)
        return p

    def jobs(self):
        return json.loads(subprocess.check_output([CLI, 'status'], env=self.env))

    def until(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.03)
        self.fail('condition timed out')

    def test_fifo_across_directories_and_exit_codes(self):
        marker = self.root / 'marker'
        first = self.start(f'import time; time.sleep(.8); open({str(marker)!r},"w").write("first")')
        self.until(lambda: len(self.jobs()) == 1)
        other = self.root / 'other'
        other.mkdir()
        second = self.start(f'import os; assert open({str(marker)!r}).read()=="first"; assert os.getcwd()=={str(other)!r}; raise SystemExit(7)', other)
        self.until(lambda: len(self.jobs()) == 2)
        self.assertEqual([j['state'] for j in self.jobs()], ['running', 'waiting'])
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertEqual(second.wait(timeout=5), 7)
        self.assertEqual(self.jobs(), [])

    def test_terminated_waiter_does_not_block(self):
        first = self.start('import time; time.sleep(.8)')
        self.until(lambda: len(self.jobs()) == 1)
        second = self.start('raise SystemExit(42)')
        self.until(lambda: len(self.jobs()) == 2)
        second.terminate()
        self.assertEqual(second.wait(timeout=5), 143)
        third = self.start('pass')
        self.assertEqual(first.wait(timeout=5), 0)
        self.assertEqual(third.wait(timeout=5), 0)

    def test_killed_waiter_is_cleaned_up(self):
        first = self.start('import time; time.sleep(.8)')
        self.until(lambda: len(self.jobs()) == 1)
        second = self.start('pass')
        self.until(lambda: len(self.jobs()) == 2)
        second.kill()
        second.wait(timeout=5)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(first.wait(timeout=5), 0)

    def test_signal_forwarding_releases_slot(self):
        first = self.start('import time; time.sleep(30)')
        self.until(lambda: any(j['state'] == 'running' for j in self.jobs()))
        first.terminate()
        self.assertEqual(first.wait(timeout=5), 143)
        self.assertEqual(self.jobs(), [])

    def test_killed_runner_keeps_slot_until_child_exits(self):
        marker = self.root / 'finished'
        first = self.start(f'import time; time.sleep(1); open({str(marker)!r},"w").close()')
        self.until(lambda: any(j['state'] == 'running' for j in self.jobs()))
        time.sleep(.15)
        first.kill()
        first.wait(timeout=5)
        second = self.start(f'assert __import__("os").path.exists({str(marker)!r})')
        self.assertEqual(second.wait(timeout=5), 0)

    def test_missing_command_and_environment(self):
        p = subprocess.run([CLI, 'run', '--', '/does-not-exist'], env=self.env, capture_output=True)
        self.assertEqual(p.returncode, 127)
        child = self.start('import os; assert os.environ["TEST_QUEUE_STATE_DIR"]')
        self.assertEqual(child.wait(timeout=5), 0)

    def test_priority_and_metrics(self):
        child = self.start('import os; assert os.getpriority(os.PRIO_PROCESS, 0) >= 10')
        self.assertEqual(child.wait(timeout=5), 0)
        record = json.loads((self.root / 'state/history.jsonl').read_text().splitlines()[-1])
        self.assertEqual(record['state'], 'finished')
        self.assertGreater(record['peak_process_rss_bytes'], 0)
        self.assertGreaterEqual(record['cpu_user_seconds'], 0)
        self.assertGreater(record['run_seconds'], 0)
        self.assertGreaterEqual(record['wait_seconds'], 0)

    def test_memory_pressure_delays_head_job(self):
        with mock.patch.dict(os.environ, dict(self.env, TEST_QUEUE_MEMORY_WAIT_SECONDS='30')), \
             mock.patch.object(queue_cli, 'memory_pressure', side_effect=[2, 1]), \
             mock.patch.object(queue_cli.time, 'sleep'), \
             mock.patch.object(queue_cli.time, 'monotonic', side_effect=[1, 1, 4, 4]):
            self.assertEqual(queue_cli.run([sys.executable, '-c', 'pass']), 0)
        record = json.loads((self.root / 'state/history.jsonl').read_text().splitlines()[-1])
        self.assertEqual(record['memory_pressure'], 2)
        self.assertEqual(record['state'], 'finished')

    def test_persistent_memory_pressure_cannot_stall_queue(self):
        import itertools
        for pressure in (2, 4):
            with self.subTest(pressure=pressure), \
                 mock.patch.dict(os.environ, dict(self.env, TEST_QUEUE_MEMORY_WAIT_SECONDS='30')), \
                 mock.patch.object(queue_cli, 'memory_pressure', return_value=pressure), \
                 mock.patch.object(queue_cli.time, 'sleep'), \
                 mock.patch.object(queue_cli.time, 'monotonic', side_effect=itertools.count(0, 10)):
                self.assertEqual(queue_cli.run([sys.executable, '-c', 'pass']), 0)
            record = json.loads((self.root / 'state/history.jsonl').read_text().splitlines()[-1])
            self.assertTrue(record['memory_wait_limit_reached'])
            self.assertEqual(record['exit_code'], 0)

    def test_memory_probe_unavailable(self):
        with mock.patch.object(queue_cli.sys, 'platform', 'darwin'), \
             mock.patch.object(queue_cli.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertIsNone(queue_cli.memory_pressure())

    def test_preset_uses_two_workers(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        npm = bindir / 'npm'
        npm.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        npm.chmod(0o755)
        env = dict(self.env, PATH=str(bindir) + ':' + os.environ['PATH'])
        p = subprocess.run([CLI, 'test', '--', 'example.test.ts'], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.splitlines(), ['test', '--', 'example.test.ts', '--maxWorkers=2'])


if __name__ == '__main__':
    unittest.main()
