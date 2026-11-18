"""The jobs a request starts and then outlives, and how they end.

A dashboard request can start work that takes minutes: an Apollo enrichment,
a writer pass, a discovery run, a mailbox sign-in. Each used to be a
module-level ``asyncio.Task`` that nothing tracked and nothing cancelled, so
stopping the container dropped it between two awaits with no record that it
had been running. For a job that spends credits that is the worst place to
stop: the money has gone and the row that would have recorded it has not been
written.

Registering them here buys three things:

* **shutdown says what it interrupted.** Every job still running is named in
  the log, so the next person knows what to check rather than guessing.
* **cancellation is asked for, and waited on.** A job is cancelled and given
  a moment to unwind its ``finally`` blocks, which is where these jobs write
  what they finished.
* **one place to ask what is running**, instead of each module keeping its
  own global and its own answer.

It does not make a job atomic. A job that must not stop half-way has to be
written to record its own progress; this only makes sure it is asked to stop
at a moment of its choosing rather than killed at an arbitrary one.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# How long shutdown waits for every job together, before going anyway.
GRACE_SECONDS = 8.0


class Jobs:
    """Background tasks this process started, by name."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}

    def track(self, name: str, task: asyncio.Task) -> asyncio.Task:
        """Register a task and return it, so a caller can keep using it.

        A finished task of the same name is replaced; a *running* one is kept
        alongside under a numbered name, because dropping the reference is how
        a task gets garbage-collected mid-flight.
        """
        key = name
        if key in self._tasks and not self._tasks[key].done():
            n = 2
            while f"{name}#{n}" in self._tasks and not self._tasks[f"{name}#{n}"].done():
                n += 1
            key = f"{name}#{n}"
        self._tasks[key] = task
        task.add_done_callback(lambda t, k=key: self._tasks.pop(k, None)
                               if self._tasks.get(k) is t else None)
        return task

    def start(self, name: str, coro) -> asyncio.Task:
        """create_task, tracked."""
        return self.track(name, asyncio.create_task(coro, name=name))

    def running(self) -> list[str]:
        return sorted(name for name, task in self._tasks.items() if not task.done())

    async def shutdown(self, grace: float = GRACE_SECONDS) -> list[str]:
        """Ask every job to stop, wait briefly, and report what was running."""
        live = [(name, task) for name, task in list(self._tasks.items()) if not task.done()]
        if not live:
            return []
        names = sorted(name for name, _ in live)
        logger.warning("Shutting down with %d job(s) running: %s. Asking them to stop.",
                       len(names), ", ".join(names))
        for _, task in live:
            task.cancel()
        done, pending = await asyncio.wait([task for _, task in live], timeout=grace)
        for task in done:
            exc = None if task.cancelled() else task.exception()
            if exc is not None:
                logger.warning("Job %s ended with %s: %s",
                               task.get_name(), type(exc).__name__, exc)
        if pending:
            logger.warning("%d job(s) did not stop within %.0fs: %s",
                           len(pending), grace,
                           ", ".join(sorted(t.get_name() or "?" for t in pending)))
        return names


# The process-wide registry. One per process is right: these are tasks on the
# running event loop, not state belonging to a workspace.
jobs = Jobs()
