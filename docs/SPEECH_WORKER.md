# M10 CPU speech setup

Push-to-talk is optional. It runs [faster-whisper 1.2.1](https://github.com/SYSTRAN/faster-whisper/releases/tag/v1.2.1) in a separate Python environment with `device="cpu"`, INT8, one worker, and four CTranslate2 inference threads. The core environment does not import its inference packages. Recording is capped at 20 seconds and 3 MiB; decoded audio is capped at 20 seconds again in the worker. Audio and transcripts are transient until the user edits and explicitly sends the transcript. No transcription result calls a tool.

On Windows, from the repository root:

```powershell
uv venv E:\JarvisData\runtimes\speech-base --python 3.12
uv pip install --python E:\JarvisData\runtimes\speech-base\Scripts\python.exe -r docs\speech-worker.lock.txt
E:\JarvisData\runtimes\speech-base\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-base', revision='ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66', local_dir=r'E:\JarvisData\models\speech-base', allow_patterns=['model.bin','tokenizer.json','config.json','vocabulary.txt'])"
Copy-Item profiles\speech-base.example.toml E:\JarvisData\speech.toml
```

The four profile hashes pin the [model snapshot](https://huggingface.co/Systran/faster-whisper-base/tree/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66). Adjust only the local Python and model paths if necessary. Normal transcription is offline and refuses missing or changed model files. The separate environment is intentionally outside this repository and private data backups.

Start the browser with `--speech-profile E:\JarvisData\speech.toml` before `web`. The Record control appears only when that profile is configured. Stop recording to transcribe; the result appears in the existing editable message box and is never sent automatically. Silence leaves the box unchanged. Cancel transcription stops the owned worker tree. Each request uses a new process assigned to a kill-on-close Windows Job Object; a failed or unverified tree exit blocks further transcription in that server process. Restarting the server does not replay audio.

The admission threshold is 4.5 GiB of verified available RAM: the initial 1.5 GiB speech peak bound plus the 3 GiB system reserve. A shared resource gate prevents a chat/image request or model switch from beginning during transcription. The worker has no GPU allocation path. A local WebM speech fixture peaked at about 313 MiB worker RSS during M10 qualification; auxiliary library threads made the OS thread count larger than the four configured inference threads. Language quality should still be checked with the user's actual voice and language. The faster-whisper published benchmark is not a latency promise for this PC.
