"""Agent handlers driven by FakeLLM (no AWS, no network)."""

import pytest
from conftest import FakeLambdaContext, load_function

from acme_common.guardrails import check_draft, ticket_requires_human
from acme_common.llm import FakeLLM, LLMResponseError, set_llm_client

CTX = FakeLambdaContext()

TICKET = {
    "subject": "Charged twice this month",
    "body": "I was double charged for my Pro plan. Please refund the extra charge.",
    "customer_tier": "pro",
}
SAFE_DRAFT = {
    "reply": "Sorry about that - our billing team will review the duplicate charge.",
    "tone": "empathetic",
    "needs_followup": True,
}


@pytest.fixture
def llm():
    fakes = []

    def install(**script):
        fake = FakeLLM(script=script or None)
        set_llm_client(fake)
        fakes.append(fake)
        return fake

    yield install
    set_llm_client(None)


@pytest.fixture(scope="module")
def fns():
    return {n: load_function(n) for n in ("planner", "classifier", "responder", "reviewer")}


def _draft_result(draft):
    return [{"step": "draft_reply", "output": draft, "usage": {}}]


# ---------------------------------------------------------------- planner
def test_planner_drops_hallucinated_step_and_reports_usage(fns, llm):
    llm(plan={"steps": ["classify", "escalate_to_engineering", "draft_reply"], "reason": "r"})
    out = fns["planner"].lambda_handler({"ticket": TICKET, "ticket_id": "t1"}, CTX)

    assert out["steps"] == ["classify", "draft_reply"]
    assert out["rejected_steps"] == ["escalate_to_engineering"]
    assert out["requires_review"] is True
    assert out["usage"]["agent"] == "planner"
    assert out["usage"]["input_tokens"] > 0


def test_planner_heuristic_spam_is_classify_only(fns, llm):
    llm()
    spam = {"subject": "SEO services", "body": "Cheap backlinks for you", "customer_tier": "free"}
    out = fns["planner"].lambda_handler({"ticket": spam}, CTX)
    assert out["steps"] == ["classify"]
    assert out["requires_review"] is False


# ---------------------------------------------------------------- classifier
def test_classifier_clamps_confidence(fns, llm):
    llm(
        classification={
            "category": "billing",
            "priority": "p2",
            "sentiment": "negative",
            "confidence": 1.7,
            "rationale": "x",
        }
    )
    out = fns["classifier"].lambda_handler({"ticket": TICKET}, CTX)
    assert out["step"] == "classify"
    assert out["output"]["confidence"] == 1.0


def test_classifier_rejects_schema_violations(fns, llm):
    llm(classification={"category": "billing"})
    with pytest.raises(LLMResponseError):
        fns["classifier"].lambda_handler({"ticket": TICKET}, CTX)


# ---------------------------------------------------------------- responder
def test_responder_redraft_includes_feedback_and_context(fns, llm):
    fake = llm(draft_reply=SAFE_DRAFT)
    results = [
        {"step": "classify", "output": {"category": "billing"}, "usage": {}},
        *_draft_result(
            {"reply": "We will refund you in full.", "tone": "upbeat", "needs_followup": False}
        ),
    ]
    out = fns["responder"].lambda_handler(
        {"ticket": TICKET, "results": results, "feedback": "No refund promises.", "revision": 1},
        CTX,
    )
    prompt = fake.calls[0]["user"]
    assert "<reviewer_feedback>" in prompt
    assert "<previous_draft>" in prompt
    assert '"category": "billing"' in prompt
    assert out["revision"] == 1
    assert out["output"] == SAFE_DRAFT


# ---------------------------------------------------------------- reviewer
def test_reviewer_approves_clean_draft(fns, llm):
    llm(review={"verdict": "approve", "issues": [], "feedback": "ok"})
    out = fns["reviewer"].lambda_handler(
        {"ticket": TICKET, "results": _draft_result(SAFE_DRAFT), "redraft": None}, CTX
    )
    assert out["verdict"] == "approve"
    assert out["usage"]["agent"] == "reviewer"


def test_reviewer_rules_override_an_approving_llm(fns, llm):
    """The LLM says approve, but the deterministic rule catches the refund promise."""
    llm(review={"verdict": "approve", "issues": [], "feedback": "ok"})
    risky = {**SAFE_DRAFT, "reply": "We'll issue a full refund and it will be fixed by tomorrow."}
    out = fns["reviewer"].lambda_handler({"ticket": TICKET, "results": _draft_result(risky)}, CTX)
    assert out["verdict"] == "revise"
    assert {i["type"] for i in out["issues"]} == {"refund_or_credit", "timeline_commitment"}


def test_reviewer_merges_llm_issues(fns, llm):
    llm(
        review={
            "verdict": "revise",
            "issues": [{"type": "hallucinated_feature", "detail": "No offline AI mode exists."}],
            "feedback": "Remove the invented feature.",
        }
    )
    out = fns["reviewer"].lambda_handler(
        {"ticket": TICKET, "results": _draft_result(SAFE_DRAFT)}, CTX
    )
    assert out["verdict"] == "revise"
    assert out["issues"][0]["source"] == "llm"


def test_reviewer_prefers_redraft_over_original(fns, llm):
    fake = llm(review={"verdict": "approve", "issues": [], "feedback": "ok"})
    risky = {**SAFE_DRAFT, "reply": "We guarantee a refund in full."}
    redraft = {"step": "draft_reply", "output": SAFE_DRAFT, "usage": {}}
    out = fns["reviewer"].lambda_handler(
        {"ticket": TICKET, "results": _draft_result(risky), "redraft": redraft, "revision": 1}, CTX
    )
    assert out["verdict"] == "approve"
    assert out["revision"] == 1
    assert SAFE_DRAFT["reply"] in fake.calls[0]["user"]


def test_reviewer_escalates_legal_threats_without_calling_llm(fns, llm):
    fake = llm()
    legal = {**TICKET, "body": "Refund me or my lawyer will be in touch."}
    out = fns["reviewer"].lambda_handler(
        {"ticket": legal, "results": _draft_result(SAFE_DRAFT)}, CTX
    )
    assert out["verdict"] == "escalate"
    assert fake.calls == []


# ---------------------------------------------------------------- guardrail rules
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We will refund you in full today.", {"refund_or_credit"}),
        ("I'll give you a credit for the inconvenience.", {"refund_or_credit"}),
        ("This will be fixed within 2 days.", {"timeline_commitment"}),
        ("We guarantee your notes are safe.", {"unsupported_promise"}),
        ("Please send us your password so we can check.", {"credential_request"}),
        ("Our billing team will review the charge and follow up.", set()),
    ],
)
def test_guardrail_rules(text, expected):
    assert {i["type"] for i in check_draft(text)} == expected


def test_ticket_escalation_rule():
    assert ticket_requires_human({"subject": "", "body": "I will take legal action"})
    assert ticket_requires_human({"subject": "Sync", "body": "Sync is slow"}) is None
