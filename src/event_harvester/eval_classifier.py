"""Evaluate classifier accuracy against LLM ground-truth labels."""

import json
import logging
import random
from pathlib import Path

from event_harvester.analysis import _prioritize
from event_harvester.classifier import (
    filter_actionable as classifier_filter,
)
from event_harvester.classifier import (
    has_trained_models,
    predict,
)
from event_harvester.display import DIM, RED, RESET
from event_harvester.sources import filter_read_sent as gmail_filter
from event_harvester.weights import extract_events, prefilter_events

logger = logging.getLogger("event_harvester")

W = 64


def run_eval(
    load_labels: str,
    *,
    save_eval: str | None = None,
) -> None:
    """Evaluate classifier accuracy on labeled data.

    Prints classification reports, pipeline funnel simulation,
    keyword heuristic accuracy, and optionally saves eval samples.
    """
    if not has_trained_models():
        print(f"{RED}No classifier models found. Train first with --train-classifier.{RESET}\n")
        return

    print(f"\n{'=' * W}")
    print("[ Evaluating classifier ]")
    print(f"{'=' * W}\n")

    from event_harvester.utils import load_json
    all_labeled = load_json(load_labels, default=None)
    if all_labeled is None:
        logger.error("Failed to load labels from %s", load_labels)
        return

    eval_msgs = [{k: v for k, v in m.items() if k != "label"} for m in all_labeled]
    truth = [m["label"] for m in all_labeled]
    print(f"Loaded {len(eval_msgs)} labeled messages.")

    preds = predict(eval_msgs)

    from sklearn.metrics import classification_report, confusion_matrix

    pred_labels = [1 if p else 0 for p in preds]

    # ── Per-group classification reports ──────────────────────────────────
    def _is_email(m):
        return m.get("platform", "").lower() == "gmail"

    def _is_chat(m):
        return m.get("platform", "").lower() in ("discord", "telegram")

    classifier_groups = {
        "Email (classifier_email.pkl)": _is_email,
        "Chat (classifier_chat.pkl)": _is_chat,
    }

    for name, match_fn in classifier_groups.items():
        idxs = [i for i, m in enumerate(eval_msgs) if match_fn(m)]
        if not idxs:
            continue
        grp_truth = [truth[i] for i in idxs]
        grp_preds = [pred_labels[i] for i in idxs]

        print(f"\n=== {name} — {len(idxs)} messages ===")
        print(classification_report(grp_truth, grp_preds, target_names=["noise", "actionable"]))

        if len(set(grp_truth)) > 1 and len(set(grp_preds)) > 1:
            gcm = confusion_matrix(grp_truth, grp_preds)
            print(f"  {'':15s} pred_noise  pred_action")
            print(f"  {'true_noise':15s} {gcm[0][0]:>10d}  {gcm[0][1]:>11d}")
            print(f"  {'true_action':15s} {gcm[1][0]:>10d}  {gcm[1][1]:>11d}")

    # ── Per-platform breakdown (if multiple chat platforms) ───────────────
    chat_platforms = sorted(set(
        m.get("platform", "?") for m in eval_msgs
        if m.get("platform", "").lower() in ("discord", "telegram")
    ))
    if len(chat_platforms) > 1:
        for plat in chat_platforms:
            idxs = [i for i, m in enumerate(eval_msgs) if m.get("platform") == plat]
            if not idxs:
                continue
            plat_truth = [truth[i] for i in idxs]
            plat_preds = [pred_labels[i] for i in idxs]

            print(f"\n  --- {plat.capitalize()} ({len(idxs)} messages) ---")
            report = classification_report(
                plat_truth, plat_preds, target_names=["noise", "actionable"],
            )
            print(report)

    # ── Pipeline funnel simulation ────────────────────────────────────────
    print(f"\n{'=' * W}")
    print("[ Pipeline Funnel ]")
    print(f"{'=' * W}\n")

    total = len(eval_msgs)
    print(f"  {total:>6d}  total messages")

    # Stage 1: read/sent filter (Gmail only)
    after_read = gmail_filter(eval_msgs)
    n_read_sent = total - len(after_read)
    print(
        f"  {len(after_read):>6d}  after read/sent filter "
        f"({n_read_sent} Gmail read/sent dropped)"
    )

    # Stage 2: classifier
    after_clf = classifier_filter(after_read)
    n_clf = len(after_read) - len(after_clf)
    print(f"  {len(after_clf):>6d}  after classifier ({n_clf} noise dropped)")

    # Stage 3: keyword heuristic
    after_kw = _prioritize(after_clf)
    n_kw = len(after_clf) - len(after_kw)
    print(f"  {len(after_kw):>6d}  after keyword heuristic ({n_kw} dropped)")

    print(f"  {len(after_kw):>6d}  -> LLM for task extraction")

    # Event funnel
    events = extract_events(after_read)
    future_events = prefilter_events(events)
    print(
        f"\n  Events: {len(after_read)} messages -> "
        f"{len(events)} with dates -> "
        f"{len(future_events)} future events -> LLM"
    )

    # Build index: eval_msgs id -> truth label
    id_to_truth = {m["id"]: truth[i] for i, m in enumerate(eval_msgs)}
    after_read_ids = set(m["id"] for m in after_read)
    clf_kept_ids = set(m["id"] for m in after_clf)
    kw_kept_ids = set(m["id"] for m in after_kw)

    total_actionable = sum(truth)
    lost_read = sum(
        1 for m in eval_msgs
        if id_to_truth[m["id"]] == 1 and m["id"] not in after_read_ids
    )
    lost_clf = sum(
        1 for m in after_read
        if id_to_truth[m["id"]] == 1 and m["id"] not in clf_kept_ids
    )
    lost_kw = sum(1 for m in after_clf if id_to_truth[m["id"]] == 1 and m["id"] not in kw_kept_ids)
    survived = sum(1 for m in after_kw if id_to_truth[m["id"]] == 1)

    print("\n  Actionable messages through funnel:")
    print(f"    {total_actionable:>5d}  total actionable (per LLM labels)")
    if lost_read:
        print(f"    {lost_read:>5d}  lost at read/sent filter")
    if lost_clf:
        print(f"    {lost_clf:>5d}  lost at classifier")
    if lost_kw:
        print(f"    {lost_kw:>5d}  lost at keyword heuristic")
    recall_pct = 100 * survived / max(total_actionable, 1)
    print(
        f"    {survived:>5d}  reach the LLM "
        f"({survived}/{total_actionable} = {recall_pct:.0f}% recall)"
    )

    # ── Heuristic accuracy ────────────────────────────────────────────────
    print(f"\n{'=' * W}")
    print("[ Keyword Heuristic Accuracy (scored against LLM labels) ]")
    print(f"{'=' * W}\n")

    kw_preds = [1 if m["id"] in kw_kept_ids else 0 for m in after_clf]
    kw_truth = [id_to_truth[m["id"]] for m in after_clf]

    from sklearn.metrics import classification_report as cls_report

    print(cls_report(kw_truth, kw_preds, target_names=["noise", "actionable"]))

    # ── Show actionable items discarded by heuristic ──────────────────────
    discarded_actionable = [
        m for m in after_clf
        if id_to_truth[m["id"]] == 1 and m["id"] not in kw_kept_ids
    ]
    if discarded_actionable:
        print(f"\n{'=' * W}")
        print(f"[ Actionable messages DROPPED by heuristic ({len(discarded_actionable)}) ]")
        print(f"{'=' * W}\n")

        discarded_actionable.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        for m in discarded_actionable[:20]:
            plat = m.get("platform", "?")
            author = m.get("author", "?")[:30]
            ts = m.get("timestamp", "")[:16]
            content = m.get("content", "")[:100].replace("\n", " ")
            print(f"  [{plat}] {ts} @{author}")
            print(f"    {DIM}{content}{RESET}")
            print()

        if len(discarded_actionable) > 20:
            print(f"  ... and {len(discarded_actionable) - 20} more.\n")

    # ── Save eval samples for manual review ───────────────────────────────
    if save_eval:
        _save_eval_samples(
            save_eval, eval_msgs, id_to_truth,
            clf_kept_ids, kw_kept_ids,
            after_read, after_clf, after_kw,
        )

    print()


def _save_eval_samples(
    save_dir: str,
    eval_msgs: list[dict],
    id_to_truth: dict,
    clf_kept_ids: set,
    kw_kept_ids: set,
    after_read: list[dict],
    after_clf: list[dict],
    after_kw: list[dict],
) -> None:
    """Save eval samples to directory for manual review."""
    eval_dir = Path(save_dir)
    eval_dir.mkdir(parents=True, exist_ok=True)

    def _sample_with_labels(msgs, label_map, n=500):
        items = []
        for m in msgs:
            mid = m["id"]
            items.append({
                "id": mid,
                "platform": m.get("platform", "?"),
                "author": m.get("author", "?"),
                "timestamp": m.get("timestamp", ""),
                "channel": m.get("channel", ""),
                "content": m.get("content", "")[:500],
                "llm_label": label_map.get(mid, -1),
                "llm_label_str": "actionable" if label_map.get(mid) == 1 else "noise",
                "classifier_pred": 1 if mid in clf_kept_ids else 0,
                "heuristic_kept": mid in kw_kept_ids,
                "manual_label": None,
            })
        if len(items) > n:
            random.seed(42)
            items = random.sample(items, n)
        return items

    files = {
        "01_all_messages.json": eval_msgs,
        "02_after_classifier_kept.json": after_clf,
        "03_after_classifier_dropped.json": [m for m in after_read if m["id"] not in clf_kept_ids],
        "04_after_heuristic_kept.json": after_kw,
        "05_after_heuristic_dropped.json": [m for m in after_clf if m["id"] not in kw_kept_ids],
    }

    for filename, msgs in files.items():
        samples = _sample_with_labels(msgs, id_to_truth, n=500)
        path = eval_dir / filename
        path.write_text(
            json.dumps(samples, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  Saved {len(samples)} samples -> {path}")

    print(f"\nReview files in {eval_dir}/")
    print("Set 'manual_label' to 1 (actionable) or 0 (noise) for each message.")
    print("Then retrain: pixi run event-harvester --load-labels <corrected-file>\n")
