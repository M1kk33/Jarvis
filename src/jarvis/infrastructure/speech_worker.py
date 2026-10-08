"""One-shot CPU-only worker. Run only in the pinned speech environment."""

import json
import sys
from importlib.metadata import version
from pathlib import Path

MAX_SAMPLES = 20 * 16_000


def decode_bounded(path: Path):
    import av  # type: ignore[import-not-found]
    import numpy as np  # type: ignore[import-not-found]

    chunks = []
    count = 0
    with av.open(str(path), mode="r", options={"protocol_whitelist": "file"}) as media:
        if len(media.streams.audio) != 1 or media.streams.video:
            raise ValueError("invalid_media")
        stream = media.streams.audio[0]
        if (
            stream.duration is not None
            and stream.time_base is not None
            and float(stream.duration * stream.time_base) > 20.1
        ):
            raise ValueError("duration_limit")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16_000)
        for frame in media.decode(stream):
            for output in resampler.resample(frame):
                samples = output.to_ndarray().reshape(-1)
                count += len(samples)
                if count > MAX_SAMPLES:
                    raise ValueError("duration_limit")
                chunks.append(samples)
        for output in resampler.resample(None):
            samples = output.to_ndarray().reshape(-1)
            count += len(samples)
            if count > MAX_SAMPLES:
                raise ValueError("duration_limit")
            chunks.append(samples)
    if count == 0:
        raise ValueError("invalid_media")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def run() -> dict[str, object]:
    import numpy as np  # type: ignore[import-not-found]

    raw = sys.stdin.buffer.readline(4097)
    if len(raw) > 4096 or not raw.endswith(b"\n"):
        raise ValueError("invalid_request")
    request = json.loads(raw)
    if (
        type(request) is not dict
        or set(request) != {"version", "audio", "model"}
        or request["version"] != 1
        or type(request["audio"]) is not str
        or type(request["model"]) is not str
    ):
        raise ValueError("invalid_request")
    if version("faster-whisper") != "1.2.1":
        raise RuntimeError("wrong_version")
    audio = decode_bounded(Path(request["audio"]))
    if float(np.sqrt(np.mean(audio * audio))) < 0.003:
        return {"version": 1, "text": ""}
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    model = WhisperModel(
        request["model"],
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        num_workers=1,
        local_files_only=True,
    )
    segments, _info = model.transcribe(
        audio,
        beam_size=1,
        condition_on_previous_text=False,
        vad_filter=False,
        task="transcribe",
    )
    text = "".join(segment.text for segment in segments).strip()
    if len(text) > 65_536:
        raise ValueError("transcript_limit")
    return {"version": 1, "text": text}


def emit_result(result: dict[str, object]) -> None:
    raw = (
        json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    if len(raw) > 65_536:
        raw = b'{"version":1,"error":"transcript_limit"}\n'
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        result = run()
    except ValueError as exc:
        result = {
            "version": 1,
            "error": str(exc)
            if str(exc)
            in {
                "invalid_media",
                "duration_limit",
                "invalid_request",
                "transcript_limit",
            }
            else "invalid_media",
        }
    except Exception:  # noqa: BLE001 - process boundary, no audio/content in diagnostics
        result = {"version": 1, "error": "worker_failure"}
    emit_result(result)
