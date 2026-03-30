#!/usr/bin/env python
"""Interlock worker — runs inside ``ryzenai-python``.

Reads JSON-line commands on stdin, performs ONNX Runtime inference, and writes
JSON-line responses on stdout.  All diagnostic output goes to stderr so that
the stdout channel stays clean for the protocol.

This script is never imported directly; it is spawned by ``interlock.py``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort


def _log(msg: str) -> None:
    """Print a diagnostic message to stderr (never stdout)."""
    print(f"[interlock-worker] {msg}", file=sys.stderr, flush=True)


def _respond(obj: dict[str, Any]) -> None:
    """Send a JSON-line response on stdout."""
    print(json.dumps(obj, separators=(",", ":")), flush=True)


# ---------------------------------------------------------------------------
# Provider stacking
# ---------------------------------------------------------------------------

def _build_providers(device: str, cache_dir: Path, cache_key: str) -> tuple[list[Any], ort.SessionOptions]:
    """Return (provider list, session options) for the requested device."""
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


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

_session: ort.InferenceSession | None = None
_io_dir: Path | None = None


def _handle_init(msg: dict[str, Any]) -> None:
    global _session, _io_dir

    model_path = Path(msg["model_path"])
    device = msg.get("device", "npu")
    cache_dir = Path(msg.get("cache_dir", "agent/cache"))
    cache_key = msg.get("cache_key", "default")
    _io_dir = Path(msg["io_dir"])

    cache_dir.mkdir(parents=True, exist_ok=True)

    _log(f"Loading model {model_path.name} on device={device}")
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


def _handle_run(msg: dict[str, Any]) -> None:
    if _session is None or _io_dir is None:
        _respond({"status": "error", "error": "Session not initialised."})
        return

    # Load input arrays.
    feed: dict[str, np.ndarray] = {}
    for name, path in msg.get("inputs", {}).items():
        feed[name] = np.load(path)

    # Run inference.
    output_names = [o.name for o in _session.get_outputs()]
    t0 = time.perf_counter()
    results = _session.run(output_names, feed)
    elapsed = time.perf_counter() - t0

    # Save output arrays.
    output_paths: dict[str, str] = {}
    for oname, arr in zip(output_names, results):
        p = _io_dir / f"out_{oname}.npy"
        np.save(p, arr)
        output_paths[oname] = str(p)

    _respond({"status": "ok", "elapsed_s": round(elapsed, 6), "outputs": output_paths})


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
