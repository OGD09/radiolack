"""Test doubles shared by the engine and server tests."""

import asyncio

import numpy as np

FAKE_CENTRE_HZ = 145_000_000.0
FAKE_RATE_HZ = 245_760.0     # 8192 samples per frame at 30 frames per second
FAKE_TONE_BIN = 1000         # with 8192-point FFTs


class FakeSource:
    """A fast source: one tone, delivered every few milliseconds."""

    kind = "test"
    centre_hz = FAKE_CENTRE_HZ
    sample_rate_hz = FAKE_RATE_HZ

    def __init__(self, fail_after: int | None = None) -> None:
        self.reads = 0
        self._fail_after = fail_after

    async def read(self, count: int) -> np.ndarray:
        await asyncio.sleep(0.005)
        if self._fail_after is not None and self.reads >= self._fail_after:
            raise RuntimeError("the fake receiver went away")
        self.reads += 1
        offset_hz = (FAKE_TONE_BIN - 4096) * FAKE_RATE_HZ / 8192
        t = np.arange(count) / FAKE_RATE_HZ
        return (0.1 * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)
