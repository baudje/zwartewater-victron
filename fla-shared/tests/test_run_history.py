#!/usr/bin/env python3
"""Tests for the persistent run-history store (issue #25).

One JSON line per finished run; reads tolerate corrupt/partial lines so a
power-cut mid-append can never blind the dashboard to older runs.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from run_history import append_run, read_last


class TestRunHistory(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        f.close()
        os.unlink(f.name)  # store must create it on first append
        self.path = f.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def test_append_and_read_roundtrip(self):
        append_run(self.path, {"start": "t0", "end": "t1", "outcome": "success",
                               "peak_trojan_voltage": 31.4,
                               "minutes_at_target": 16, "reconnect_delta": 0.97})
        runs = read_last(self.path, 5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["outcome"], "success")
        self.assertEqual(runs[0]["reconnect_delta"], 0.97)

    def test_read_last_returns_newest_first(self):
        for i in range(5):
            append_run(self.path, {"outcome": "success", "n": i})
        runs = read_last(self.path, 3)
        self.assertEqual([r["n"] for r in runs], [4, 3, 2])

    def test_corrupt_lines_are_skipped_not_fatal(self):
        append_run(self.path, {"outcome": "success", "n": 0})
        with open(self.path, "a") as f:
            f.write('{"outcome": "aborted", "n": 1truncated-by-power-cut\n')
            f.write("not json at all\n")
        append_run(self.path, {"outcome": "failed", "n": 2})
        runs = read_last(self.path, 10)
        self.assertEqual([r["n"] for r in runs], [2, 0])

    def test_missing_file_reads_empty(self):
        self.assertEqual(read_last(self.path, 10), [])

    def test_append_never_raises(self):
        # A run must complete its teardown even if the data partition is
        # unwritable — history is best-effort.
        append_run("/nonexistent-dir/x/y.jsonl", {"outcome": "success"})


if __name__ == "__main__":
    unittest.main()
