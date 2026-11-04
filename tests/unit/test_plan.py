import pytest

from acme_common.plan import ALLOWED_STEPS, PlanValidationError, validate_plan


def test_valid_plan_passes_through_in_canonical_order():
    plan = validate_plan({"steps": ["draft_reply", "extract", "classify"], "reason": "full triage"})
    assert plan.steps == ["classify", "extract", "draft_reply"]
    assert plan.requires_review is True
    assert plan.rejected_steps == []


def test_steps_outside_allow_list_are_dropped_and_recorded():
    plan = validate_plan(
        {"steps": ["classify", "escalate_to_engineering", "rm -rf /", "draft_reply"], "reason": ""}
    )
    assert plan.steps == ["classify", "draft_reply"]
    assert plan.rejected_steps == ["escalate_to_engineering", "rm -rf /"]
    assert set(plan.steps) <= set(ALLOWED_STEPS)


def test_classify_is_always_added():
    plan = validate_plan({"steps": ["extract"], "reason": "x"})
    assert plan.steps == ["classify", "extract"]


def test_empty_plan_becomes_classify_only_without_review():
    plan = validate_plan({"steps": [], "reason": "spam"})
    assert plan.steps == ["classify"]
    assert plan.requires_review is False


def test_requires_review_is_derived_not_trusted():
    plan = validate_plan({"steps": ["draft_reply"], "reason": "", "requires_review": False})
    assert plan.requires_review is True


def test_duplicates_case_and_whitespace_are_normalised():
    plan = validate_plan({"steps": [" Classify ", "CLASSIFY", "extract", 42], "reason": "r"})
    assert plan.steps == ["classify", "extract"]
    assert plan.rejected_steps == ["42"]


def test_reason_is_truncated():
    assert len(validate_plan({"steps": [], "reason": "x" * 5000}).reason) == 500


@pytest.mark.parametrize("raw", [None, [], {"steps": "classify"}, {"reason": "no steps"}])
def test_structurally_invalid_plans_raise(raw):
    with pytest.raises(PlanValidationError):
        validate_plan(raw)
