"""Recruiter email grading — score Gmail recruiter messages 0-100."""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from event_harvester.config import LLMConfig

logger = logging.getLogger("event_harvester.recruiter_score")


@dataclass
class RecruiterGrade:
    """Result of grading a single recruiter email."""

    score: int  # 0-100, clamped
    reasons: list[str]
    action: str  # "trash" | "ignore" | "review" | "respond"
    message_id: str
    subject: str
    sender: str


# ── Staffing firm domains (body shops / mass-blast recruiting) ──────────

_STAFFING_FIRM_DOMAINS = {
    "q1tech.com",
    "tekrecruiter.com",
    "cyient.com",
    "infosys.com",
    "wipro.com",
    "tcs.com",
    "hcltech.com",
    "cognizant.com",
    "mindtree.com",
    "ltimindtree.com",
    "mphasis.com",
    "hexaware.com",
    "birlasoft.com",
    "zensar.com",
    "mastech.com",
    "mastechdigital.com",
    "syntel.com",
    "niit-tech.com",
    "coforge.com",
    "sonata-software.com",
    "persistent.com",
    "techmahindra.com",
    "collabera.com",
    "trigent.com",
    "vdart.com",
    "idctechnologies.com",
    "appirio.com",
    "sievert-larsen.com",
    "nttdata.com",
    "ust.com",
    "happiest-minds.com",
    "l&tcorp.com",
    "accolite.com",
    "staffaugmentation.com",
    "apexsystems.com",
    "insightglobal.com",
    "teksystems.com",
    "randstadusa.com",
    "kforce.com",
    "roberthalf.com",
}

# ── Pattern lists ───────────────────────────────────────────────────────

_TEMPLATE_OPENERS = [
    "hope you are well",
    "hope this finds you well",
    "hope you are doing well",
    "i came across your profile",
    "i found your resume",
    "i have an exciting opportunity",
    "i have a great opportunity",
    "we have a requirement",
    "please find the job description below",
    "please review the below job description",
    "kindly share your updated resume",
    "please share your updated cv",
    "please go through the below requirement",
]

_BODY_SHOP_FORMAT_RE = re.compile(
    r"(?i)job\s*title\s*[:=].*(?://|location\s*[:=])", re.DOTALL
)
_DURATION_LINE_RE = re.compile(
    r"(?i)duration\s*[:=]\s*\d+\s*(?:months?|weeks?|yrs?|years?)"
)
_MASS_BLAST_SUBJECT_RE = re.compile(
    r"(?i)(?:urgent\s+requirement|hot\s+job|immediate\s+(?:opening|need|hire)"
    r"|multiple\s+positions|multiple\s+openings)"
)
_CONTRACT_RE = re.compile(
    r"(?i)\b(?:C2C|corp[\s-]?to[\s-]?corp|W2\s+only|W2/C2C|contract\s+position)\b"
)
_FULLTIME_RE = re.compile(r"(?i)\b(?:full[\s-]?time|FTE|permanent)\b")

_MEETING_PHRASES = [
    "let's chat",
    "lets chat",
    "grab coffee",
    "hop on a call",
    "jump on a call",
    "quick call",
    "would love to meet",
    "would love to chat",
    "let's set up a time",
    "schedule a call",
    "schedule a conversation",
    "book a time",
    "pick a time",
    "select a time on my calendar",
    "select a time",
]

_CALENDAR_DOMAINS = [
    "calendly.com",
    "cal.com",
    "savvycal.com",
    "chili.com",
    "doodle.com",
    "youcanbook.me",
    "meetingbird.com",
    "x.ai",
    "reclaim.ai",
]

_QUALITY_COMPANIES = {
    "openai",
    "anthropic",
    "google",
    "deepmind",
    "meta",
    "apple",
    "microsoft",
    "stripe",
    "scale ai",
    "databricks",
    "snowflake",
    "figma",
    "vercel",
    "supabase",
    "anyscale",
    "modal",
    "hugging face",
    "huggingface",
    "cohere",
    "mistral",
    "tesla",
    "spacex",
    "nvidia",
    "amd",
    "intel",
    "netflix",
    "spotify",
    "airbnb",
    "uber",
    "lyft",
    "doordash",
    "instacart",
    "rippling",
    "ramp",
    "brex",
    "plaid",
    "notion",
    "linear",
    "retool",
}

_PERSONALIZATION_RE = re.compile(
    r"(?i)(?:your\s+github|your\s+project|your\s+work\s+on|your\s+background\s+in"
    r"|saw\s+your|noticed\s+your|your\s+experience\s+(?:with|at|in))"
)

_SF_BAY_RE = re.compile(
    r"(?i)\b(?:san\s+francisco|SF|bay\s+area|palo\s+alto|mountain\s+view"
    r"|sunnyvale|cupertino|menlo\s+park|redwood\s+city|oakland|berkeley)\b"
)
_ONSITE_RE = re.compile(r"(?i)\b(?:hybrid|on[\s-]?site|in[\s-]?person|in[\s-]?office)\b")

_SALARY_RE = re.compile(
    r"(?:\$\s*\d{2,3}\s*[kK]|\$\s*\d{3},\d{3}|\bequity\b|\bRSU\b|\bstock\s+options\b)",
)


# ── Scoring functions ───────────────────────────────────────────────────


def _extract_domain(sender: str) -> str:
    """Pull domain from a From header like 'Name <email@domain.com>'."""
    match = re.search(r"@([\w.-]+)", sender)
    return match.group(1).lower() if match else ""


def _score_sender(sender: str) -> tuple[int, list[str]]:
    delta = 0
    reasons: list[str] = []
    domain = _extract_domain(sender)

    if domain in _STAFFING_FIRM_DOMAINS:
        delta -= 25
        reasons.append(f"Known staffing firm domain: {domain}")

    return delta, reasons


def _score_subject(subject: str) -> tuple[int, list[str]]:
    delta = 0
    reasons: list[str] = []

    if _MASS_BLAST_SUBJECT_RE.search(subject):
        delta -= 5
        reasons.append("Mass-blast subject pattern")

    # Check for contract signals in subject
    if _CONTRACT_RE.search(subject) and not _FULLTIME_RE.search(subject):
        delta -= 5
        reasons.append("Contract-only in subject")

    return delta, reasons


def _score_body(body: str) -> tuple[int, list[str]]:
    delta = 0
    reasons: list[str] = []
    body_lower = body.lower()

    # Negative: body shop format
    if _BODY_SHOP_FORMAT_RE.search(body):
        delta -= 20
        reasons.append("Body shop email format (Job Title // Location)")
    elif _DURATION_LINE_RE.search(body):
        delta -= 10
        reasons.append("Contract duration line detected")

    # Negative: template openers
    for phrase in _TEMPLATE_OPENERS:
        if phrase in body_lower:
            delta -= 10
            reasons.append(f"Generic template: '{phrase}'")
            break

    # Negative: contract without full-time
    if _CONTRACT_RE.search(body) and not _FULLTIME_RE.search(body):
        delta -= 8
        reasons.append("Contract/C2C only, no full-time mention")

    # Positive: meeting/call request
    for phrase in _MEETING_PHRASES:
        if phrase in body_lower:
            delta += 15
            reasons.append(f"Direct meeting request: '{phrase}'")
            break

    # Positive: calendar link
    for domain in _CALENDAR_DOMAINS:
        if domain in body_lower:
            delta += 15
            reasons.append(f"Calendar scheduling link ({domain})")
            break

    # Positive: personalization
    if _PERSONALIZATION_RE.search(body):
        delta += 10
        reasons.append("Personalized content (references your work)")

    # Positive: quality company mentioned
    for company in _QUALITY_COMPANIES:
        if company in body_lower:
            delta += 10
            reasons.append(f"Quality company mentioned: {company}")
            break

    # Positive: full-time
    if _FULLTIME_RE.search(body):
        delta += 8
        reasons.append("Full-time position")

    # Positive: SF Bay Area + onsite/hybrid
    if _SF_BAY_RE.search(body) and _ONSITE_RE.search(body):
        delta += 5
        reasons.append("SF/Bay Area hybrid or on-site role")

    # Positive: salary/comp mentioned
    if _SALARY_RE.search(body):
        delta += 5
        reasons.append("Salary or equity mentioned")

    return delta, reasons


def _action_for_score(score: int) -> str:
    if score <= 20:
        return "trash"
    if score <= 45:
        return "ignore"
    if score <= 65:
        return "review"
    return "respond"


# ── Public API ──────────────────────────────────────────────────────────


def grade_email(message: dict, body: str = "") -> RecruiterGrade:
    """Grade a single recruiter email using local heuristics.

    Args:
        message: Standard message dict with platform, id, timestamp, author, content.
        body: Full email body text (uses content/snippet if empty).

    Returns:
        RecruiterGrade with score 0-100 and breakdown.
    """
    sender = message.get("author", "")
    subject = message.get("content", "").split("\n", 1)[0]  # first line = subject
    text = body or message.get("content", "")

    base_score = 50
    all_reasons: list[str] = []

    for scorer in (_score_sender, _score_subject):
        args = (sender,) if scorer is _score_sender else (subject,)
        delta, reasons = scorer(*args)
        base_score += delta
        all_reasons.extend(reasons)

    body_delta, body_reasons = _score_body(text)
    base_score += body_delta
    all_reasons.extend(body_reasons)

    # Clamp to 0-100
    final_score = max(0, min(100, base_score))
    action = _action_for_score(final_score)

    return RecruiterGrade(
        score=final_score,
        reasons=all_reasons,
        action=action,
        message_id=message.get("id", ""),
        subject=subject[:80],
        sender=sender[:80],
    )


def grade_emails_batch(
    messages: list[dict],
    bodies: dict[str, str] | None = None,
    llm_cfg: Optional[LLMConfig] = None,
    llm_threshold: tuple[int, int] = (30, 60),
) -> list[RecruiterGrade]:
    """Grade a batch of emails. Optionally use LLM for borderline cases.

    Args:
        messages: List of Gmail message dicts.
        bodies: Optional dict mapping message_id -> full body text.
        llm_cfg: If provided, use LLM for borderline scores.
        llm_threshold: (low, high) — scores between these go to LLM.

    Returns:
        List of RecruiterGrade, sorted by score descending.
    """
    if bodies is None:
        bodies = {}

    grades = []
    for msg in messages:
        body = bodies.get(msg.get("id", ""), "")
        grade = grade_email(msg, body)
        grades.append(grade)

    # Optional LLM refinement for borderline cases
    if llm_cfg and llm_cfg.is_configured:
        low, high = llm_threshold
        borderline = [g for g in grades if low <= g.score <= high]
        if borderline:
            _llm_refine_borderline(borderline, messages, bodies, llm_cfg)

    grades.sort(key=lambda g: g.score, reverse=True)
    return grades


def _llm_refine_borderline(
    grades: list[RecruiterGrade],
    messages: list[dict],
    bodies: dict[str, str],
    cfg: LLMConfig,
) -> None:
    """Use LLM to refine scores for borderline recruiter emails (in-place)."""
    from event_harvester.llm import chat_completion

    msg_map = {m["id"]: m for m in messages}
    items = []
    for g in grades:
        msg = msg_map.get(g.message_id, {})
        body = bodies.get(g.message_id, "")
        items.append({
            "id": g.message_id,
            "subject": g.subject,
            "sender": g.sender,
            "body_preview": (body or msg.get("content", ""))[:500],
            "current_score": g.score,
        })

    prompt = (
        "You are grading recruiter emails for a job-seeking AI engineer in San Francisco. "
        "For each email, decide if it's worth responding to.\n\n"
        "Score 0-100: high = worth responding, low = spam/body-shop.\n"
        "Positive: direct meeting requests, calendar links, named company hiring, "
        "personalized content, full-time SF roles.\n"
        "Negative: mass-blast templates, staffing firms, contract-only, generic.\n\n"
        f"Emails:\n```json\n{json.dumps(items, indent=2)}\n```\n\n"
        "Output in INI format:\n\n"
        "[Grade.message_id_here]\n"
        "score = 72\n"
        "reason = Direct meeting request, personalized\n\n"
        "[Grade.another_id]\n"
        "score = 18\n"
        "reason = Body shop template\n\n"
        "Use the actual message IDs from the emails above."
    )

    try:
        raw = chat_completion(
            messages=[{"role": "user", "content": prompt}],
            cfg=cfg,
            max_tokens=2048,
        )
        from event_harvester.analysis import _parse_llm_ini
        sections = _parse_llm_ini(raw)
        llm_grades = {}
        for section_name, fields in sections.items():
            if not section_name.lower().startswith("grade"):
                continue
            # Extract ID from "Grade.msg_123"
            msg_id = section_name.split(".", 1)[-1] if "." in section_name else ""
            try:
                score = int(fields.get("score", "50"))
                reason = fields.get("reason", "")
                llm_grades[msg_id] = {"score": score, "reason": reason}
            except ValueError:
                pass

        for grade in grades:
            if grade.message_id in llm_grades:
                llm_data = llm_grades[grade.message_id]
                grade.score = max(0, min(100, llm_data.get("score", grade.score)))
                grade.action = _action_for_score(grade.score)
                if llm_data.get("reason"):
                    grade.reasons.append(f"LLM: {llm_data['reason']}")

        logger.info("LLM refined %d borderline recruiter grades.", len(llm_grades))
    except Exception as e:
        logger.warning("LLM recruiter refinement failed: %s", e)
