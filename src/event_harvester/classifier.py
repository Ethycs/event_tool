"""Binary classifiers for pre-filtering messages into actionable vs noise.

Two classifiers:
  - **email**: trained on Gmail features (unsubscribe, html density, noreply senders, etc.)
  - **chat**: trained on Discord + Telegram features (mentions, message length, thread activity, etc.)

Each runs locally with zero API calls after training.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger("event_harvester.classifier")

_CLASSIFIERS_DIR = Path(__file__).resolve().parent.parent.parent / "classifiers"
EMAIL_MODEL_PATH = _CLASSIFIERS_DIR / "classifier_email.pkl"
CHAT_MODEL_PATH = _CLASSIFIERS_DIR / "classifier_chat.pkl"
_LEGACY_MODEL_PATH = _CLASSIFIERS_DIR / "classifier.pkl"

# ── Shared keywords ──────────────────────────────────────────────────────

_SCHEDULING_KEYWORDS = [
    "meeting", "schedule", "rsvp", "deadline", "call", "interview",
    "zoom", "teams", "webex", "calendar", "appointment", "standup",
    "sync", "1:1", "one-on-one", "sprint", "demo", "retro",
    "check-in", "check in", "huddle", "office hours",
]

_QUESTION_PHRASES = [
    "could you", "can you", "please", "let me know", "would you",
    "are you", "do you", "will you", "have you", "did you",
]

_NOTIFICATION_SENDERS = [
    "notifications@", "alerts@", "digest@", "noreply@", "no-reply@",
    "mailer-daemon@", "postmaster@", "info@", "updates@", "news@",
    "marketing@", "support@", "donotreply@", "do-not-reply@",
]

_URL_PATTERN = re.compile(r"https?://\S+")


# ── Feature extraction ───────────────────────────────────────────────────

def extract_email_features(message: dict) -> dict[str, float]:
    """Features tuned for Gmail messages."""
    content = message.get("content", "")
    content_lower = content.lower()
    author = message.get("author", "").lower()
    length = len(content)

    return {
        "length": float(length),
        "word_count": float(len(content.split())),
        "num_links": float(len(_URL_PATTERN.findall(content))),
        "num_questions": float(content.count("?")),
        "has_unsubscribe": 1.0 if "unsubscribe" in content_lower else 0.0,
        "is_reply": 1.0 if content_lower.startswith("re:") else 0.0,
        "has_scheduling": 1.0 if any(kw in content_lower for kw in _SCHEDULING_KEYWORDS) else 0.0,
        "has_question_words": 1.0 if any(p in content_lower for p in _QUESTION_PHRASES) else 0.0,
        "is_noreply_sender": 1.0 if ("noreply" in author or "no-reply" in author) else 0.0,
        "is_notification": 1.0 if any(pat in author for pat in _NOTIFICATION_SENDERS) else 0.0,
        "sender_is_self": 1.0 if message.get("is_sent") else 0.0,
        "html_density": (
            (content.count("&#") + content.count("<") + content.count(">")) / length
            if length > 0
            else 0.0
        ),
    }


def extract_chat_features(message: dict) -> dict[str, float]:
    """Features tuned for Discord + Telegram messages."""
    content = message.get("content", "")
    content_lower = content.lower()
    length = len(content)
    words = content.split()

    return {
        "length": float(length),
        "word_count": float(len(words)),
        "num_links": float(len(_URL_PATTERN.findall(content))),
        "num_questions": float(content.count("?")),
        "has_scheduling": 1.0 if any(kw in content_lower for kw in _SCHEDULING_KEYWORDS) else 0.0,
        "has_question_words": 1.0 if any(p in content_lower for p in _QUESTION_PHRASES) else 0.0,
        "has_mention": 1.0 if "@" in content else 0.0,
        "has_everyone": 1.0 if "@everyone" in content or "@here" in content else 0.0,
        "has_code_block": 1.0 if "```" in content else 0.0,
        "is_short": 1.0 if length < 20 else 0.0,
        "is_long": 1.0 if length > 200 else 0.0,
        "is_reaction_only": 1.0 if length < 5 and not any(c.isalnum() for c in content) else 0.0,
        "has_attachment_signal": 1.0 if any(
            ext in content_lower for ext in [".png", ".jpg", ".pdf", ".zip", ".py", ".js"]
        ) else 0.0,
        "is_bot": 1.0 if message.get("author", "").lower().endswith("bot") else 0.0,
        "is_pinned": 1.0 if message.get("pinned") else 0.0,
    }


def extract_features(message: dict) -> dict[str, float]:
    """Route to the right feature extractor based on platform."""
    platform = message.get("platform", "").lower()
    if platform == "gmail":
        return extract_email_features(message)
    return extract_chat_features(message)


def _model_path_for(message: dict) -> str:
    """Return the right model path for a message's platform."""
    if message.get("platform", "").lower() == "gmail":
        return EMAIL_MODEL_PATH
    return CHAT_MODEL_PATH


# ── Training ─────────────────────────────────────────────────────────────

def _features_to_array(
    messages: list[dict],
    feature_fn,
) -> tuple[list[list[float]], list[str]]:
    """Convert messages to feature matrix using the given extractor."""
    all_feats = [feature_fn(m) for m in messages]
    if not all_feats:
        return [], []
    feature_names = list(all_feats[0].keys())
    matrix = [[f[name] for name in feature_names] for f in all_feats]
    return matrix, feature_names


def train(
    messages: list[dict],
    labels: list[int],
    model_path: str | None = None,
) -> None:
    """Train classifier(s) and save.

    If model_path is given, trains a single model for all messages.
    Otherwise, splits by platform and trains separate email/chat models.
    """
    if model_path:
        # Single model mode (backward compat)
        _train_one(messages, labels, extract_features, model_path)
        return

    # Split by platform
    email_msgs, email_labels = [], []
    chat_msgs, chat_labels = [], []

    for m, label in zip(messages, labels):
        if m.get("platform", "").lower() == "gmail":
            email_msgs.append(m)
            email_labels.append(label)
        else:
            chat_msgs.append(m)
            chat_labels.append(label)

    if email_msgs:
        print(f"\n--- Email classifier ({len(email_msgs)} samples) ---")
        _train_one(email_msgs, email_labels, extract_email_features, EMAIL_MODEL_PATH)

    if chat_msgs:
        print(f"\n--- Chat classifier ({len(chat_msgs)} samples) ---")
        _train_one(chat_msgs, chat_labels, extract_chat_features, CHAT_MODEL_PATH)

    if not email_msgs and not chat_msgs:
        raise ValueError("No messages to train on.")


def _train_one(messages, labels, feature_fn, model_path):
    """Train and save a single classifier."""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import classification_report
    import joblib

    X, feature_names = _features_to_array(messages, feature_fn)
    if not X:
        raise ValueError("No messages to train on.")

    clf = RandomForestClassifier(
        n_estimators=100, random_state=42, class_weight="balanced",
    )
    clf.fit(X, labels)

    y_pred = clf.predict(X)
    print(f"\n=== Classification Report ({model_path}) ===")
    print(classification_report(labels, y_pred, target_names=["noise", "actionable"]))

    print("=== Feature Importances ===")
    importances = sorted(
        zip(feature_names, clf.feature_importances_),
        key=lambda x: x[1],
        reverse=True,
    )
    for name, imp in importances:
        bar = "#" * int(imp * 50)
        print(f"  {name:25s} {imp:.4f}  {bar}")
    print()

    Path(model_path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": clf, "feature_names": feature_names}, model_path)
    print(f"Model saved -> {model_path}")


# ── Prediction ───────────────────────────────────────────────────────────

# Threshold for actionable classification. Lower = fewer missed actionable
# messages (higher recall) at the cost of more noise getting through.
# 0.5 = default, 0.3 = aggressive recall (prefer false positives over false negatives)
ACTIONABLE_THRESHOLD = 0.3


def predict(
    messages: list[dict],
    model_path: str | None = None,
    threshold: float = ACTIONABLE_THRESHOLD,
) -> list[bool]:
    """Predict actionable (True) vs noise (False).

    Uses predict_proba with a low threshold to favor recall — better to
    let some noise through than miss real action items.
    """
    import joblib

    if model_path:
        data = joblib.load(model_path)
        clf, feature_names = data["model"], data["feature_names"]
        all_feats = [extract_features(m) for m in messages]
        X = [[f[name] for name in feature_names] for f in all_feats]
        probs = clf.predict_proba(X)
        # Column 1 = probability of actionable (class 1)
        actionable_idx = list(clf.classes_).index(1)
        return [bool(p[actionable_idx] >= threshold) for p in probs]

    # Per-platform prediction
    results: list[bool | None] = [None] * len(messages)
    groups: dict[str, list[tuple[int, dict]]] = {"email": [], "chat": []}

    for i, m in enumerate(messages):
        key = "email" if m.get("platform", "").lower() == "gmail" else "chat"
        groups[key].append((i, m))

    for key, path, feat_fn in [
        ("email", EMAIL_MODEL_PATH, extract_email_features),
        ("chat", CHAT_MODEL_PATH, extract_chat_features),
    ]:
        items = groups[key]
        if not items:
            continue
        if not path.exists():
            # No model — default to actionable
            for i, _ in items:
                results[i] = True
            continue

        data = joblib.load(path)
        clf, feature_names = data["model"], data["feature_names"]
        feats = [feat_fn(m) for _, m in items]
        X = [[f[name] for name in feature_names] for f in feats]
        probs = clf.predict_proba(X)
        actionable_idx = list(clf.classes_).index(1)
        for (i, _), p in zip(items, probs):
            results[i] = bool(p[actionable_idx] >= threshold)

    return [r if r is not None else True for r in results]


# ── Filtering ────────────────────────────────────────────────────────────

def has_trained_models() -> bool:
    """Check if any trained classifier models exist."""
    return (
        EMAIL_MODEL_PATH.exists()
        or CHAT_MODEL_PATH.exists()
        or _LEGACY_MODEL_PATH.exists()
    )


def filter_actionable(messages: list[dict]) -> list[dict]:
    """Filter messages to only actionable ones using trained classifier(s).

    Uses the email classifier for Gmail, chat classifier for Discord/Telegram.
    If no model exists for a platform, those messages pass through unfiltered.
    """
    has_email = EMAIL_MODEL_PATH.exists()
    has_chat = CHAT_MODEL_PATH.exists()

    # Also check legacy single model
    has_legacy = _LEGACY_MODEL_PATH.exists()

    if not has_email and not has_chat and not has_legacy:
        logger.warning(
            "No classifier models found — returning all %d messages unfiltered. "
            "Run with --train-classifier to train.",
            len(messages),
        )
        return messages

    if has_legacy and not has_email and not has_chat:
        # Use legacy single model
        preds = predict(messages, model_path=_LEGACY_MODEL_PATH)
    else:
        preds = predict(messages)

    actionable = [m for m, is_act in zip(messages, preds) if is_act]

    logger.info(
        "Classifier: %d / %d messages are actionable (filtered %d noise).",
        len(actionable),
        len(messages),
        len(messages) - len(actionable),
    )
    return actionable
