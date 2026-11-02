"""
api/jobs.py

Background jobs of the API process on bounded executors, instead of Starlette's BackgroundTasks (which ran every job on the shared request
worker threads, without limit):

  * user_jobs      rankings, descriptions, own-brand lookups and the profile-signals job. USER_JOB_WORKERS (default 4) threads run them;
                   USER_JOB_QUEUE_MAX (default 50) more may wait. A full queue raises QueueFull -> HTTP 503 with Retry-After, and the daily cap
                   is not charged.
  * operator_jobs  /seed, /enrich/signals, /score/brands: ONE worker thread, OPERATOR_JOB_QUEUE_MAX (default 10) waiting.

The queues live in memory: a restart loses whatever was waiting. api/recovery.py marks those jobs failed at startup.

Each user job holds a per-user lock (feature_locks, see usage_limits.try_acquire_lock) from the moment it is queued until it ends; the lock row
doubles as proof that the job is alive: a job whose status says queued/running while its lock is gone or stale is reported as failed
(sweep_stale).
"""

import logging
import os
import queue
import threading
import time
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import update

from api import usage_limits

logger = logging.getLogger(__name__)

STALE_MESSAGE = "This took too long and was stopped. Please try again."
BUSY_DETAIL = "Server is busy, please try again in a few seconds"


def _int(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


class QueueFull(Exception):
    """The executor's queue is full."""


class JobExecutor:
    """`workers` daemon threads taking jobs from a bounded queue. Threads start on the first submit and may be restarted after shutdown()."""

    def __init__(self, name: str, workers: int, queue_max: int):
        self.name, self.workers = name, workers
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()
        self._threads: list[tuple[threading.Thread, threading.Event]] = []
        self.running = 0

    def has_room(self) -> bool:
        return not self._queue.full()

    def pending(self) -> int:
        return self._queue.qsize()

    def submit(self, job: Callable[[], None]) -> None:
        self._ensure_threads()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            raise QueueFull(self.name) from None

    def _ensure_threads(self) -> None:
        with self._lock:
            self._threads = [(t, stop) for t, stop in self._threads if t.is_alive() and not stop.is_set()]
            while len(self._threads) < self.workers:
                stop = threading.Event()
                thread = threading.Thread(target=self._work, args=(stop,), name=f"{self.name}-{len(self._threads)}", daemon=True)
                self._threads.append((thread, stop))
                thread.start()

    def _work(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                job = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            with self._lock:
                self.running += 1
            try:
                job()
            except Exception:
                logger.exception("A background job in %s failed", self.name)
            finally:
                with self._lock:
                    self.running -= 1

    def shutdown(self, timeout: float = 5.0) -> int:
        """
        Stop taking jobs: waiting jobs are dropped (a restart loses them anyway; startup recovery marks them failed), idle workers exit within a
        fraction of a second, and a worker in the middle of a job is given `timeout` seconds in all, then left behind (daemon threads never keep
        the process alive). Returns how many waiting jobs were dropped. Never hangs.
        """
        with self._lock:
            threads, self._threads = self._threads, []
        for _, stop in threads:
            stop.set()
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        deadline = time.monotonic() + timeout
        for thread, _ in threads:
            thread.join(max(deadline - time.monotonic(), 0))
        return dropped


def user_job_workers() -> int:
    return _int("USER_JOB_WORKERS", 4, 1)


def user_job_queue_max() -> int:
    return _int("USER_JOB_QUEUE_MAX", 50, 1)


def operator_job_queue_max() -> int:
    return _int("OPERATOR_JOB_QUEUE_MAX", 10, 1)


def operator_job_stale_seconds() -> int:
    """OPERATOR_JOB_STALE_SECONDS (default 21600 = 6 h): an operator job running longer is reported as failed and no longer blocks a new one."""
    return _int("OPERATOR_JOB_STALE_SECONDS", 21600, 60)


def operator_job_keep_seconds() -> int:
    """OPERATOR_JOB_KEEP_SECONDS (default 3600): a finished operator job is forgotten after this long."""
    return _int("OPERATOR_JOB_KEEP_SECONDS", 3600, 1)


for _check in (user_job_workers, user_job_queue_max, operator_job_queue_max, operator_job_stale_seconds, operator_job_keep_seconds):
    _check()   # a bad setting stops the application at start

user_jobs = JobExecutor("user-jobs", user_job_workers(), user_job_queue_max())
operator_jobs = JobExecutor("operator-jobs", 1, operator_job_queue_max())


def busy_error() -> HTTPException:
    return HTTPException(status_code=503, detail=BUSY_DETAIL, headers={"Retry-After": "5"})


def ensure_room() -> None:
    """HTTP 503 now, before anything is locked or counted, when the user-job queue is full."""
    if not user_jobs.has_room():
        raise busy_error()


def submit_user_job(fn: Callable[..., None], *args, creator_id: int, locks: tuple[str, ...], bind) -> None:
    """Queue `fn(*args)`; the locks (already acquired by the caller) are released when it ends, however it ends. Raises QueueFull."""
    def job() -> None:
        try:
            fn(*args)
        finally:
            for feature in locks:
                usage_limits.release_lock(bind, creator_id, feature)
    user_jobs.submit(job)


# creator_profiles status column, its error column, and the lock that proves the job is alive
JOB_KINDS = (
    ("instagram_description_status", "instagram_description_error", "description:instagram"),
    ("youtube_description_status", "youtube_description_error", "description:youtube"),
    ("llm_ranking_status", "llm_ranking_error", "ranking:v2"),
    ("llm_v3_ranking_status", "llm_v3_ranking_error", "ranking:v3"),
)
_PENDING = ("queued", "running")


def sweep_stale(db, row) -> bool:
    """
    Report as failed (and save that) every description / ranking of this creator that says queued/running while its lock is gone or older than
    JOB_LOCK_STALE_SECONDS: that job is not alive (a hung thread, a lost queue). Returns whether anything changed. Commits when it did.
    """
    pending = [(status, error, feature) for status, error, feature in JOB_KINDS if (getattr(row, status) or "idle") in _PENDING]
    if not pending:
        return False
    alive = usage_limits.active_locks(db, row.id)
    changed = False
    for status, error, feature in pending:
        if feature not in alive:
            setattr(row, status, "failed")
            setattr(row, error, STALE_MESSAGE)
            changed = True
    if changed:
        db.commit()
    return changed
