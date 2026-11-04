"""FakeLLM - a deterministic stand-in for a real model.

Two modes:
* scripted: `FakeLLM(script={"plan": {...}, "review": [first, second]})` returns exactly what a
  test asks for (a list is consumed one item per call; a callable gets (system, user)).
* heuristic (default): keyword rules good enough to drive the whole pipeline end to end, so the
  stack can be deployed and smoke-tested with `LlmProvider=fake` - no API key, no cost.

The heuristics intentionally reproduce interesting behaviour: a hallucinated plan step, a first
draft that over-promises a refund (caught by the reviewer, fixed on redraft) and a legal threat
that must go to a human.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from acme_common.llm.base import LLMClient, RawCompletion
from acme_common.prompts import read_block

Responder = dict[str, Any] | list[dict[str, Any]] | Callable[[str, str], dict[str, Any]]


def _has(text: str, *needles: str) -> bool:
    return any(n in text for n in needles)


class FakeLLM(LLMClient):
    provider = "fake"

    def __init__(self, model: str = "fake-heuristic-1", script: dict[str, Responder] | None = None):
        super().__init__(model)
        self._script: dict[str, Responder] = dict(script or {})
        self.calls: list[dict[str, Any]] = []

    def _complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
    ) -> RawCompletion:
        self.calls.append({"schema_name": schema_name, "system": system, "user": user})
        if schema_name in self._script:
            data = self._scripted(schema_name, system, user)
        else:
            data = self._heuristic(schema_name, user, schema)
        prompt_chars = len(system) + len(user)
        return RawCompletion(
            data=data,
            input_tokens=math.ceil(prompt_chars / 4),
            output_tokens=math.ceil(len(str(data)) / 4),
        )

    # ------------------------------------------------------------------ scripted
    def _scripted(self, name: str, system: str, user: str) -> dict[str, Any]:
        entry = self._script[name]
        if callable(entry):
            return entry(system, user)
        if isinstance(entry, list):
            if not entry:
                raise AssertionError(f"FakeLLM script for '{name}' exhausted")
            return entry.pop(0)
        return entry

    # ------------------------------------------------------------------ heuristic
    def _heuristic(self, name: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        ticket = read_block(user, "ticket") or {}
        text = f"{ticket.get('subject', '')}\n{ticket.get('body', '')}".lower()
        handler = {
            "plan": self._plan,
            "classification": self._classify,
            "extraction": lambda t, u: self._extract(t, schema),
            "draft_reply": self._draft,
            "review": self._review,
        }.get(name)
        if handler is None:
            raise ValueError(f"FakeLLM has no heuristic for schema '{name}'")
        return handler(text, user)

    @staticmethod
    def _plan(text: str, _user: str) -> dict[str, Any]:
        if _has(text, "unsubscribe", "seo services", "backlinks", "crypto opportunity"):
            steps = ["classify"]
            reason = "No product issue detected; classify only so it can be routed or discarded."
        elif _has(text, "feature request", "would love", "it would be great", "please add"):
            steps = ["classify", "draft_reply"]
            reason = "Feature request: classify and acknowledge; nothing to extract."
        else:
            steps = ["classify", "extract", "draft_reply"]
            reason = "Product issue: classify, extract diagnostics, and draft a reply."
        if _has(text, "escalate to engineering"):
            steps.append("escalate_to_engineering")  # not on the allow-list - must be dropped
        return {"steps": steps, "reason": reason}

    @staticmethod
    def _classify(text: str, _user: str) -> dict[str, Any]:
        if _has(text, "unsubscribe", "seo services", "backlinks", "crypto"):
            category = "other"
        elif _has(text, "outage", "is down", "cannot access", "can't access", "http 500"):
            category = "outage"
        elif _has(text, "invoice", "charged", "refund", "billing", "credit card", "subscription"):
            category = "billing"
        elif _has(text, "password", "log in", "login", "2fa", "locked out", "account"):
            category = "account"
        elif _has(text, "feature request", "would love", "it would be great", "please add"):
            category = "feature_request"
        elif _has(text, "how do i", "how to", "is there a way"):
            category = "how_to"
        elif _has(text, "error", "crash", "bug", "stuck", "sync", "broken", "not working"):
            category = "bug"
        else:
            category = "other"

        if _has(text, "lawyer", "legal action", "unacceptable", "furious", "!!!"):
            sentiment = "angry"
        elif _has(text, "frustrat", "annoy", "not working", "still", "again"):
            sentiment = "negative"
        elif _has(text, "love", "thanks", "thank you", "great app"):
            sentiment = "positive"
        else:
            sentiment = "neutral"

        priority = {"outage": "p1", "bug": "p2", "billing": "p2", "account": "p2"}.get(
            category, "p3" if category != "feature_request" else "p4"
        )
        if sentiment == "angry" and priority in ("p3", "p4"):
            priority = "p2"
        return {
            "category": category,
            "priority": priority,
            "sentiment": sentiment,
            "confidence": 0.72,
            "rationale": f"Keyword heuristics matched category '{category}'.",
        }

    @staticmethod
    def _extract(text: str, schema: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, spec in schema.get("properties", {}).items():
            enum = [v for v in spec.get("enum", []) if v is not None]
            if name == "error_code":
                m = re.search(r"\b([a-z]{2,6}-\d{3,5})\b", text)
                out[name] = m.group(1).upper() if m else None
            elif name == "app_version":
                m = re.search(r"\bv?(\d+\.\d+(?:\.\d+)?)\b", text)
                out[name] = m.group(1) if m else None
            elif name == "os" and enum:
                aliases = {"mac": "macos", "iphone": "ios", "ipad": "ios", "browser": "web"}
                hit = next((v for v in enum if v in text), None)
                hit = hit or next((v for k, v in aliases.items() if k in text), None)
                out[name] = hit or ("unknown" if "unknown" in enum else None)
            elif enum:
                # Generic rule: first enum value mentioned (underscores as spaces). This is what
                # makes a brand-new registry field work with zero code changes.
                out[name] = next(
                    (v for v in enum if isinstance(v, str) and v.replace("_", " ") in text), None
                )
            elif "boolean" in (spec.get("type") or []):
                out[name] = None
            else:
                out[name] = None
        return out

    @staticmethod
    def _draft(text: str, user: str) -> dict[str, Any]:
        feedback = read_block(user, "reviewer_feedback")
        opening = "Thanks for reaching out to Acme Notes support, and sorry for the trouble."
        if feedback:
            body = (
                "I've passed the details to the right team so they can review your account. "
                "We'll follow up as soon as we have an update."
            )
        elif _has(text, "refund", "charged twice", "double charged"):
            body = "We will refund your last invoice in full right away and fix this by tomorrow."
        elif _has(text, "feature request", "would love", "please add"):
            body = "I've logged your suggestion with our product team - thank you for the idea."
        else:
            body = (
                "Could you try signing out and back in, then confirm your app version? "
                "That helps us narrow down the issue quickly."
            )
        return {
            "reply": f"{opening} {body}\n\n- The Acme Notes Support Team",
            "tone": "empathetic",
            "needs_followup": not _has(text, "feature request", "please add"),
        }

    @staticmethod
    def _review(text: str, user: str) -> dict[str, Any]:
        draft = (read_block(user, "draft") or {}).get("reply", "").lower()
        issues = []
        if "refund" in draft and "in full" in draft:
            issues.append(
                {
                    "type": "refund_or_credit",
                    "detail": "Draft promises a refund agents can't grant.",
                }
            )
        if "tomorrow" in draft:
            issues.append({"type": "timeline_commitment", "detail": "Draft commits to a fix date."})
        verdict = "revise" if issues else "approve"
        feedback = "; ".join(i["detail"] for i in issues) or "Looks good."
        return {"verdict": verdict, "issues": issues, "feedback": feedback}
