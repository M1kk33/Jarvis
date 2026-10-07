"""One bounded Piper CPU synthesis in a disposable child process."""

import json
import os
import sys
import wave
from importlib.metadata import version
from pathlib import Path

MAX_AUDIO_BYTES = 20 * 22_050 * 2


def run() -> dict[str, object]:
    import onnxruntime as ort  # type: ignore[import-not-found]
    from piper import PiperVoice  # type: ignore[import-not-found]
    from piper.config import PiperConfig  # type: ignore[import-not-found]
    from piper.voice import ESPEAK_DATA_DIR  # type: ignore[import-not-found]

    raw = sys.stdin.buffer.readline(4097)
    if len(raw) > 4096 or not raw.endswith(b"\n"):
        raise ValueError("invalid_request")
    request = json.loads(raw)
    if (
        type(request) is not dict
        or set(request) != {"version", "voice", "output", "text"}
        or request["version"] != 1
        or type(request["voice"]) is not str
        or type(request["output"]) is not str
        or type(request["text"]) is not str
        or not 1 <= len(request["text"]) <= 256
    ):
        raise ValueError("invalid_request")
    if version("piper-tts") != "1.8.0":
        raise RuntimeError("wrong_version")
    voice_path = Path(request["voice"])
    output_path = Path(request["output"])
    if (
        output_path.parent.resolve() != Path.cwd().resolve()
        or output_path.name != "audio.wav"
    ):
        raise ValueError("invalid_request")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session = ort.InferenceSession(
        str(voice_path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    config = PiperConfig.from_dict(
        json.loads(Path(str(voice_path) + ".json").read_text(encoding="utf-8"))
    )
    voice = PiperVoice(session=session, config=config, espeak_data_dir=ESPEAK_DATA_DIR)
    count = 0
    with output_path.open("xb") as output:
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(22_050)
            for chunk in voice.synthesize(request["text"]):
                if (
                    chunk.sample_rate != 22_050
                    or chunk.sample_width != 2
                    or chunk.sample_channels != 1
                ):
                    raise RuntimeError("unexpected_audio_format")
                audio = chunk.audio_int16_bytes
                count += len(audio)
                if count > MAX_AUDIO_BYTES:
                    raise ValueError("audio_limit")
                wav.writeframes(audio)
        output.flush()
        os.fsync(output.fileno())
    if count == 0:
        raise RuntimeError("empty_audio")
    return {"version": 1, "bytes": count, "sample_rate": 22_050}


if __name__ == "__main__":
    try:
        result = run()
    except ValueError as exc:
        result = {
            "version": 1,
            "error": str(exc)
            if str(exc) in {"invalid_request", "audio_limit"}
            else "invalid_request",
        }
    except Exception:  # noqa: BLE001 - no text or audio in error output
        result = {"version": 1, "error": "worker_failure"}
    print(json.dumps(result, separators=(",", ":")), flush=True)
