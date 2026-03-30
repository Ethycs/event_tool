# Quick Start

Convert any HuggingFace model to run on the Ryzen AI NPU.

## Prerequisites

`ryzenai-python` on PATH with providers working:
```bash
ryzenai-python -c "import onnxruntime; print(onnxruntime.get_available_providers())"
# ['VitisAIExecutionProvider', 'DmlExecutionProvider', 'CPUExecutionProvider']
```

If not set up yet, see [SETUP.md](SETUP.md).

## Convert a Model

```bash
ryzenai-python -m agent gpt2 --output-dir ./out --device npu
```

This runs 4 steps:
1. **Export** — downloads from HuggingFace, converts to ONNX
2. **Quantize** — applies INT8 quantization (or `--precision bf16`)
3. **Validate** — checks which ops the NPU supports
4. **Infer** — runs a test inference on the NPU

Output:
```
out/gpt2/
  model.onnx          # full-precision (623 MB)
  model_int8.onnx      # quantized (270 MB)
```

## Examples

```bash
# GPT-2 on NPU (default)
ryzenai-python -m agent gpt2 --output-dir ./out

# Phi-2 with BF16 on iGPU
ryzenai-python -m agent microsoft/phi-2 --precision bf16 --device gpu --output-dir ./out

# Export only, skip inference
ryzenai-python -m agent gpt2 --output-dir ./out --skip-run

# Custom sequence length
ryzenai-python -m agent gpt2 --seq-length 256 --output-dir ./out
```

## Device Modes

| Flag | Providers | Best for |
|------|-----------|----------|
| `--device npu` | NPU → iGPU → CPU | Default, fastest for quantized models |
| `--device gpu` | iGPU → CPU | Float models, larger batch sizes |
| `--device cpu` | CPU only | Debugging, baseline comparison |

## Use the Model from Another Project

Copy `interlock.py` + `_interlock_worker.py` into your project, then:

```python
from interlock import RyzenSession
import numpy as np

with RyzenSession("out/gpt2/model_int8.onnx", device="npu") as sess:
    ids = np.array([[101, 2054, 2003, 1996, 3462, 102]], dtype=np.int64)
    mask = np.ones_like(ids)
    out = sess.run({"input_ids": ids, "attention_mask": mask})
    print(out["logits"].shape)  # (1, 6, 50257)
```

Only needs `numpy`. No onnxruntime install required in your project.

## All Options

```
ryzenai-python -m agent --help

positional arguments:
  model_id              HuggingFace model ID (e.g. gpt2, microsoft/phi-2)

options:
  --precision {int8,bf16}   Quantization (default: int8)
  --device {npu,gpu,cpu}    Execution target (default: npu)
  --output-dir DIR          Where to save models (default: agent/out)
  --cache-dir DIR           VAI-EP compilation cache (default: agent/cache)
  --opset N                 ONNX opset version (default: 17)
  --seq-length N            Sequence length for test inputs (default: 128)
  --skip-run                Skip inference after conversion
  --trust-remote-code       Trust remote code from HuggingFace
  -v, --verbose             Debug logging
```

## Pre-optimized LLMs (OGA)

For 7B+ models, use AMD's pre-optimized OGA models instead of the custom pipeline. They run on the NPU + iGPU at ~7 tok/s.

```bash
# Install OGA
ryzenai-pip install onnxruntime-genai-directml-ryzenai==0.11.2 --extra-index-url=https://pypi.amd.com/simple

# Download (use 1.7-specific models)
git clone https://huggingface.co/amd/Qwen2.5-7B-Instruct-onnx-ryzenai-1.7-hybrid

# Run
ryzenai-python -c "
import onnxruntime_genai as og
model = og.Model('Qwen2.5-7B-Instruct-onnx-ryzenai-1.7-hybrid')
tokenizer = og.Tokenizer(model)
params = og.GeneratorParams(model)
params.set_search_options(max_length=256)
generator = og.Generator(model, params)
generator.append_tokens(tokenizer.encode('What is 2+2?'))
tokens = []
while not generator.is_done():
    generator.generate_next_token()
    tokens.append(generator.get_next_tokens()[0])
print(tokenizer.decode(tokens))
"
```

Browse all available models: [AMD Ryzen AI 1.7 Hybrid LLMs](https://huggingface.co/collections/amd/ryzen-ai-17-hybrid-llm)
