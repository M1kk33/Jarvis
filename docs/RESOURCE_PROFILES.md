# Resource qualification

The admission profile values are conservative limits, not promises about a particular desktop workload. Jarvis retains 1 GiB free VRAM and 3 GiB available system RAM when admitting a heavy worker. Missing telemetry is never substituted with an estimate.

On this PC, an RTX 3060 Ti (8,192 MiB VRAM) with NVIDIA driver 576.80 ran the pinned Qwen3-4B-Instruct-2507 Q4_K_M text model through llama.cpp b11429 and the pinned ComfyUI v0.39.0 SD 1.5 FP16 512×512 workflow. See [the text model profile](../profiles/qwen3-4b-instruct-2507.example.toml), [the image profile](../profiles/sd15-512.example.toml), and [the image worker pinning](IMAGE_WORKER.md) for versions and hashes.

M4 measured a 3.136 GiB VRAM / 2.947 GiB RAM cold peak for text and up to 0.082 GiB VRAM / 0.170 GiB RAM additional warm demand. Load took 3.315 s, first token 0.993 s, and a 371-token reply averaged 99.87 tokens/s after its first token. The text profile retains conservative 5 GiB VRAM / 6 GiB RAM cold and 0.5/0.5 GiB warm bounds.

For M8, three successive text → image → text cycles completed through durable jobs. All three image artifacts survived an independent backup restore, and every follow-up named its artifact ID without placing image bytes in text context. System-wide free-memory samples taken about every 0.5 s started at 9.97 GiB RAM and 6.56 GiB VRAM. The minimum observed at any point was 7.26 GiB RAM and 3.52 GiB VRAM; samples while the image profile was identified had minima of 7.86 GiB RAM and 4.36 GiB VRAM. Both remained above the admission reserves. These samples include other desktop activity and do not isolate image-worker cold or warm peaks; some startup samples precede assignment of a profile identity. Accordingly, the image profile remains `calibrated = false` with provisional 5.5 GiB VRAM / 6 GiB RAM cold and 0.5/0.5 GiB warm headroom bounds. Cold/warm peak timings for the image worker remain unmeasured.

After the three cycles, the workers were stopped, no image or text worker process remained, and free memory was about 10.14 GiB RAM / 6.63 GiB VRAM. These are observations from this PC, not guaranteed resource requirements on another machine.
