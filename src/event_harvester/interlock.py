"""Lightweight client for RyzenAI interlock — no AI dependencies required.

Spawns a ``ryzenai-python`` subprocess running ``_interlock_worker.py`` and
communicates over JSON-lines on stdin/stdout.  Numpy arrays are exchanged
via temporary ``.npy`` files to avoid pipe-size limitations.

Example (Qwen2.5-7B-int4)::

    with RyzenSession(Path("models/qwen2.5-7b-int4.onnx")) as sess:
        out = sess.run({"input_ids": ids_array, "attention_mask": mask_array})
        logits = out["logits"]
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("event_harvester.interlock")

# Ceiling on how long to wait for any single worker reply before treating the
# worker as wedged. Deliberately generous: first-run NPU compilation can take
# minutes, and a false positive here kills a healthy session. The point is to
# convert "hangs forever" into "fails after a bounded wait", not to be tight.
DEFAULT_IO_TIMEOUT_S = 600.0

__all__ = ["RyzenSession", "InterlockError"]

_WORKER_SCRIPT = Path(__file__).with_name("_interlock_worker.py")


class InterlockError(RuntimeError):
    """Raised when the worker reports an error or dies unexpectedly."""


def _find_ryzenai_python() -> str:
    """Return the absolute path to ``ryzenai-python`` on PATH."""
    exe = shutil.which("ryzenai-python")
    if exe is None:
        raise FileNotFoundError(
            "ryzenai-python is not on PATH. "
            "Install the RyzenAI SDK or add its bin directory to PATH."
        )
    return exe


def _model_cache_key(model_path: Path) -> str:
    """SHA-256 hex digest of *model_path* (first 16 hex chars)."""
    h = hashlib.sha256()
    with model_path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()[:16]


class RyzenSession:
    """Context-managed ONNX inference session running inside ``ryzenai-python``.

    Parameters
    ----------
    model_path:
        Absolute or relative path to an ``.onnx`` model file.
    device:
        Execution device — ``"npu"``, ``"gpu"``, or ``"cpu"``.
    cache_dir:
        Directory used by VitisAI for compilation caching.
    cache_key:
        Optional override; defaults to SHA-256 of the model file.
    """

    def __init__(
        self,
        model_path: Path | str,
        *,
        device: str = "npu",
        cache_dir: Path | str = "agent/cache",
        cache_key: str | None = None,
        tokenizer_path: Path | str | None = None,
        io_timeout_s: float = DEFAULT_IO_TIMEOUT_S,
    ) -> None:
        self.model_path = Path(model_path).resolve()
        self.device = device
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_key = cache_key
        self.tokenizer_path = str(Path(tokenizer_path).resolve()) if tokenizer_path else None
        # 0 or None disables the timeout and restores the old block-forever
        # behaviour, for anyone debugging a worker under a breakpoint.
        self.io_timeout_s = io_timeout_s

        self._proc: subprocess.Popen[str] | None = None
        self._io_dir: Path | None = None
        self._providers: list[str] = []
        self._input_meta: list[dict[str, Any]] = []
        self._output_meta: list[dict[str, Any]] = []

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "RyzenSession":
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")

        # Compute cache key lazily (skip for directories / OGA models).
        if self.cache_key is None:
            if self.model_path.is_file():
                self.cache_key = _model_cache_key(self.model_path)
            else:
                self.cache_key = self.model_path.name

        # Create a temp directory for .npy exchange.
        self._io_dir = Path(tempfile.mkdtemp(prefix="ryzenai_io_"))

        # Spawn the worker.
        exe = _find_ryzenai_python()
        self._proc = subprocess.Popen(
            [exe, str(_WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,  # line-buffered
        )

        # Send init command.
        init_msg: dict[str, Any] = {
            "cmd": "init",
            "model_path": str(self.model_path),
            "device": self.device,
            "cache_dir": str(self.cache_dir),
            "cache_key": self.cache_key,
            "io_dir": str(self._io_dir),
        }
        if self.tokenizer_path:
            init_msg["tokenizer_path"] = self.tokenizer_path
        self._send(init_msg)

        # Wait for ready.
        resp = self._recv()
        if resp.get("status") != "ready":
            error = resp.get("error", "unknown error during init")
            raise InterlockError(f"Worker failed to initialise: {error}")

        self._providers = resp.get("providers", [])
        self._input_meta = resp.get("inputs", [])
        self._output_meta = resp.get("outputs", [])
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self._proc is not None and self._proc.poll() is None:
                self._send({"cmd": "quit"})
                self._proc.wait(timeout=5)
        except Exception:
            if self._proc is not None:
                self._proc.kill()
        finally:
            # Clean up temp directory.
            if self._io_dir is not None:
                shutil.rmtree(self._io_dir, ignore_errors=True)
            self._proc = None
            self._io_dir = None

    # -- public API -----------------------------------------------------------

    def run(self, inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Run inference and return output arrays.

        The returned dict also contains ``_elapsed_s`` as a 0-d float64 array.
        """
        if self._proc is None or self._proc.poll() is not None:
            raise InterlockError("Worker process is not running.")

        assert self._io_dir is not None

        # Save input arrays to .npy files.
        input_paths: dict[str, str] = {}
        for name, arr in inputs.items():
            p = self._io_dir / f"in_{name}.npy"
            np.save(p, arr)
            input_paths[name] = str(p)

        self._send({"cmd": "run", "inputs": input_paths})
        resp = self._recv()

        if resp.get("status") != "ok":
            raise InterlockError(f"Inference failed: {resp.get('error', 'unknown')}")

        # Load output arrays.
        result: dict[str, np.ndarray] = {}
        for name, path in resp.get("outputs", {}).items():
            result[name] = np.load(path)

        result["_elapsed_s"] = np.float64(resp.get("elapsed_s", 0.0))
        return result

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Run autoregressive text generation with chat messages.

        ``messages`` uses the OpenAI chat format::

            [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]

        Returns a dict with keys: ``text``, ``elapsed_s``, ``tokens_generated``.
        Requires ``tokenizer_path`` to have been set during init.
        """
        if self._proc is None or self._proc.poll() is not None:
            raise InterlockError("Worker process is not running.")

        self._send({
            "cmd": "generate",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        resp = self._recv()

        if resp.get("status") != "ok":
            raise InterlockError(f"Generation failed: {resp.get('error', 'unknown')}")

        return {
            "text": resp["text"],
            "elapsed_s": resp.get("elapsed_s", 0.0),
            "tokens_generated": resp.get("tokens_generated", 0),
        }

    def get_providers(self) -> list[str]:
        """Return the ONNX Runtime execution providers reported by the worker."""
        return list(self._providers)

    # -- internal helpers -----------------------------------------------------

    def _kill_wedged_worker(self) -> None:
        """Terminate an unresponsive worker; escalate to kill if it ignores us."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Local worker ignored terminate(); killing.")
                proc.kill()
                proc.wait(timeout=5)
        except Exception as exc:  # pragma: no cover - OS-dependent
            logger.warning("Failed to kill wedged local worker: %s", exc)

    def _send(self, obj: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            line = json.dumps(obj, separators=(",", ":"))
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except BrokenPipeError as exc:
            raise InterlockError("Worker process died unexpectedly.") from exc

    def _recv(self, timeout_s: float | None = None) -> dict[str, Any]:
        """Read one JSON line from the worker.

        ``readline()`` on a pipe blocks forever, and the only failure this
        used to recover from was a *clean* worker exit (which closes stdout
        and returns ""). A worker that is alive but wedged — hung on NPU
        init, stuck in a generate loop, deadlocked — would hang
        ``chat_completion(backend="local")`` indefinitely, with no timeout
        anywhere above it to break the wait.

        The read runs on an explicit **daemon** thread. That detail is
        load-bearing: `ThreadPoolExecutor` threads are non-daemon and are
        joined by an `atexit` hook, so abandoning a blocked `readline` on a
        pool thread hangs interpreter shutdown instead of the call — trading
        one hang for a worse one. A daemon thread is simply dropped.

        On timeout the worker is killed, because a session whose response we
        abandoned is desynchronised: the next ``_recv`` would read *this*
        call's reply.
        """
        assert self._proc is not None and self._proc.stdout is not None

        if timeout_s is None:
            timeout_s = self.io_timeout_s

        if timeout_s and timeout_s > 0:
            import threading

            box: dict[str, Any] = {}

            def _read() -> None:
                try:
                    box["line"] = self._proc.stdout.readline()  # type: ignore[union-attr]
                except Exception as exc:  # pragma: no cover - pipe torn down
                    box["error"] = exc

            t = threading.Thread(target=_read, name="ryzen-recv", daemon=True)
            t.start()
            t.join(timeout_s)

            if t.is_alive():
                logger.error(
                    "Local worker did not respond within %.0fs; killing it.",
                    timeout_s,
                )
                self._kill_wedged_worker()
                raise InterlockError(
                    f"Worker process timed out after {timeout_s:.0f}s "
                    f"(alive but unresponsive); killed."
                )
            if "error" in box:
                raise InterlockError(
                    f"Reading from worker failed: {box['error']}"
                ) from box["error"]
            line = box.get("line", "")
        else:
            line = self._proc.stdout.readline()

        if not line:
            raise InterlockError(
                "Worker process closed stdout (crashed or exited prematurely)."
            )
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise InterlockError(
                f"Worker sent invalid JSON: {line!r}"
            ) from exc
