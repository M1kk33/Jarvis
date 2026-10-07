# M11 local voice setup

Spoken replies are optional. The browser's **Play voice** control reads an already completed assistant reply; **Stop voice**, changing conversations, or sending a new message cancels playback. Voice failure leaves the stored text available. Voice never approves tools or submits user input. The worker uses [Piper v1.8.0](https://github.com/OHF-Voice/piper1-gpl/releases/tag/v1.8.0) on CPU in a separate environment. The engine is GPL-3.0-or-later. The [voice repository](https://huggingface.co/rhasspy/piper-voices) is MIT-licensed; the single pinned [en_US-norman-medium voice](https://huggingface.co/rhasspy/piper-voices/tree/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/norman/medium) is trained from LibriVox public-domain recordings. Its model card and hashes are recorded in the example profile.

On Windows, from the repository root:

```powershell
uv venv E:\JarvisData\runtimes\piper-norman --python 3.12
uv pip install --python E:\JarvisData\runtimes\piper-norman\Scripts\python.exe -r docs\piper-worker.lock.txt
E:\JarvisData\runtimes\piper-norman\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='rhasspy/piper-voices', revision='c10ece1aade47bb51c153c893d14e5bf8e5b7117', local_dir=r'E:\JarvisData\models\piper-norman', allow_patterns=['en/en_US/norman/medium/en_US-norman-medium.onnx','en/en_US/norman/medium/en_US-norman-medium.onnx.json','en/en_US/norman/medium/MODEL_CARD'])"
Copy-Item profiles\piper-norman.example.toml E:\JarvisData\piper.toml
```

Adjust only the two local paths in the copied profile if necessary. Jarvis verifies the model, voice configuration, and model card hashes before use. Start the browser with `--piper-profile E:\JarvisData\piper.toml` before `web`. This profile is deliberately browser-only because M11 playback requires an explicit Play control and local browser audio output. The separate Piper environment is outside the core dependency lock and private backups; retain a copy if restoring on a new PC. Normal playback is offline.

Each request synthesizes one text chunk of at most 256 characters in a disposable, owned Windows worker. The browser receives and plays one bounded WAV chunk at a time. A reply is limited to 4,096 characters and 24 chunks; an idle playback session expires after two minutes. The worker is released after each chunk, including on cancellation or failure. Admission needs at least 3.8 GiB of verified available RAM (0.8 GiB peak bound plus the 3 GiB reserve). M10 transcription and M11 synthesis share the CPU worker gate; active chat/image resource transitions also exclude them. The CPU execution provider is selected explicitly with four ONNX inference threads. Cleanup that cannot be verified blocks further synthesis in that server process.
