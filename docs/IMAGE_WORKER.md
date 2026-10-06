# M8 image worker

M8 uses one fixed 512×512 SD 1.5 workflow, at most one image per turn, and an isolated ComfyUI process. The model and Python environment stay outside the source checkout. Jarvis never accepts a model-supplied ComfyUI graph or custom node.

The pinned sources are [ComfyUI v0.39.0](https://github.com/Comfy-Org/ComfyUI/releases/tag/v0.39.0), commit `b0b743566f65daafc423b4fea8a2fbda94b3384a`, and [Comfy-Org's SD 1.5 FP16 conversion](https://huggingface.co/Comfy-Org/stable-diffusion-v1-5-archive/blob/9cfd069101959ca3828bf9c04a4419870832b74f/v1-5-pruned-emaonly-fp16.safetensors), SHA-256 `e9476a13728cd75d8279f6ec8bad753a66a1957ca375a1464dc63b37db6e3916`. The latter carries the CreativeML Open RAIL-M license. `image-worker.lock.txt` records the 84 installed worker packages, including PyTorch 2.8.0 CUDA 12.8. The core environment does not install Torch or ComfyUI.

On Windows, after installing `uv` and choosing a private data directory (shown here as `E:\JarvisData`):

```powershell
git clone --depth 1 --branch v0.39.0 https://github.com/Comfy-Org/ComfyUI.git E:\JarvisData\runtimes\comfyui-v0.39.0
git -C E:\JarvisData\runtimes\comfyui-v0.39.0 rev-parse HEAD
uv venv --python 3.12 E:\JarvisData\runtimes\comfyui-v0.39.0\.venv
uv pip install --python E:\JarvisData\runtimes\comfyui-v0.39.0\.venv\Scripts\python.exe torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python E:\JarvisData\runtimes\comfyui-v0.39.0\.venv\Scripts\python.exe -r docs\image-worker.lock.txt
curl.exe -L --fail --output E:\JarvisData\runtimes\comfyui-v0.39.0\models\checkpoints\v1-5-pruned-emaonly-fp16.safetensors "https://huggingface.co/Comfy-Org/stable-diffusion-v1-5-archive/resolve/9cfd069101959ca3828bf9c04a4419870832b74f/v1-5-pruned-emaonly-fp16.safetensors"
Get-FileHash E:\JarvisData\runtimes\comfyui-v0.39.0\models\checkpoints\v1-5-pruned-emaonly-fp16.safetensors -Algorithm SHA256
```

Copy [the example image profile](../profiles/sd15-512.example.toml) to `E:\JarvisData\image.toml`, set `device_uuid` and `device_index` to the same GPU used by the text profile, and keep the pinned hashes. The example's 5.5 GiB VRAM and 6 GiB RAM cold bounds are conservative admission limits, not measured image peaks. Keep `calibrated = false` until a dedicated cold/warm peak calibration verifies those bounds on the target PC. The M8 repeated-cycle check and its sampled free-memory values are recorded in [resource profiles](RESOURCE_PROFILES.md). Start a model-backed session or browser with both `--profile E:\JarvisData\model.toml` and `--image-profile E:\JarvisData\image.toml`.

The worker binds to a random loopback port with a per-launch bearer secret. Jarvis installs authentication inside ComfyUI's HTTP and WebSocket application before routes are served; browser-origin requests are rejected even with a bearer header. Custom nodes, partner nodes, external listening, and browser auto-launch are disabled. The image process owns a kill-on-close Windows Job Object and shares the text model's single device lock. Text inference releases its operation handle before image admission; ComfyUI exits and its complete child tree is verified before text is loaded again.

Generated PNGs are validated, flushed to `staging`, checked by size and hash, renamed into `artifacts`, then recorded with the tool result and image step in one SQLite transaction. A crash between rename and commit can leave an unreferenced file for inspection; the tool outcome is not replayed. On restart, referenced images are checked again and missing or corrupt ones are marked unavailable. Backups include only referenced, verified artifacts and restore independently of the source data directory. The browser serves available images through authenticated local preview and download routes; the text model receives only the artifact ID and dimensions, not image bytes.
