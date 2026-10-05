"""SSE (Server-Sent Events) stream parser per wire protocol §3.8.

The wire carries ``data: <json>\\n\\n`` frames; ``:`` comment lines are
heartbeats and are skipped. The parser yields one decoded event dict per
``data:`` frame, joining multi-line data payloads with newlines.
"""

import json


class SseParser:
    def __init__(self):
        self._buf = ""

    def feed(self, chunk: str):
        """Feed decoded text. Returns a list of parsed event dicts."""
        self._buf += chunk
        events = []
        while "\n\n" in self._buf:
            raw, self._buf = self._buf.split("\n\n", 1)
            data_lines = []
            for line in raw.split("\n"):
                line = line.rstrip("\r")
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line.startswith(":"):
                    continue  # comment / heartbeat
                # "event:", "id:", "retry:" fields are ignored in protocol v1
            if data_lines:
                try:
                    events.append(json.loads("\n".join(data_lines)))
                except json.JSONDecodeError:
                    continue  # ignore malformed frames; keep the stream open
        return events


def iter_sse_events(response, decode_unicode=True):
    """Yield parsed event dicts from a streaming ``requests`` response."""
    parser = SseParser()
    for line in response.iter_lines(decode_unicode=decode_unicode):
        if line is None:
            continue
        yield from parser.feed(line + "\n")
