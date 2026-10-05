"""Task dispatcher: claim -> run -> report.

Lifecycle per task:
  1. policy.get_handler(task["type"]) — TaskRejected => report failed, no execution
  2. progress "running" + start log-chunk streamer thread
     (the claim endpoint already moved the task queued -> claimed
     server-side; re-reporting "claimed" would be a claimed -> claimed
     self-transition, which the control-plane state machine rejects)
  3. handler(ctx, task)
     - success => progress "completed" with result
     - exception => progress "failed" with scrubbed traceback tail

Secret scrubbing: every value in task payload "secrets" plus the host token
is replaced with "***" in log lines, log chunks, and error text before
anything is written to disk or sent to the control plane (PROTOCOL §3.7 /
§6: secrets never appear in logs, events, or task results).
"""
from __future__ import annotations

import threading
import time
import traceback

from agent import policy
from agent.api import build_progress_payload

STREAM_INTERVAL_SECS = 5
MAX_ERROR_CHARS = 8000


class SecretScrubber:
    def __init__(self, secrets: list):
        self._values = sorted(
            {str(s) for s in (secrets or []) if s and len(str(s)) >= 4},
            key=len, reverse=True,
        )

    def scrub(self, text) -> str:
        if not isinstance(text, str):
            text = str(text)
        for value in self._values:
            text = text.replace(value, "***")
        return text


class _ProgressStreamer(threading.Thread):
    """Sends buffered log lines as log_chunk progress updates while running."""

    def __init__(self, api, task_id: str, current_status: str,
                 read_chunk, scrub, interval: int = STREAM_INTERVAL_SECS):
        super().__init__(daemon=True, name=f"progress-{task_id[:8]}")
        self._api = api
        self._task_id = task_id
        self._status = current_status  # validated "running" self-transition
        self._read_chunk = read_chunk
        self._scrub = scrub
        self._interval = interval
        # Named _stop_event (not _stop): threading.Thread._stop() is internal
        # cleanup invoked from _wait_for_tstate_lock during join(); an Event
        # attribute named _stop would shadow it and make join() raise
        # TypeError: 'Event' object is not callable.
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            chunk = self._read_chunk()
            if not chunk:
                continue
            try:
                self._api.progress(
                    self._task_id,
                    build_progress_payload(self._status, self._status,
                                           log_chunk=self._scrub(chunk)),
                )
            except Exception:
                pass  # streaming is best-effort; final report carries the tail

    def stop(self) -> None:
        self._stop_event.set()


class TaskDispatcher:
    def __init__(self, ctx, api):
        self.ctx = ctx
        self.api = api

    def _report(self, task_id: str, current: str, new: str, **kwargs) -> str:
        payload = build_progress_payload(current, new, **kwargs)
        self.api.progress(task_id, payload)
        return new

    def dispatch(self, task: dict) -> dict:
        """Run one claimed task to a terminal state. Returns the final result."""
        task_id = task.get("id", "unknown")
        task_type = task.get("type")
        payload = task.get("payload") or {}
        secrets = payload.get("secrets") or {}

        scrubber = SecretScrubber(
            [self.ctx.config.host_token, *secrets.values()]
        )
        # route all ctx.log() output through the scrubber for this task
        previous_scrub = self.ctx.scrub
        self.ctx.scrub = scrubber.scrub
        # NB: the claim endpoint already moved the task queued -> claimed
        # server-side (atomic UPDATE ... WHERE status='queued'), so our
        # client-side tracking starts at "claimed".
        status = "claimed"
        log_path = self.ctx.log_store.task_log_path(task_id)
        last_sent = [0]  # bytes of task log already streamed

        def read_chunk() -> str:
            try:
                size = log_path.stat().st_size
            except OSError:
                return ""
            if size <= last_sent[0]:
                return ""
            with open(log_path, "rb") as fh:
                fh.seek(last_sent[0])
                data = fh.read(min(size - last_sent[0], 200_000))
            last_sent[0] = size
            return data.decode("utf-8", errors="replace")

        try:
            try:
                handler = policy.get_handler(task_type)
                # Security rejections for disallowed payload fields happen
                # here too (TaskRejected => failed, nothing executed).
                policy.reject_disallowed_fields(task_type, task)
            except policy.TaskRejected as exc:
                self.ctx.log(task_id, f"REJECTED: {exc}")
                self._report(task_id, status, "failed",
                             error=scrubber.scrub(str(exc))[:MAX_ERROR_CHARS])
                return {"task_id": task_id, "status": "failed",
                        "rejected": True}

            self.ctx.log(task_id, f"task claimed: type={task_type}")
            # NB: the claim endpoint already moved the task queued -> claimed
            # server-side (atomic UPDATE ... WHERE status='queued'), so the
            # first progress report goes straight to "running". Re-reporting
            # "claimed" would be a claimed -> claimed self-transition, which
            # the control-plane state machine rejects with 409.
            status = self._report(task_id, status, "running")

            streamer = _ProgressStreamer(self.api, task_id, status,
                                         read_chunk, scrubber.scrub)
            streamer.start()
            try:
                result = handler(self.ctx, task)
            finally:
                streamer.stop()
                streamer.join(timeout=5)

            final_chunk = read_chunk()
            self.ctx.log(task_id, "task completed")
            status = self._report(
                task_id, status, "completed",
                log_chunk=scrubber.scrub(final_chunk) or None,
                result=result if isinstance(result, dict) else {"result": result},
            )
            return {"task_id": task_id, "status": "completed", "result": result}
        except Exception:
            tail = traceback.format_exc(limit=8)[-MAX_ERROR_CHARS:]
            try:
                self.ctx.log(task_id, f"task failed:\n{tail}")
                final_chunk = read_chunk()
                self._report(task_id, status, "failed",
                             log_chunk=scrubber.scrub(final_chunk) or None,
                             error=scrubber.scrub(tail))
            except Exception:
                pass  # never let reporting a failure raise
            return {"task_id": task_id, "status": "failed",
                    "error": scrubber.scrub(tail)}
        finally:
            self.ctx.scrub = previous_scrub
