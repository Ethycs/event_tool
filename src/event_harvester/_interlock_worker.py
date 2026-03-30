#!/usr/bin/env python
"""Interlock worker — runs inside ``ryzenai-python``.

Reads JSON-line commands on stdin, performs ONNX Runtime inference, and writes
JSON-line responses on stdout.  All diagnostic output goes to stderr so that
the stdout channel stays clean for the protocol.

Supports two backends:
  - **ORT**: standard ``ort.InferenceSession`` for single ONNX files
  - **OGA**: ``onnxruntime_genai`` for AMD pre-optimized hybrid models (NPU+iGPU)

The backend is auto-detected: if ``model_path`` is a directory containing
``genai_config.json``, OGA is used; otherwise ORT.

This script is never imported directly; it is spawned by ``interlock.py``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np


def _log(msg: str) -> None:
    """Print a diagnostic message to stderr (never stdout)."""
    print(f"[interlock-worker] {msg}", file=sys.stderr, flush=True)


def _respond(obj: dict[str, Any]) -> None:
    """Send a JSON-line response on stdout."""
    print(json.dumps(obj, separators=(",", ":")), flush=True)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_backend: str = ""  # "ort" or "oga"
_session: Any = None
_tokenizer: Any = None
_oga_model: Any = None
_oga_tokenizer: Any = None
_io_dir: Path | None = None


# ---------------------------------------------------------------------------
# ORT backend
# ---------------------------------------------------------------------------

def _build_providers(device: str, cache_dir: Path, cache_key: str) -> tuple[list[Any], Any]:
    """Return (provider list, session options) for the requested device."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.log_severity_level = 3  # warnings only

    available = set(ort.get_available_providers())

    if device == "npu":
        providers: list[Any] = []
        if "VitisAIExecutionProvider" in available:
            vaip_config = cache_dir / "vaip_config.json"
            compile_cache = cache_dir / cache_key
            compile_cache.mkdir(parents=True, exist_ok=True)
            providers.append(
                (
                    "VitisAIExecutionProvider",
                    {
                        "config_file": str(vaip_config),
                        "cacheDir": str(compile_cache),
                        "cacheKey": cache_key,
                    },
                )
            )
        if "DmlExecutionProvider" in available:
            providers.append("DmlExecutionProvider")
        providers.append("CPUExecutionProvider")

    elif device == "gpu":
        providers = []
        if "DmlExecutionProvider" in available:
            providers.append("DmlExecutionProvider")
        providers.append("CPUExecutionProvider")

    else:  # cpu
        providers = ["CPUExecutionProvider"]

    return providers, opts


def _init_ort(msg: dict[str, Any]) -> None:
    """Initialise using standard ort.InferenceSession."""
    global _session, _tokenizer, _backend
    import onnxruntime as ort

    model_path = Path(msg["model_path"])
    device = msg.get("device", "npu")
    cache_dir = Path(msg.get("cache_dir", "agent/cache"))
    cache_key = msg.get("cache_key", "default")
    tokenizer_path = msg.get("tokenizer_path")

    cache_dir.mkdir(parents=True, exist_ok=True)

    _log(f"[ORT] Loading {model_path.name} on device={device}")
    providers, opts = _build_providers(device, cache_dir, cache_key)

    try:
        _session = ort.InferenceSession(str(model_path), sess_options=opts, providers=[
            p if isinstance(p, str) else p[0] for p in providers
        ], provider_options=[
            {} if isinstance(p, str) else p[1] for p in providers
        ])
    except Exception as exc:
        _respond({"status": "error", "error": str(exc)})
        return

    if tokenizer_path:
        try:
            from transformers import AutoTokenizer
            _tokenizer = AutoTokenizer.from_pretrained(
                tokenizer_path, trust_remote_code=True,
            )
            _log(f"Tokenizer loaded from {tokenizer_path}")
        except Exception as exc:
            _respond({"status": "error", "error": f"Tokenizer load failed: {exc}"})
            return

    _backend = "ort"
    active_providers = _session.get_providers()
    inputs = [
        {"name": i.name, "shape": i.shape, "type": i.type}
        for i in _session.get_inputs()
    ]
    outputs = [
        {"name": o.name, "shape": o.shape, "type": o.type}
        for o in _session.get_outputs()
    ]

    _log(f"Ready — providers: {active_providers}")
    _respond({
        "status": "ready",
        "providers": active_providers,
        "inputs": inputs,
        "outputs": outputs,
    })


# ---------------------------------------------------------------------------
# OGA backend (onnxruntime-genai for hybrid NPU+iGPU models)
# ---------------------------------------------------------------------------

def _init_oga(msg: dict[str, Any]) -> None:
    """Initialise using onnxruntime_genai (OGA) for pre-optimised models."""
    global _oga_model, _oga_tokenizer, _backend

    model_dir = str(Path(msg["model_path"]))

    _log(f"[OGA] Loading model from {model_dir}")

    try:
        import onnxruntime_genai as og
        _oga_model = og.Model(model_dir)
        _oga_tokenizer = og.Tokenizer(_oga_model)
    except Exception as exc:
        _respond({"status": "error", "error": f"OGA model load failed: {exc}"})
        return

    _backend = "oga"
    _log("Ready — backend: OGA (NPU+iGPU hybrid)")
    _respond({
        "status": "ready",
        "providers": ["OGA-hybrid"],
        "inputs": [],
        "outputs": [],
    })


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_init(msg: dict[str, Any]) -> None:
    global _io_dir

    model_path = Path(msg["model_path"])
    _io_dir = Path(msg["io_dir"])

    # Auto-detect backend: directory with genai_config.json → OGA, else ORT.
    if model_path.is_dir() and (model_path / "genai_config.json").exists():
        _init_oga(msg)
    else:
        _init_ort(msg)


def _handle_run(msg: dict[str, Any]) -> None:
    if _backend != "ort" or _session is None or _io_dir is None:
        _respond({"status": "error", "error": "Raw run only supported with ORT backend."})
        return

    feed: dict[str, np.ndarray] = {}
    for name, path in msg.get("inputs", {}).items():
        feed[name] = np.load(path)

    output_names = [o.name for o in _session.get_outputs()]
    t0 = time.perf_counter()
    results = _session.run(output_names, feed)
    elapsed = time.perf_counter() - t0

    output_paths: dict[str, str] = {}
    for oname, arr in zip(output_names, results):
        p = _io_dir / f"out_{oname}.npy"
        np.save(p, arr)
        output_paths[oname] = str(p)

    _respond({"status": "ok", "elapsed_s": round(elapsed, 6), "outputs": output_paths})


def _handle_generate(msg: dict[str, Any]) -> None:
    """Text generation — dispatches to OGA or ORT autoregressive loop."""
    if _backend == "oga":
        _generate_oga(msg)
    elif _backend == "ort":
        _generate_ort(msg)
    else:
        _respond({"status": "error", "error": "No model loaded."})


def _generate_oga(msg: dict[str, Any]) -> None:
    """Generate text using onnxruntime_genai (OGA)."""
    import onnxruntime_genai as og

    messages = msg.get("messages", [])
    max_tokens = msg.get("max_tokens", 2048)

    # Build prompt using Qwen chat template.
    parts = []
    for m in messages:
        role, content = m["role"], m["content"]
        parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
    parts.append("<|im_start|>assistant\n")
    prompt = "\n".join(parts)

    input_ids = _oga_tokenizer.encode(prompt)

    params = og.GeneratorParams(_oga_model)
    params.set_search_options(max_length=len(input_ids) + max_tokens)

    t0 = time.perf_counter()
    generator = og.Generator(_oga_model, params)
    generator.append_tokens(input_ids)

    tokens: list[int] = []
    while not generator.is_done():
        generator.generate_next_token()
        tokens.append(generator.get_next_tokens()[0])

    elapsed = time.perf_counter() - t0
    text = _oga_tokenizer.decode(tokens)

    _log(f"Generated {len(tokens)} tokens in {elapsed:.2f}s ({len(tokens)/elapsed:.1f} tok/s)")
    _respond({
        "status": "ok",
        "text": text,
        "elapsed_s": round(elapsed, 6),
        "tokens_generated": len(tokens),
    })


def _generate_ort(msg: dict[str, Any]) -> None:
    """Autoregressive generation using standard ort.InferenceSession."""
    if _session is None:
        _respond({"status": "error", "error": "Session not initialised."})
        return
    if _tokenizer is None:
        _respond({"status": "error", "error": "No tokenizer loaded. Pass tokenizer_path in init."})
        return

    messages = msg.get("messages", [])
    max_tokens = msg.get("max_tokens", 2048)
    temperature = msg.get("temperature", 0.0)

    try:
        prompt_text = _tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        input_ids = _tokenizer.encode(prompt_text, return_tensors=None)
    except Exception as exc:
        _respond({"status": "error", "error": f"Tokenization failed: {exc}"})
        return

    input_names = [i.name for i in _session.get_inputs()]
    output_names = [o.name for o in _session.get_outputs()]

    eos_id = _tokenizer.eos_token_id
    generated_ids: list[int] = []

    t0 = time.perf_counter()

    ids = list(input_ids)
    for _ in range(max_tokens):
        feed: dict[str, np.ndarray] = {}
        ids_arr = np.array([ids], dtype=np.int64)
        attn_arr = np.ones_like(ids_arr)

        for name in input_names:
            if "input_ids" in name:
                feed[name] = ids_arr
            elif "attention_mask" in name:
                feed[name] = attn_arr
            elif "position_ids" in name:
                feed[name] = np.arange(len(ids), dtype=np.int64).reshape(1, -1)

        results = _session.run(output_names, feed)
        next_logits = results[0][0, -1, :]

        if temperature <= 0:
            next_id = int(np.argmax(next_logits))
        else:
            probs = _softmax(next_logits / temperature)
            next_id = int(np.random.choice(len(probs), p=probs))

        if next_id == eos_id:
            break

        ids.append(next_id)
        generated_ids.append(next_id)

    elapsed = time.perf_counter() - t0
    text = _tokenizer.decode(generated_ids, skip_special_tokens=True)

    _log(f"Generated {len(generated_ids)} tokens in {elapsed:.2f}s")
    _respond({
        "status": "ok",
        "text": text,
        "elapsed_s": round(elapsed, 6),
        "tokens_generated": len(generated_ids),
    })


def _softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x))
    return e / e.sum()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    _log("Worker started, waiting for commands...")
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _respond({"status": "error", "error": f"Invalid JSON: {line!r}"})
            continue

        cmd = msg.get("cmd")
        try:
            if cmd == "init":
                _handle_init(msg)
            elif cmd == "run":
                _handle_run(msg)
            elif cmd == "generate":
                _handle_generate(msg)
            elif cmd == "quit":
                _log("Quit requested, exiting.")
                break
            else:
                _respond({"status": "error", "error": f"Unknown command: {cmd!r}"})
        except Exception as exc:
            _log(f"Unhandled exception: {exc}")
            _respond({"status": "error", "error": str(exc)})

    _log("Worker exiting.")


if __name__ == "__main__":
    main()
