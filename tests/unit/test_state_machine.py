"""Static checks on the ASL plus simulated executions of the real definition."""

import json
import re

import pytest
from conftest import ROOT, load_cfn
from support.asl import Simulator

import acme_common.llm.errors as llm_errors

DEFINITION = json.loads((ROOT / "statemachine" / "triage.asl.json").read_text())
DDB = "arn:aws:states:::dynamodb:updateItem"
TICKET = {"subject": "Sync stuck", "body": "SYNC-409 on Windows", "customer_tier": "pro"}
INPUT = {"ticket_id": "t-1", "ticket": TICKET, "received_at": "2026-11-01T00:00:00Z"}


# --------------------------------------------------------------------------- static checks
def _walk(machine):
    for name, state in machine["States"].items():
        yield name, state, machine
        if state["Type"] == "Map":
            yield from _walk(state["ItemProcessor"])


def test_all_transitions_point_at_existing_states():
    for name, state, machine in _walk(DEFINITION):
        targets = [state.get("Next"), state.get("Default")]
        targets += [c["Next"] for c in state.get("Choices", [])]
        targets += [c["Next"] for c in state.get("Catch", [])]
        for target in filter(None, targets):
            assert target in machine["States"], f"{name} -> {target} does not exist"


def test_every_task_retries_and_every_top_level_task_catches():
    for name, state, machine in _walk(DEFINITION):
        if state["Type"] != "Task":
            continue
        assert state.get("Retry"), f"{name} has no Retry"
        for retrier in state["Retry"]:
            if retrier["MaxAttempts"] > 1:
                assert retrier["BackoffRate"] >= 2.0, f"{name}: backoff is not exponential"
        if machine is DEFINITION:
            assert any("States.ALL" in c["ErrorEquals"] for c in state.get("Catch", [])), name
    assert DEFINITION["States"]["RunSpecialists"]["Catch"][0]["Next"] == "MarkFailed"


def test_retry_error_names_match_python_exception_classes():
    names = {
        e
        for _, s, _ in _walk(DEFINITION)
        for r in s.get("Retry", [])
        for e in r["ErrorEquals"]
        if e.startswith("LLM")
    }
    assert names == {"LLMThrottledError", "LLMUnavailableError", "LLMResponseError"}
    for name in names:
        assert issubclass(getattr(llm_errors, name), llm_errors.LLMError)


def test_definition_substitutions_match_the_template():
    used = set(re.findall(r"\$\{(\w+)\}", json.dumps(DEFINITION)))
    sm = load_cfn("stacks/statemachine.yaml")["Resources"]["TriageStateMachine"]["Properties"]
    assert used == set(sm["DefinitionSubstitutions"])


def test_loop_limit_is_one_revision():
    loop = DEFINITION["States"]["Initialize"]["Parameters"]["review_loop"]
    assert loop["max_revisions"] == 1


# --------------------------------------------------------------------------- simulation
class Stub:
    """In-memory stand-ins for the six Lambdas and the DynamoDB integration."""

    def __init__(self, plan_steps=("classify", "extract", "draft_reply"), verdicts=("approve",)):
        self.plan_steps = list(plan_steps)
        self.verdicts = list(verdicts)
        self.ddb_updates = []
        self.persisted = None
        self.calls = []
        self.fail = {}  # function -> list of exceptions to raise first

    def _maybe_fail(self, fn):
        errors = self.fail.get(fn) or []
        if errors:
            raise errors.pop(0)

    def handlers(self):
        def fn(name, impl):
            def call(event):
                self.calls.append(name)
                self._maybe_fail(name)
                return impl(event)

            return call

        return {
            "${PlannerFunctionArn}": fn(
                "planner",
                lambda e: {
                    "steps": self.plan_steps,
                    "reason": "r",
                    "requires_review": "draft_reply" in self.plan_steps,
                    "rejected_steps": [],
                    "usage": {"agent": "planner", "input_tokens": 10, "output_tokens": 2},
                },
            ),
            "${ClassifierFunctionArn}": fn(
                "classifier", lambda e: {"step": "classify", "output": {}, "usage": {}}
            ),
            "${ExtractorFunctionArn}": fn(
                "extractor", lambda e: {"step": "extract", "output": {}, "usage": {}}
            ),
            "${ResponderFunctionArn}": fn(
                "responder",
                lambda e: {
                    "step": "draft_reply",
                    "revision": e.get("revision", 0),
                    "output": {"reply": f"draft v{e.get('revision', 0)}"},
                    "usage": {},
                },
            ),
            "${ReviewerFunctionArn}": fn(
                "reviewer",
                lambda e: {
                    "verdict": self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0],
                    "issues": [],
                    "feedback": "fix it",
                    "revision": e["revision"],
                    "usage": {},
                },
            ),
            "${PersistFunctionArn}": fn("persist", self._persist),
            DDB: self._ddb,
        }

    def _persist(self, state):
        self.persisted = state
        return {"ticket_id": state["ticket_id"], "status": state["final_status"]}

    def _ddb(self, params):
        self.ddb_updates.append(params["ExpressionAttributeValues"][":status"]["S"])
        assert params["Key"]["pk"]["S"] == "TICKET#t-1"
        return {}


def run(stub):
    return Simulator(DEFINITION, stub.handlers()).run(INPUT)


def test_happy_path_approved_first_time():
    stub = Stub(verdicts=["approve"])
    ex = run(stub)

    assert ex.status == "SUCCEEDED"
    assert ex.output == {"ticket_id": "t-1", "status": "COMPLETED"}
    assert stub.ddb_updates == ["PROCESSING"]
    assert ex.visited("Review") == 1 and ex.visited("Redraft") == 0
    assert [r["step"] for r in stub.persisted["results"]] == ["classify", "extract", "draft_reply"]


def test_one_revision_then_approve_completes():
    stub = Stub(verdicts=["revise", "approve"])
    ex = run(stub)

    assert ex.output["status"] == "COMPLETED"
    assert ex.visited("Redraft") == 1
    assert ex.visited("Review") == 2
    loop = stub.persisted["review_loop"]
    assert loop["revision"] == 1
    assert loop["previous_review"]["verdict"] == "revise"
    assert loop["redraft"]["output"]["reply"] == "draft v1"


def test_reviewer_loop_is_capped_at_one_revision_then_needs_human():
    stub = Stub(verdicts=["revise"])  # reviewer never satisfied
    ex = run(stub)

    assert ex.output["status"] == "NEEDS_HUMAN"
    assert ex.visited("Redraft") == 1, "must loop back to the Responder at most once"
    assert ex.visited("Review") == 2
    assert stub.calls.count("responder") == 2  # first draft in the Map + one redraft


def test_escalation_skips_the_redraft():
    stub = Stub(verdicts=["escalate"])
    ex = run(stub)
    assert ex.output["status"] == "NEEDS_HUMAN"
    assert ex.visited("Redraft") == 0


def test_classify_only_plan_skips_review():
    stub = Stub(plan_steps=["classify"])
    ex = run(stub)
    assert ex.output["status"] == "COMPLETED"
    assert ex.visited("Review") == 0
    assert stub.calls == ["planner", "classifier", "persist"]


def test_throttling_is_retried():
    stub = Stub()
    stub.fail["classifier"] = [llm_errors.LLMThrottledError("429"), llm_errors.LLMThrottledError()]
    ex = run(stub)
    assert ex.output["status"] == "COMPLETED"
    assert stub.calls.count("classifier") == 3


def test_retries_exhausted_marks_failed():
    stub = Stub()
    stub.fail["planner"] = [llm_errors.LLMUnavailableError("503")] * 10
    ex = run(stub)

    assert ex.status == "FAILED"
    assert ex.error == "LLMUnavailableError"
    assert stub.ddb_updates == ["PROCESSING", "FAILED"]
    assert stub.calls.count("planner") == 6  # 1 + 5 retries


@pytest.mark.parametrize("exc", [llm_errors.LLMRequestError("401"), KeyError("ticket")])
def test_non_retryable_errors_fail_fast(exc):
    stub = Stub()
    stub.fail["extractor"] = [exc]
    ex = run(stub)
    assert ex.status == "FAILED"
    assert stub.calls.count("extractor") == 1
    assert stub.ddb_updates[-1] == "FAILED"
    assert "persist" not in stub.calls


def test_unknown_step_in_map_fails_closed():
    stub = Stub(plan_steps=["classify", "delete_account"])
    ex = run(stub)
    assert ex.status == "FAILED"
    assert ex.error == "PlanValidationError"
    assert stub.ddb_updates[-1] == "FAILED"
