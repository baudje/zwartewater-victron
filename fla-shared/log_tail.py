"""Bounded log-file tail for the dashboard's log card (issue #22).

Reads at most `max_bytes` from the END of the file, whatever line count is
requested — a multi-megabyte log can never stall the single HTTP thread
that also serves the Abort button.
"""

import os

DEFAULT_MAX_BYTES = 64 * 1024


def tail(path, lines=50, max_bytes=DEFAULT_MAX_BYTES):
    """Return up to the last `lines` lines of `path` (oldest first).

    Reads only the final `max_bytes` of the file. A missing or unreadable
    file yields [] — the dashboard shows an empty card, not an error.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read(max_bytes)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    result = text.splitlines()[-lines:] if lines > 0 else []
    # If the byte cap cut mid-line, the first surviving line may be a
    # fragment — acceptable for a log tail; truncate oversize output so the
    # response itself stays within the cap.
    while result and len("\n".join(result).encode()) > max_bytes:
        if len(result) == 1:
            result[0] = result[0][-max_bytes:]
            break
        result = result[1:]
    return result
