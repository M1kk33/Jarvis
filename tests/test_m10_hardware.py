"""Opt-in local CPU speech fixture checks; no microphone or network required."""

import asyncio
import io
import os
import wave
from pathlib import Path

import pytest

from jarvis.domain.speech import SpeechInvalid
from jarvis.infrastructure.speech import CpuSpeechTranscriber
from jarvis.infrastructure.speech_profile import SpeechProfile


def _silence(seconds: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 16_000 * seconds)
    return output.getvalue()


@pytest.mark.skipif(
    not os.environ.get("JARVIS_SPEECH_PROFILE"), reason="No pinned speech profile"
)
def test_pinned_worker_silence_invalid_and_duration(tmp_path):
    path = Path(os.environ["JARVIS_SPEECH_PROFILE"])
    profile = SpeechProfile.from_file(path, path.parent)
    worker = CpuSpeechTranscriber(profile, tmp_path / "run")

    async def scenario():
        assert await worker.transcribe(_silence(1), "audio/wav") == ""
        with pytest.raises(SpeechInvalid, match="invalid_media"):
            await worker.transcribe(b"not audio", "audio/wav")
        with pytest.raises(SpeechInvalid, match="duration_limit"):
            await worker.transcribe(_silence(21), "audio/wav")

    asyncio.run(scenario())
