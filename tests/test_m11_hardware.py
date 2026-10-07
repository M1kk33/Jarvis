"""Opt-in pinned Piper voice check with an actual CPU worker."""

import asyncio
import io
import os
import wave
from pathlib import Path

import pytest

from jarvis.infrastructure.piper_adapter import CpuPiperSynthesizer
from jarvis.infrastructure.piper_profile import PiperProfile


@pytest.mark.skipif(
    not os.environ.get("JARVIS_PIPER_PROFILE"), reason="No pinned Piper profile"
)
def test_pinned_piper_generates_bounded_wav(tmp_path):
    path = Path(os.environ["JARVIS_PIPER_PROFILE"])
    profile = PiperProfile.from_file(path, path.parent)
    worker = CpuPiperSynthesizer(profile, tmp_path / "run")
    audio = asyncio.run(worker.synthesize("Hello from Jarvis."))
    assert len(audio) < 1_000_000
    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (
            1,
            2,
            22_050,
        )
        assert 0 < wav.getnframes() <= 20 * 22_050
    assert not list((tmp_path / "run").iterdir())
