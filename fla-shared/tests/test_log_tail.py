#!/usr/bin/env python3
"""Tests for the bounded log-tail helper (issue #22).

The dashboard's log card must never stall the single HTTP thread: reads
are bounded both in line count and in bytes, whatever the log size.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from log_tail import tail


class TestLogTail(unittest.TestCase):
    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".log")
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_returns_exactly_the_last_n_lines(self):
        path = self._write("".join("line %d\n" % i for i in range(100)))
        self.assertEqual(tail(path, lines=3), ["line 97", "line 98", "line 99"])

    def test_short_file_returns_all_lines(self):
        path = self._write("only\ntwo\n")
        self.assertEqual(tail(path, lines=50), ["only", "two"])

    def test_missing_file_returns_empty_not_error(self):
        self.assertEqual(tail("/nonexistent/nope.log", lines=10), [])

    def test_byte_cap_bounds_the_read_regardless_of_requested_lines(self):
        # A single huge line larger than the cap must come back truncated
        # to at most the cap — never read whole.
        path = self._write("x" * 100_000 + "\nend\n")
        result = tail(path, lines=10, max_bytes=1024)
        joined = "\n".join(result)
        self.assertLessEqual(len(joined.encode()), 1024)
        self.assertIn("end", result[-1])

    def test_empty_file_returns_empty_list(self):
        self.assertEqual(tail(self._write(""), lines=10), [])


if __name__ == "__main__":
    unittest.main()
