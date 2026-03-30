# Interlock: Using the Global `ryzenai-python` from Any Pixi Project

## Problem

Running models on the Ryzen AI NPU requires ~1.5 GB of wheels, 17 native DLLs, pinned `numpy<2`, and conda C++ libraries. Installing all of this into every project env is wasteful and fragile.

## Solution

Install the VAI-EP stack **once** in a global pixi environment. Any project calls out to `ryzenai-python` via subprocess for inference — zero AI dependencies in the project env.

```
┌─────────────────────────┐     subprocess      ┌──────────────────────────┐
│   Your pixi project     │ ──── stdin/stdout ──▶│  ryzenai-python          │
│                         │     (JSON + .npy)    │                          │
│  - no onnxruntime       │                      │  - onnxruntime-vitisai   │
│  - no voe               │                      │  - voe, DLLs, xclbins   │
│  - no numpy<2 pin       │                      │  - numpy, protobuf, etc  │
│  - just subprocess call │ ◀── results ─────────│  - VitisAI + DML + CPU   │
└─────────────────────────┘                      └──────────────────────────┘
```

## Prerequisites

### 1. Global ryzenai environment

Already set up via `pixi-global.toml`:

```toml
[envs.ryzenai]
channels = ["conda-forge"]
dependencies = {
  python = "3.12.*",
  spdlog = "1.15.*",
  nlohmann_json = "3.12.*",
  eigen = "3.4.*",
  xtensor = "0.26.*",
  xtl = "0.8.*",
  libabseil = "20250512.*",
  libprotobuf = "6.31.*",
  libprotobuf-static = "6.31.*",
  dlfcn-win32 = ">=1.4.2,<2",
  zlib = "1.3.1.*",
  pip = "*"
}
exposed = { ryzenai-python = "python", ryzenai-pip = "pip" }
```

### 2. Ryzen AI wheels installed into the global env

```bash
ryzenai-pip install agent/ryzenai/wheels/onnxruntime_vitisai-1.23.2-cp312-cp312-win_amd64.whl
ryzenai-pip install agent/ryzenai/wheels/voe-1.7.0-py3-none-win_amd64.whl
ryzenai-pip install agent/ryzenai/wheels/ryzenai_dynamic_dispatch-1.7.0-cp312-cp312-win_amd64.whl
# ... and the rest of the extracted wheels
```

### 3. Deployment DLLs copied into the global env's onnxruntime capi directory

```bash
# Find the global env's site-packages
ryzenai-python -c "import onnxruntime; print(onnxruntime.__file__)"
# Copy all DLLs from agent/ryzenai/deployment/ into that capi/ directory
```

### 4. Verify

```bash
ryzenai-python -c "import onnxruntime; print(onnxruntime.get_available_providers())"
# Should print: ['VitisAIExecutionProvider', 'DmlExecutionProvider', 'CPUExecutionProvider']
```

## How the Interlock Works

### Worker script

A small Python script that runs inside `ryzenai-python`. It:
1. Accepts a model path and device choice (`npu`, `gpu`, `cpu`)
2. Creates an `ort.InferenceSession` with the appropriate provider stack
3. Reads input arrays from `.npy` files
4. Runs inference
5. Writes output arrays to `.npy` files
6. Returns metadata (shapes, timing, active providers) as JSON on stdout

### Provider stacking by device

| `device` | Provider order | What runs where |
|----------|---------------|-----------------|
| `npu` | VitisAI → DirectML → CPU | Quantized matmuls on NPU, float ops on iGPU, rest on CPU |
| `gpu` | DirectML → CPU | Everything on iGPU, unsupported on CPU |
| `cpu` | CPU | Everything on CPU |

ONNX Runtime handles graph partitioning automatically. Each EP calls `GetCapability()` to claim the ops it supports. Ops not claimed fall through to the next EP.

### Client module

A lightweight Python module (no AI dependencies) that:
1. Locates `ryzenai-python` on PATH (installed by the global pixi env)
2. Spawns it as a subprocess running the worker script
3. Sends commands via stdin (JSON lines)
4. Passes numpy arrays via temp `.npy` files (avoids pipe size limits)
5. Reads results from stdout JSON + `.npy` files

### Protocol

All messages are single-line JSON on stdin/stdout.

**Init** (client → worker):
```json
{"cmd": "init", "model_path": "/abs/path/model.onnx", "device": "npu", "cache_dir": "/abs/path/cache", "cache_key": "abc123", "io_dir": "/tmp/ryzenai_io_xxx"}
```

**Ready** (worker → client):
```json
{"status": "ready", "providers": ["VitisAIExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"], "inputs": [{"name": "input_ids", "shape": [1, 128], "dtype": "int64"}], "outputs": [{"name": "logits", "shape": [1, 128, 50257], "dtype": "float32"}]}
```

**Run** (client → worker):
```json
{"cmd": "run", "inputs": {"input_ids": "/tmp/ryzenai_io_xxx/in_input_ids.npy", "attention_mask": "/tmp/ryzenai_io_xxx/in_attention_mask.npy"}}
```

**Result** (worker → client):
```json
{"status": "ok", "elapsed_s": 0.042, "outputs": {"logits": "/tmp/ryzenai_io_xxx/out_logits.npy"}}
```

**Quit** (client → worker):
```json
{"cmd": "quit"}
```

## Using from Any Pixi Project

### Project pyproject.toml

No Ryzen AI dependencies needed:

```toml
[project]
dependencies = ["numpy"]  # only for reading .npy results

[tool.pixi.tasks]
infer = "python run_my_model.py"
```

### Example: run_my_model.py

```python
from interlock import RyzenSession
import numpy as np

ids = np.array([[101, 2054, 2003, 1996, 3462, 102]], dtype=np.int64)
mask = np.ones_like(ids)

with RyzenSession("model.onnx", device="npu") as sess:
    print("Providers:", sess.get_providers())
    out = sess.run({"input_ids": ids, "attention_mask": mask})
    print("Logits shape:", out["logits"].shape)
    print("Inference time:", out["_elapsed_s"][0], "s")
```

### Example: event_tool integration

Replace the OpenRouter API call with local inference:

```python
# Before (API call)
resp = await client.chat.completions.create(model=cfg.model, ...)

# After (local NPU inference via interlock)
from interlock import RyzenSession

with RyzenSession("phi-3-mini-int8.onnx", device="npu") as sess:
    tokens = tokenizer.encode(prompt)
    out = sess.run({"input_ids": np.array([tokens], dtype=np.int64)})
    response = tokenizer.decode(out["logits"].argmax(-1)[0])
```

## Files

| File | Location | Purpose |
|------|----------|---------|
| `interlock.py` | Project that calls the NPU | Client — spawns `ryzenai-python`, sends/receives data |
| `_interlock_worker.py` | Global env or project | Worker — runs inside `ryzenai-python`, owns the session |
| `pixi-global.toml` | `~/.pixi/manifests/` | Defines the `ryzenai` global environment |

## Porting to Another Project

### Prerequisites (one-time)

The global `ryzenai` pixi env must be set up on the machine. See [SETUP.md](SETUP.md) for full instructions. Verify with:

```bash
ryzenai-python -c "import onnxruntime; print(onnxruntime.get_available_providers())"
# ['VitisAIExecutionProvider', 'DmlExecutionProvider', 'CPUExecutionProvider']
```

### Steps

1. **Copy two files** into any package directory in your project:
   ```
   your_project/
     src/
       your_package/
         interlock.py            # ← copy from agent/interlock.py
         _interlock_worker.py    # ← copy from agent/_interlock_worker.py
   ```
   They must be siblings (same directory). No other files needed.

2. **Add numpy** as a dependency (the only requirement):
   ```bash
   pixi add --pypi numpy
   ```

3. **Use it:**
   ```python
   from your_package.interlock import RyzenSession
   import numpy as np

   with RyzenSession("path/to/model.onnx", device="npu") as sess:
       out = sess.run({"input": np.random.rand(1, 3, 32, 32).astype(np.float32)})
       print(out["output"].shape)
   ```

### What you DON'T need in the project

- No `onnxruntime` or `onnxruntime-vitisai`
- No `voe` or any Ryzen AI wheels
- No `numpy<2` pin (the global env handles that)
- No DLLs, xclbins, or vaip_config.json
- No conda dependencies
- No activate.bat or environment variables

### Defaults

| Parameter | Default | Override |
|-----------|---------|----------|
| `device` | `"npu"` | `"gpu"` or `"cpu"` |
| `cache_dir` | `"agent/cache"` | Any writable directory for VAI-EP compilation cache |
| `cache_key` | Auto (SHA256 of model) | String key for cache lookup |

## Why Not Just Use pixi project dependencies?

| Approach | Pros | Cons |
|----------|------|------|
| All deps in project | Single env, simple | 1.5 GB wheels per project, numpy<2 pin conflicts, DLL colocation headaches |
| Global env + interlock | Zero AI deps in project, works everywhere, one install | Subprocess overhead (~50ms startup), temp file I/O for arrays |

The subprocess overhead is negligible compared to model inference time (typically 10-500ms per run). For batch workloads, keep the session alive across multiple `run()` calls.
