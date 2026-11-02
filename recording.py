"""Bounded, nonblocking admission to a single SQLite writer. No stream I/O here."""
import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
import threading
import time
from uuid import uuid4


class FinalizationState(str, Enum):
    OPEN = 'OPEN'
    DRAINING = 'DRAINING'
    DECIDED_COMPLETE = 'DECIDED_COMPLETE'
    DECIDED_INCOMPLETE = 'DECIDED_INCOMPLETE'
    COMPLETE = 'COMPLETE'
    INCOMPLETE = 'INCOMPLETE'


@dataclass(frozen=True)
class LossRange:
    first: int
    last: int


@dataclass(frozen=True)
class Envelope:
    run_id: str
    ordinal: int
    kind: str
    utc_ns: int
    monotonic_ns: int
    generation: int = 0
    subscription_epoch: int = 0
    sid: int | None = None
    command_id: int | None = None
    seq: int | None = None
    ticker: str | None = None
    event_type: str | None = None
    parent_ordinal: int | None = None
    raw: str | bytes | None = None
    details: tuple[tuple[str, str], ...] = ()
    lost_before: LossRange | None = None


@dataclass(frozen=True)
class RecordingHealth:
    run_id: str
    coverage: str
    issued_ordinal: int
    committed_ordinal: int
    buffered_records: int
    buffered_bytes: int
    lost_count: int
    first_lost: int | None
    last_lost: int | None
    error_count: int
    last_error: str
    clean_shutdown: bool
    writer_alive: bool
    finalization_state: str


class Recorder:
    """Disk work is owned by storage.writer_main; locks never span disk operations.

    Budgets include in-flight, uncommitted batches. Missing ordinals are retained
    as bounded ranges, attached to the next admitted event (or the final run row).
    """
    def __init__(self, path, *, max_records=10000, max_bytes=64 * 1024 * 1024,
                 batch_size=500, batch_interval=0.1):
        if min(max_records, max_bytes, batch_size) <= 0 or batch_interval <= 0:
            raise ValueError('Recording bounds must be positive')
        self.run_id = str(uuid4())
        self.started_utc_ns = time.time_ns()
        self.started_monotonic_ns = time.monotonic_ns()
        self.max_records, self.max_bytes = max_records, max_bytes
        self.batch_size, self.batch_interval = batch_size, batch_interval
        self._condition = threading.Condition()
        self._queue = deque()
        self._issued = self._committed = self._buffered = self._bytes = 0
        self._lost_count = self._errors = 0
        self._first_lost = self._last_lost = None
        self._pending_loss = None
        self._last_error = ''
        self._incomplete = False
        self._state = FinalizationState.OPEN
        self._abandoned = self._clean = False
        self._deadline = None
        self.offer('run_started')
        from storage import writer_main
        self._thread = threading.Thread(target=writer_main, args=(self, str(path)),
                                        name='rainmaker-sqlite', daemon=True)
        self._thread.start()

    def offer(self, kind, *, raw=None, generation=0, subscription_epoch=0,
              sid=None, command_id=None, seq=None, ticker=None, event_type=None,
              parent_ordinal=None, details=(), incomplete=False):
        # Raw strings/bytes and tuple-of-string details are immutable. No JSON
        # serialization or deep copies of live books run on the producer thread.
        if raw is not None and not isinstance(raw, (str, bytes)):
            raise TypeError('Raw application messages must be str or bytes')
        details = tuple((str(k), str(v)) for k, v in details)
        size = 512 + (len(raw) * (4 if isinstance(raw, str) else 1) if raw is not None else 0)
        size += sum(128 + 4 * (len(k) + len(v)) for k, v in details)
        size += sum(4 * len(value) for value in (ticker, event_type) if value is not None)
        with self._condition:
            if self._closing:
                return None
            self._issued += 1
            ordinal = self._issued
            self._incomplete |= incomplete
            if self._buffered >= self.max_records or self._bytes + size > self.max_bytes:
                self._incomplete = True
                self._lost_count += 1
                if self._first_lost is None:
                    self._first_lost = ordinal
                self._last_lost = ordinal
                self._pending_loss = LossRange(
                    self._pending_loss.first if self._pending_loss else ordinal, ordinal)
                self._condition.notify()
                return ordinal  # Identity exists even when its record could not be admitted.
            record = Envelope(self.run_id, ordinal, kind, time.time_ns(), time.monotonic_ns(),
                              generation, subscription_epoch, sid, command_id, seq, ticker,
                              event_type, parent_ordinal, raw, details, self._pending_loss)
            self._pending_loss = None
            self._queue.append((record, size))
            self._buffered += 1
            self._bytes += size
            self._condition.notify()
            return ordinal

    @property
    def _closing(self):
        return self._state != FinalizationState.OPEN

    def _expire_locked(self):
        """Deadline and writer decisions share one short-lived memory lock.

        Success can be decided only AFTER the provisional data commit. Once won,
        its publication cannot be relabeled as a timeout; callers see FINALIZING.
        A timeout decision can never transition to a complete decision.
        """
        if self._state in (FinalizationState.DECIDED_COMPLETE,
                            FinalizationState.DECIDED_INCOMPLETE):
            return self._abandoned
        if (not self._closing or time.monotonic() < self._deadline or
                self._state in (FinalizationState.COMPLETE, FinalizationState.INCOMPLETE)):
            return False
        self._abandoned = self._incomplete = True
        self._last_error = 'ShutdownTimeout'
        self._state = FinalizationState.DECIDED_INCOMPLETE
        return True

    def _decide(self):
        with self._condition:
            self._expire_locked()
            if self._state == FinalizationState.DRAINING:
                self._state = (FinalizationState.DECIDED_INCOMPLETE if self._incomplete
                               else FinalizationState.DECIDED_COMPLETE)

    def _publication(self):
        """Snapshot of a terminal decision, not a pre-commit guess at success."""
        with self._condition:
            if self._state not in (FinalizationState.DECIDED_COMPLETE,
                                   FinalizationState.DECIDED_INCOMPLETE):
                raise RuntimeError('Finalization requires a terminal decision')
            coverage = ('COMPLETE' if self._state == FinalizationState.DECIDED_COMPLETE
                        else 'INCOMPLETE')
            return coverage, not self._abandoned and self._buffered == 0

    def _published(self):
        with self._condition:
            coverage, self._clean = self._publication()
            self._state = FinalizationState(coverage)
            self._condition.notify_all()

    def health(self):
        with self._condition:
            coverage = 'INCOMPLETE' if self._incomplete else (
                'COMPLETE' if self._state == FinalizationState.COMPLETE else
                'FINALIZING' if self._state == FinalizationState.DECIDED_COMPLETE else 'OPEN')
            return RecordingHealth(self.run_id, coverage, self._issued, self._committed,
                                   self._buffered, self._bytes, self._lost_count,
                                   self._first_lost, self._last_lost, self._errors,
                                   self._last_error, self._clean,
                                   self._thread.is_alive() if hasattr(self, '_thread') else False,
                                   self._state.value)

    def _failure(self, exc):
        with self._condition:
            self._incomplete = True
            self._errors += 1
            self._last_error = type(exc).__name__  # Never persist arbitrary exception payloads.
            if self._state == FinalizationState.DECIDED_COMPLETE:
                # A failed publication rolls back; it must never retry as COMPLETE.
                self._state = FinalizationState.DECIDED_INCOMPLETE

    def _take_batch(self):
        with self._condition:
            if not self._queue and not self._closing:
                self._condition.wait(self.batch_interval)
            batch = []
            while self._queue and len(batch) < self.batch_size:
                batch.append(self._queue.popleft())
            return batch

    def _ack(self, batch):
        with self._condition:
            if batch:
                self._committed = max(self._committed, batch[-1][0].ordinal)
                self._buffered -= len(batch)
                self._bytes -= sum(size for _, size in batch)

    async def close(self, timeout=5):
        """Bounded drain, followed by confirmed metadata publication when possible.

        FINALIZING means the data commit and success decision beat the deadline,
        but publication is not yet confirmed. It is never reported as COMPLETE.
        """
        if timeout < 0:
            raise ValueError('Shutdown timeout must be nonnegative')
        with self._condition:
            if not self._closing:
                self.offer('run_stopping')
                self._state = FinalizationState.DRAINING
                self._deadline = time.monotonic() + timeout
                self._condition.notify_all()
            remaining = max(0, self._deadline - time.monotonic())
        await asyncio.to_thread(self._thread.join, remaining)
        with self._condition:
            if self._thread.is_alive():
                self._expire_locked()
                self._condition.notify_all()
        return self.health()
