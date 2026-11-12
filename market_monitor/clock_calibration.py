"""Calibrate public exchange timestamps against the collector's wall clock."""

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

from .market import now_ms


REFRESH_SECONDS = 300
RETRY_SECONDS = 60
CONFIRM_SECONDS = 15
MAX_AGE_SECONDS = 900
REQUEST_TIMEOUT_SECONDS = 5
MAX_RTT_MS = 500
MAX_OFFSET_MS = 60_000
WARNING_OFFSET_MS = 500
WALL_STEP_MS = 250
IN_REQUEST_WALL_STEP_MS = 50
BETWEEN_SAMPLES_DRIFT_MS = 50


@dataclass(frozen=True, slots=True)
class Sample:
    wall_at: int
    steady_at: float
    offset_ms: int
    uncertainty_ms: int
    rtt_ms: int


class ClockCalibration:
    def __init__(self, venue: str, *, wall=now_ms, steady=monotonic):
        self._wall = wall
        self._steady = steady
        # WhiteBIT v4 returns seconds. Converting to ms does not add precision.
        self._availability = {'whitebit': 'coarse', 'edgex': 'unsupported', 'extended': 'unsupported'}.get(venue)
        self._captures = ContextVar(f'{venue}_clock_transport', default=None)
        self._confirmed = self._candidate = None
        self._task = None
        self._due = 0.0
        self._error = None

    def observe_transport(self, request):
        async def fetch(*args, **kwargs):
            captures = self._captures.get()
            if captures is None:
                return await request(*args, **kwargs)
            before = (self._wall(), self._steady())
            response = await request(*args, **kwargs)
            captures.append((before, (self._wall(), self._steady())))
            return response
        return fetch

    def _current(self):
        previous = self._confirmed or self._candidate
        if previous is None:
            return None
        age = self._steady() - previous.steady_at
        wall_elapsed = self._wall() - previous.wall_at
        if age < 0 or abs(wall_elapsed - age * 1000) > WALL_STEP_MS:
            self._confirmed = self._candidate = None
            self._error = 'local_clock_step'
            self._due = 0
            return None
        expires = MAX_AGE_SECONDS if self._confirmed is not None else 2 * RETRY_SECONDS
        if age > expires:
            self._confirmed = self._candidate = None
            self._error = 'sample_expired'
            self._due = 0
        return self._confirmed

    def offset(self):
        sample = self._current()
        return sample.offset_ms if sample is not None else None

    def correct(self, stamp):
        offset = self.offset()
        return stamp - offset if stamp is not None and offset is not None else stamp

    def status(self):
        sample = self._current()
        if sample is not None:
            return dict(state='calibrated', offsetMs=sample.offset_ms, uncertaintyMs=sample.uncertainty_ms,
                        rttMs=sample.rtt_ms, sampledAt=sample.wall_at,
                        warning=abs(sample.offset_ms) > WARNING_OFFSET_MS, lastError=self._error)
        state = self._availability or ('sampling' if self._candidate is not None else
                                       'unavailable' if self._error else 'pending')
        return dict(state=state, offsetMs=None, uncertaintyMs=None, rttMs=None, sampledAt=None,
                    warning=False, lastError=self._error)

    def start(self, client):
        if client is None or self._availability is not None:
            return
        if getattr(client, 'has', {}).get('fetchTime') is not True:
            self._availability = 'unsupported'
            return
        self._current()
        if (self._task is None or self._task.done()) and self._steady() >= self._due:
            self._task = asyncio.create_task(self._probe(client.fetch_time))

    async def _probe(self, fetch_time):
        captures = []
        token = self._captures.set(captures)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                stamp = await fetch_time()
        except TimeoutError:
            self._failed('timeout')
            return
        except Exception as exc:
            self._failed(type(exc).__name__)
            return
        finally:
            self._captures.reset(token)
        if type(stamp) is not int or len(captures) != 1:
            self._failed('invalid_time_response')
            return
        ((before, started), (after, finished)), = captures
        rtt = round((finished - started) * 1000)
        elapsed_wall = after - before
        if elapsed_wall < 0 or abs(elapsed_wall - rtt) > IN_REQUEST_WALL_STEP_MS:
            self._confirmed = self._candidate = None
            self._failed('local_clock_step')
            return
        if not 0 <= rtt <= MAX_RTT_MS:
            self._failed('slow_response')
            return
        offset = stamp - (before + after) // 2
        if abs(offset) > MAX_OFFSET_MS:
            self._failed('invalid_time_response')
            return
        uncertainty = (max(rtt, elapsed_wall) + 1) // 2 + 1
        self._accept(Sample(after, finished, offset, uncertainty, rtt))

    def _failed(self, reason):
        self._error = reason
        self._due = self._steady() + RETRY_SECONDS

    def _accept(self, sample):
        self._current()
        previous = self._confirmed or self._candidate
        compatible = previous is not None and abs(previous.offset_ms - sample.offset_ms) <= (
            previous.uncertainty_ms + sample.uncertainty_ms + BETWEEN_SAMPLES_DRIFT_MS)
        if compatible:
            self._confirmed, self._candidate = sample, None
            self._error = None
            self._due = sample.steady_at + REFRESH_SECONDS
        else:
            self._error = 'offset_changed' if self._confirmed is not None else self._error
            self._confirmed, self._candidate = None, sample
            self._due = sample.steady_at + CONFIRM_SECONDS

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
