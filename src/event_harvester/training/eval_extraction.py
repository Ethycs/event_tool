"""A/B two LLMs on the extraction stage, over the same real batches.

**Why this exists.** The extraction model is the only cost-bearing stage, and
the question "could a much weaker model do this?" was going to be answered by
opinion. It is answerable by measurement, because the task is narrow and the
inputs are on disk: 817 of 20,716 messages reach the LLM (3.9%), ~82 calls per
harvest, ~1.9k prompt tokens at p95, and **32% of those messages arrive with a
date already resolved into ISO by `build_prompt`**. That is a structured-
extraction task, not a reasoning one — exactly where a small model may hold up,
and exactly where guessing is unwise.

What is compared, per model, over an identical set of batches:

* **events extracted** — the headline, but on its own it rewards a model that
  hallucinates,
* **batches that failed to parse** — a malformed reply costs all 10 messages in
  the batch silently, so this is the failure mode that matters most,
* **field fill rates** — date / time / location / link that are not `TBD`. An
  event with no date never reaches the calendar page,
* **agreement with the baseline** — how many of A's events B also found, matched
  with the pipeline's own `events_match` so the comparison uses the same notion
  of "same event" the deduper does,
* **latency and tokens**.

The sample is deterministic: the same messages, in the same batches, in the same
order, for every model — so a difference is the model and not the draw.

    pixi run event-harvester eval-extraction \\
        --baseline openai/gpt-5.4-mini-2026-03-17 \\
        --candidate ollama/qwen3:8b \\
        --api-base https://central-dev.lan:4000/v1 \\
        --batches 20

**This spends money on the baseline.** `--batches` is small by default for that
reason, and every run prints what it is about to spend before it starts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from event_harvester.observability.tracing import traced
from event_harvester.pipeline.analysis import (
    _EVENT_EXTRACTION_PROMPT,
    _parse_event_ini,
    build_prompt,
    score_message,
)
from event_harvester.ui.display import BOLD, DIM, GREEN, RESET, YELLOW

logger = logging.getLogger("event_harvester")
_TRACER = "event_harvester.training.eval_extraction"

# The fields the report page actually shows. A model that returns a title and
# `TBD` for everything else has not done the job, however many rows it emits.
FIELDS = ("date", "time", "location", "link")

# `_parse_event_ini` maps a missing value to "TBD"; treat every spelling of
# "I don't know" as unfilled.
_EMPTY = {"", "tbd", "unknown", "n/a", "none", "null", "-"}


@dataclass
class ModelResult:
    """What one model did over the sample."""

    model: str
    events: list[dict] = field(default_factory=list)
    parse_failures: int = 0
    call_errors: int = 0
    batches: int = 0
    latencies: list[float] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_samples: list[str] = field(default_factory=list)

    @property
    def fill_rates(self) -> dict[str, float]:
        if not self.events:
            return {f: 0.0 for f in FIELDS}
        return {
            f: sum(1 for e in self.events
                   if str(e.get(f, "")).strip().lower() not in _EMPTY) / len(self.events)
            for f in FIELDS
        }

    def to_dict(self) -> dict[str, Any]:
        lat = sorted(self.latencies)
        return {
            "model": self.model,
            "batches": self.batches,
            "events": len(self.events),
            "events_per_batch": round(len(self.events) / max(1, self.batches), 2),
            "parse_failures": self.parse_failures,
            "call_errors": self.call_errors,
            "fill_rates": {k: round(v, 3) for k, v in self.fill_rates.items()},
            "latency_s": {
                "p50": round(lat[len(lat) // 2], 2) if lat else None,
                "p95": round(lat[int(len(lat) * 0.95)], 2) if lat else None,
                "total": round(sum(lat), 1),
            },
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def load_sample(batches: int, batch_size: int = 10,
                cache_path: str | Path | None = None) -> list[list[dict]]:
    """The same messages the pipeline would have sent, in the same batches.

    Taken from the shared message cache and ordered by `score_message`, which
    is the ordering `prioritize_with_caps` uses — so this is the head of the
    real queue rather than a random draw. Deterministic by construction: no
    sampling, no shuffle.
    """
    from event_harvester.utils import data_path

    path = Path(cache_path) if cache_path else data_path(".message_cache.json")
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    messages = raw.get("messages", raw) if isinstance(raw, dict) else raw
    if isinstance(messages, dict):
        messages = [m for group in messages.values() for m in group]

    usable = [
        m for m in messages
        if isinstance(m, dict) and m.get("content") and m.get("timestamp")
        and m.get("platform") and m.get("channel")
    ]
    if not usable:
        raise RuntimeError(
            f"no usable messages in {path}. Run a harvest first — the cache has "
            "a 4h TTL and this eval deliberately does not scrape.",
        )
    ranked = sorted(usable, key=score_message, reverse=True)[:batches * batch_size]
    return [ranked[i:i + batch_size] for i in range(0, len(ranked), batch_size)]


def _call(model: str, prompt: str, *, api_base: str | None, api_key: str | None,
          max_tokens: int, timeout: float) -> tuple[str, Any]:
    """One completion through litellm. Returns (text, usage)."""
    from litellm import completion

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _EVENT_EXTRACTION_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        # Determinism where the provider honours it: this is a measurement, and
        # a sampled difference between runs would be indistinguishable from a
        # difference between models.
        "temperature": 0,
        "timeout": timeout,
    }
    if api_base:
        kwargs["api_base"] = api_base
    if api_key:
        kwargs["api_key"] = api_key
    if "gpt-5.4" in model:
        kwargs["reasoning_effort"] = "none"
    elif any(tag in model for tag in ("gpt-5", "o1-", "o3-")):
        kwargs["reasoning_effort"] = "minimal"

    resp = completion(**kwargs)
    return (resp.choices[0].message.content or ""), getattr(resp, "usage", None)


@traced("training.eval_extraction.model", tracer_name=_TRACER)
def run_model(model: str, sample: list[list[dict]], *, days_back: int = 7,
              api_base: str | None = None, api_key: str | None = None,
              max_tokens: int = 4096, timeout: float = 120.0,
              keep_raw: int = 2) -> ModelResult:
    """Run one model over every batch and record what it did."""
    result = ModelResult(model=model)
    for index, batch in enumerate(sample, 1):
        prompt = build_prompt(batch, days_back)
        started = time.monotonic()
        try:
            text, usage = _call(model, prompt, api_base=api_base, api_key=api_key,
                                max_tokens=max_tokens, timeout=timeout)
        except Exception as e:  # noqa: BLE001 -- one bad batch must not end the eval
            result.call_errors += 1
            result.batches += 1
            logger.warning("%s: batch %d failed — %s: %s", model, index,
                           type(e).__name__, e)
            continue
        result.latencies.append(time.monotonic() - started)
        result.batches += 1
        if usage is not None:
            result.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            result.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

        # The real parser, not a lenient one written for the eval. A reply this
        # cannot read is a reply the pipeline would have thrown away.
        _summary, events, _dropped = _parse_event_ini(text)
        if not events:
            result.parse_failures += 1
            if len(result.raw_samples) < keep_raw:
                result.raw_samples.append(text[:600])
        result.events.extend(events)
        print(f"  {DIM}{model}  batch {index}/{len(sample)}  "
              f"{len(events)} event(s){RESET}")
    return result


def agreement(baseline: list[dict], candidate: list[dict]) -> dict[str, Any]:
    """How much of the baseline's output the candidate also found.

    Uses `event_match.events_match`, so "the same event" means here what it
    means to the deduper. Recall is the number that matters: an event the
    baseline found and the candidate missed is an event that stops appearing on
    the page.
    """
    from event_harvester.pipeline.event_match import events_match

    # `events_match` returns `(is_match, score)`. A tuple is always truthy, so
    # `if events_match(...)` matches everything — the first version of this
    # function scored 100% recall against any candidate at all, which is the
    # most dangerous possible failure for a measurement tool. Unpack it.
    def same(x: dict, y: dict) -> bool:
        return events_match(x, y)[0]

    matched = [b for b in baseline if any(same(b, c) for c in candidate)]
    extra = [c for c in candidate if not any(same(b, c) for b in baseline)]
    return {
        "baseline_events": len(baseline),
        "candidate_events": len(candidate),
        "matched": len(matched),
        "recall": round(len(matched) / len(baseline), 3) if baseline else None,
        "candidate_only": len(extra),
        "missed_titles": [b.get("title", "?") for b in baseline
                          if b not in matched][:10],
    }


def _pct(x: float | None) -> str:
    return "  —  " if x is None else f"{100 * x:5.1f}%"


def _report(a: ModelResult, b: ModelResult, agree: dict[str, Any]) -> None:
    print(f"\n{BOLD}Extraction A/B{RESET}   {a.batches} identical batches\n")
    rows = [
        ("events extracted", str(len(a.events)), str(len(b.events))),
        ("events per batch", f"{len(a.events)/max(1,a.batches):.1f}",
         f"{len(b.events)/max(1,b.batches):.1f}"),
        ("batches that parsed", f"{a.batches - a.parse_failures}/{a.batches}",
         f"{b.batches - b.parse_failures}/{b.batches}"),
        ("call errors", str(a.call_errors), str(b.call_errors)),
    ]
    for field_name in FIELDS:
        rows.append((f"{field_name} filled",
                     _pct(a.fill_rates[field_name]), _pct(b.fill_rates[field_name])))
    la, lb = sorted(a.latencies), sorted(b.latencies)
    rows.append(("latency p50",
                 f"{la[len(la)//2]:.1f}s" if la else "—",
                 f"{lb[len(lb)//2]:.1f}s" if lb else "—"))
    rows.append(("wall clock", f"{sum(a.latencies):.0f}s", f"{sum(b.latencies):.0f}s"))

    width = max(len(r[0]) for r in rows)
    print(f"  {'':<{width}}   {BOLD}{a.model[:26]:>26}{RESET}   "
          f"{BOLD}{b.model[:26]:>26}{RESET}")
    for label, left, right in rows:
        print(f"  {label:<{width}}   {left:>26}   {right:>26}")

    colour = GREEN if (agree.get("recall") or 0) >= 0.8 else YELLOW
    print(f"\n  {BOLD}Agreement{RESET} — of the baseline's {agree['baseline_events']} "
          f"event(s), the candidate found "
          f"{colour}{agree['matched']} ({_pct(agree['recall']).strip()}){RESET}")
    print(f"  {DIM}candidate also produced {agree['candidate_only']} event(s) the "
          f"baseline did not — check these by hand, they are either finds or "
          f"inventions{RESET}")
    if agree["missed_titles"]:
        print(f"\n  {DIM}missed by the candidate:{RESET}")
        for title in agree["missed_titles"]:
            print(f"    {DIM}- {title[:70]}{RESET}")

    for result in (a, b):
        if result.raw_samples:
            print(f"\n  {YELLOW}{result.model} produced {result.parse_failures} "
                  f"unparseable batch(es). First:{RESET}")
            print(f"  {DIM}{result.raw_samples[0][:300]}{RESET}")


@traced("training.eval_extraction", tracer_name=_TRACER)
def run_ab(baseline: str, candidate: str, *, batches: int = 20,
           batch_size: int = 10, days_back: int = 7,
           api_base: str | None = None, api_key: str | None = None,
           candidate_api_base: str | None = None,
           candidate_api_key: str | None = None,
           save: str | None = None, cache_path: str | None = None) -> dict[str, Any]:
    """Run both models over one sample and print the comparison."""
    sample = load_sample(batches, batch_size, cache_path)
    messages = sum(len(b) for b in sample)
    print(f"\n{BOLD}[ Extraction A/B ]{RESET}")
    print(f"  {len(sample)} batch(es), {messages} message(s), from the shared cache")
    print(f"  A  {baseline}")
    print(f"  B  {candidate}")
    print(f"  {DIM}two calls per batch — {2 * len(sample)} completions total{RESET}\n")

    a = run_model(baseline, sample, days_back=days_back,
                  api_base=api_base, api_key=api_key)
    b = run_model(candidate, sample, days_back=days_back,
                  api_base=candidate_api_base if candidate_api_base is not None else api_base,
                  api_key=candidate_api_key if candidate_api_key is not None else api_key)

    agree = agreement(a.events, b.events)
    _report(a, b, agree)

    payload = {
        "sample": {"batches": len(sample), "messages": messages,
                   "batch_size": batch_size, "days_back": days_back},
        "baseline": a.to_dict(),
        "candidate": b.to_dict(),
        "agreement": agree,
    }
    if save:
        out = Path(save)
        out.parent.mkdir(parents=True, exist_ok=True)
        # The events themselves, so a disagreement can be read rather than
        # re-run — the eval is the expensive part.
        payload["baseline"]["events_detail"] = a.events
        payload["candidate"]["events_detail"] = b.events
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(f"\n  {DIM}written to {out}{RESET}")
    print()
    return payload


def resolve_key(explicit: str | None, env_var: str = "EH_EVAL_API_KEY") -> str | None:
    """A key from the flag, else the environment. Never logged."""
    return explicit or os.environ.get(env_var) or None
