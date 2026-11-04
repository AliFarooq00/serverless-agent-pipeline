"""A small Amazon States Language interpreter for tests.

Covers exactly the subset statemachine/triage.asl.json uses (JSONPath query language):
Pass, Task, Choice, Map (inline), Succeed, Fail; Parameters/ItemSelector with `.$` keys;
InputPath/ResultPath/OutputPath; Retry (no sleeping) and Catch; the context object; and the
intrinsics States.Format and States.MathAdd.

It lets us unit-test the *workflow* - routing, the reviewer loop limit, failure handling - with
real Lambda handlers in-process, without deploying anything.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

Handler = Callable[[Any], Any]
_MISSING = object()


class StatesError(Exception):
    def __init__(self, error: str, cause: str = ""):
        super().__init__(f"{error}: {cause}")
        self.error = error
        self.cause = cause


@dataclass
class Execution:
    status: str
    output: Any
    history: list[str] = field(default_factory=list)
    error: str | None = None
    cause: str | None = None

    def visited(self, state: str) -> int:
        return self.history.count(state)


# --------------------------------------------------------------------------- paths
def _get(data: Any, path: str) -> Any:
    if path == "$":
        return data
    if not path.startswith("$."):
        raise StatesError("States.Runtime", f"unsupported path {path}")
    node = data
    for part in path[2:].split("."):
        if not isinstance(node, dict) or part not in node:
            raise StatesError("States.Runtime", f"path {path} not found")
        node = node[part]
    return node


def _set(data: Any, path: str | None, value: Any) -> Any:
    if path is None:
        return data
    if path == "$":
        return value
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    node = out
    parts = path[2:].split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    return out


def _split_args(arg_str: str) -> list[str]:
    args, depth, quote, cur = [], 0, False, ""
    for ch in arg_str:
        if ch == "'" and depth == 0:
            quote = not quote
        if ch == "," and not quote and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0) if not quote else 0
        cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def _resolve(expr: str, data: Any, ctx: dict[str, Any]) -> Any:
    if expr.startswith("$$."):
        return _get(ctx, "$." + expr[3:])
    if expr.startswith("$"):
        return _get(data, expr)
    m = re.fullmatch(r"States\.(\w+)\((.*)\)", expr, re.DOTALL)
    if not m:
        raise StatesError("States.Runtime", f"cannot evaluate {expr}")
    fn, raw_args = m.groups()
    args = [
        a[1:-1]
        if a.startswith("'")
        else (_resolve(a, data, ctx) if a.startswith("$") else json.loads(a))
        for a in _split_args(raw_args)
    ]
    if fn == "Format":
        template, *values = args
        for v in values:
            template = template.replace("{}", str(v), 1)
        return template
    if fn == "MathAdd":
        return args[0] + args[1]
    raise StatesError("States.Runtime", f"intrinsic States.{fn} not supported by the simulator")


def _payload(template: Any, data: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        out = {}
        for key, value in template.items():
            if key.endswith(".$"):
                out[key[:-2]] = _resolve(value, data, ctx)
            else:
                out[key] = _payload(value, data, ctx)
        return out
    if isinstance(template, list):
        return [_payload(v, data, ctx) for v in template]
    return template


# --------------------------------------------------------------------------- choice
_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "StringEquals": lambda a, b: a == b,
    "BooleanEquals": lambda a, b: a is b,
    "NumericEquals": lambda a, b: a == b,
    "NumericLessThan": lambda a, b: a < b,
    "NumericGreaterThanEquals": lambda a, b: a >= b,
}


def _rule_matches(rule: dict[str, Any], data: Any) -> bool:
    if "And" in rule:
        return all(_rule_matches(r, data) for r in rule["And"])
    if "Or" in rule:
        return any(_rule_matches(r, data) for r in rule["Or"])
    if "Not" in rule:
        return not _rule_matches(rule["Not"], data)
    try:
        value = _get(data, rule["Variable"])
    except StatesError:
        return False
    for op, expected in rule.items():
        if op in ("Variable", "Next"):
            continue
        if op.endswith("Path"):
            expected = _get(data, expected)
            op = op[: -len("Path")]
        compare = _OPERATORS.get(op)
        if compare is None:
            raise StatesError("States.Runtime", f"choice operator {op} not supported")
        return compare(value, expected)
    return False


def _matches(error: str, equals: list[str]) -> bool:
    return (
        "States.ALL" in equals
        or error in equals
        or ("States.TaskFailed" in equals and error != "States.Timeout")
    )


# --------------------------------------------------------------------------- interpreter
class Simulator:
    def __init__(self, definition: dict[str, Any], handlers: dict[str, Handler]):
        self.definition = definition
        self.handlers = handlers

    def run(self, execution_input: Any, name: str = "test-exec") -> Execution:
        ctx = {
            "Execution": {
                "Id": f"arn:aws:states:us-east-1:123456789012:execution:triage:{name}",
                "Name": name,
            },
            "State": {},
        }
        history: list[str] = []
        try:
            output = self._run_machine(self.definition, execution_input, ctx, history)
            return Execution("SUCCEEDED", output, history)
        except StatesError as exc:
            return Execution("FAILED", None, history, exc.error, exc.cause)

    def _run_machine(self, machine: dict, data: Any, ctx: dict, history: list[str]) -> Any:
        name = machine["StartAt"]
        for _ in range(500):  # guard against accidental infinite loops
            state = machine["States"][name]
            history.append(name)
            ctx["State"] = {"Name": name, "EnteredTime": datetime.now(UTC).isoformat()}
            kind = state["Type"]

            if kind == "Succeed":
                return data
            if kind == "Fail":
                error = (
                    _get(data, state["ErrorPath"]) if "ErrorPath" in state else state.get("Error")
                )
                cause = (
                    _get(data, state["CausePath"]) if "CausePath" in state else state.get("Cause")
                )
                raise StatesError(error or "States.Fail", cause or "")
            if kind == "Choice":
                name = next(
                    (r["Next"] for r in state["Choices"] if _rule_matches(r, data)),
                    state.get("Default"),
                )
                if name is None:
                    raise StatesError("States.NoChoiceMatched", "")
                continue

            effective = _get(data, state.get("InputPath", "$"))
            try:
                if kind == "Pass":
                    if "Parameters" in state:
                        result = _payload(state["Parameters"], effective, ctx)
                    else:
                        result = state.get("Result", effective)
                elif kind == "Task":
                    result = self._task(state, effective, ctx)
                elif kind == "Map":
                    result = self._map(state, effective, ctx, history)
                else:
                    raise StatesError("States.Runtime", f"state type {kind} not supported")
            except StatesError as exc:
                catcher = next(
                    (c for c in state.get("Catch", []) if _matches(exc.error, c["ErrorEquals"])),
                    None,
                )
                if catcher is None:
                    raise
                data = _set(
                    data, catcher.get("ResultPath", "$"), {"Error": exc.error, "Cause": exc.cause}
                )
                name = catcher["Next"]
                continue

            if "ResultSelector" in state:
                result = _payload(state["ResultSelector"], result, ctx)
            data = _set(data, state.get("ResultPath", "$"), result)
            data = _get(data, state.get("OutputPath", "$"))
            if state.get("End"):
                return data
            name = state["Next"]
        raise StatesError("States.Runtime", "too many transitions")

    def _task(self, state: dict, data: Any, ctx: dict) -> Any:
        payload = _payload(state["Parameters"], data, ctx) if "Parameters" in state else data
        handler = self.handlers.get(state["Resource"])
        if handler is None:
            raise StatesError("States.Runtime", f"no handler for {state['Resource']}")
        attempts: dict[int, int] = {}
        while True:
            try:
                # Lambda receives a JSON round-tripped copy, just like the real service.
                return json.loads(json.dumps(handler(json.loads(json.dumps(payload)))))
            except StatesError:
                raise
            except Exception as exc:
                error = getattr(exc, "states_error", None) or type(exc).__name__
                for i, retrier in enumerate(state.get("Retry", [])):
                    if _matches(error, retrier["ErrorEquals"]):
                        attempts[i] = attempts.get(i, 0) + 1
                        if attempts[i] <= retrier.get("MaxAttempts", 3):
                            break
                        raise StatesError(error, str(exc)) from exc
                else:
                    raise StatesError(error, str(exc)) from exc

    def _map(self, state: dict, data: Any, ctx: dict, history: list[str]) -> list[Any]:
        items = _get(data, state.get("ItemsPath", "$"))
        out = []
        for index, item in enumerate(items):
            item_ctx = {**ctx, "Map": {"Item": {"Index": index, "Value": item}}}
            item_input = (
                _payload(state["ItemSelector"], data, item_ctx) if "ItemSelector" in state else item
            )
            out.append(self._run_machine(state["ItemProcessor"], item_input, item_ctx, history))
        return out
