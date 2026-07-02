"""Persistent run-history store (issue #25).

One JSON line per finished FLA run (success, operator abort, or error) on
the data partition. Appends are best-effort — a failed write must never
break a run's teardown — and reads tolerate corrupt/partial lines so a
power-cut mid-append can't blind the dashboard to older runs.
"""

import json
import logging

log = logging.getLogger(__name__)

# Backstop against unbounded growth: reads only consider the final chunk.
READ_MAX_BYTES = 256 * 1024


def append_run(path, record):
    """Append one run record as a JSON line. Never raises."""
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        log.warning("Could not append run history to %s: %s", path, e)


def read_last(path, n):
    """Return the last `n` records, newest first, skipping corrupt lines."""
    import os
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - READ_MAX_BYTES))
            lines = f.read(READ_MAX_BYTES).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in reversed(lines):
        if len(records) >= n:
            break
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
