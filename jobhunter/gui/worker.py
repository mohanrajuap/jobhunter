"""Background worker for the GUI.

Tkinter is single-threaded: touching widgets from another thread corrupts the event
loop. So searching and applying run on a worker thread and communicate only through a
queue, which the UI drains on a timer. Nothing here imports tkinter.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class Message:
    kind: str  # "progress" | "results" | "outcome" | "done" | "error"
    payload: Any = None


class QueueLogHandler(logging.Handler):
    """Mirrors log records into the GUI's activity pane."""

    def __init__(self, out: queue.Queue):
        super().__init__()
        self.queue = out

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put(Message("progress", self.format(record)))
        except Exception:
            pass


class Worker:
    """Runs one job at a time on a background thread."""

    def __init__(self) -> None:
        self.queue: queue.Queue[Message] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        # Lets manual-review mode pause the worker while the user submits a form
        # themselves. The UI calls resume() when they dismiss the prompt.
        self._resume = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def send(self, kind: str, payload: Any = None) -> None:
        self.queue.put(Message(kind, payload))

    def wait_for_user(self, timeout: float = 1800.0) -> bool:
        """Block the worker until the UI calls resume(). Called from the worker thread.

        The timeout matters: without it, a dialog the user never answers would wedge the
        worker thread for the life of the process.
        """
        self._resume.clear()
        while not self._resume.wait(timeout=1.0):
            if self._cancel.is_set():
                return False
            timeout -= 1.0
            if timeout <= 0:
                log.warning("timed out waiting for the user to submit — continuing")
                return False
        return True

    def resume(self) -> None:
        """Called from the UI thread to release a worker waiting in wait_for_user()."""
        self._resume.set()

    def start(self, fn: Callable[["Worker"], None]) -> bool:
        """Run `fn(worker)` on a background thread. Returns False if already busy."""
        if self.busy:
            return False
        self._cancel.clear()

        def runner() -> None:
            try:
                fn(self)
            except Exception as exc:
                log.exception("worker task failed")
                self.send("error", str(exc))
            finally:
                self.send("done")

        self._thread = threading.Thread(target=runner, daemon=True, name="jobhunter-worker")
        self._thread.start()
        return True

    def drain(self, limit: int = 200) -> list[Message]:
        """Pop up to `limit` messages. Called from the Tk main loop on a timer."""
        out: list[Message] = []
        for _ in range(limit):
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out
