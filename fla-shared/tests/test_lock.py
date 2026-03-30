import os, sys, json, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import lock


class TestAcquire(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.lock_file = os.path.join(self.tmpdir, 'operation.lock')
        self.patcher = patch.object(lock, 'LOCK_FILE', self.lock_file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.lock_file):
            os.unlink(self.lock_file)
        os.rmdir(self.tmpdir)

    def test_acquire_returns_true_on_success(self):
        self.assertTrue(lock.acquire('fla-equalisation'))

    def test_acquire_creates_lock_file_with_json(self):
        lock.acquire('fla-equalisation')
        self.assertTrue(os.path.exists(self.lock_file))
        with open(self.lock_file) as f:
            data = json.loads(f.read())
        self.assertEqual(data['service'], 'fla-equalisation')
        self.assertEqual(data['pid'], os.getpid())
        self.assertIn('started', data)

    def test_acquire_returns_false_when_held_by_live_process(self):
        # Write a lock with our own (live) PID
        info = json.dumps({'service': 'fla-charge', 'started': '2025-01-01T00:00:00', 'pid': os.getpid()})
        with open(self.lock_file, 'w') as f:
            f.write(info)
        self.assertFalse(lock.acquire('fla-equalisation'))

    def test_acquire_clears_stale_lock_from_dead_pid(self):
        info = json.dumps({'service': 'fla-charge', 'started': '2025-01-01T00:00:00', 'pid': 99999999})
        with open(self.lock_file, 'w') as f:
            f.write(info)
        with patch.object(lock, '_pid_exists', return_value=False):
            self.assertTrue(lock.acquire('fla-equalisation'))

    def test_acquire_handles_file_exists_race(self):
        # Simulate a race: stale check passes, but another process creates file before us
        with patch('os.open', side_effect=FileExistsError):
            self.assertFalse(lock.acquire('fla-equalisation'))

    def test_acquire_handles_os_error(self):
        with patch('os.open', side_effect=OSError('disk full')):
            self.assertFalse(lock.acquire('fla-equalisation'))

    def test_acquire_clears_corrupt_json(self):
        with open(self.lock_file, 'w') as f:
            f.write('NOT VALID JSON{{{')
        self.assertTrue(lock.acquire('fla-equalisation'))


class TestRelease(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.lock_file = os.path.join(self.tmpdir, 'operation.lock')
        self.patcher = patch.object(lock, 'LOCK_FILE', self.lock_file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.lock_file):
            os.unlink(self.lock_file)
        os.rmdir(self.tmpdir)

    def test_release_removes_lock_file(self):
        lock.acquire('fla-equalisation')
        self.assertTrue(os.path.exists(self.lock_file))
        lock.release()
        self.assertFalse(os.path.exists(self.lock_file))

    def test_release_no_error_on_nonexistent_file(self):
        # Should not raise even if file doesn't exist
        lock.release()


class TestIsLocked(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.lock_file = os.path.join(self.tmpdir, 'operation.lock')
        self.patcher = patch.object(lock, 'LOCK_FILE', self.lock_file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.lock_file):
            os.unlink(self.lock_file)
        os.rmdir(self.tmpdir)

    def test_no_file_returns_false(self):
        self.assertFalse(lock.is_locked())

    def test_live_pid_returns_true(self):
        info = json.dumps({'service': 'fla-charge', 'started': '2025-01-01T00:00:00', 'pid': os.getpid()})
        with open(self.lock_file, 'w') as f:
            f.write(info)
        self.assertTrue(lock.is_locked())

    def test_dead_pid_returns_false(self):
        info = json.dumps({'service': 'fla-charge', 'started': '2025-01-01T00:00:00', 'pid': 99999999})
        with open(self.lock_file, 'w') as f:
            f.write(info)
        with patch.object(lock, '_pid_exists', return_value=False):
            self.assertFalse(lock.is_locked())

    def test_corrupt_json_returns_false(self):
        with open(self.lock_file, 'w') as f:
            f.write('CORRUPT{{{')
        self.assertFalse(lock.is_locked())


class TestHolder(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.lock_file = os.path.join(self.tmpdir, 'operation.lock')
        self.patcher = patch.object(lock, 'LOCK_FILE', self.lock_file)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if os.path.exists(self.lock_file):
            os.unlink(self.lock_file)
        os.rmdir(self.tmpdir)

    def test_no_file_returns_empty_dict(self):
        self.assertEqual(lock.holder(), {})

    def test_valid_lock_returns_dict_with_keys(self):
        lock.acquire('fla-equalisation')
        result = lock.holder()
        self.assertIsInstance(result, dict)
        self.assertEqual(result['service'], 'fla-equalisation')
        self.assertEqual(result['pid'], os.getpid())
        self.assertIn('started', result)

    def test_corrupt_file_returns_empty_dict(self):
        with open(self.lock_file, 'w') as f:
            f.write('NOT JSON')
        self.assertEqual(lock.holder(), {})


class TestPidExists(unittest.TestCase):

    def test_own_pid_returns_true(self):
        self.assertTrue(lock._pid_exists(os.getpid()))

    def test_nonexistent_pid_returns_false(self):
        self.assertFalse(lock._pid_exists(99999999))

    def test_none_returns_false(self):
        self.assertFalse(lock._pid_exists(None))


if __name__ == '__main__':
    unittest.main()
