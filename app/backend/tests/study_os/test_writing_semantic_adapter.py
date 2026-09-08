"""EWP-SP1b: unit + integration tests for the SHADOW semantic evaluator adapter.

The provider client is always mocked — no real API calls. These tests prove the
adapter's telemetry statuses, resilience (timeout/retry/circuit-breaker), and —
end to end through the worker — that shadow output NEVER affects canonical
deterministic completion and that no raw learner/prompt/source text is persisted.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pydantic")

from app.study_os.writing_practice import evaluation_worker  # noqa: E402
from app.study_os.writing_practice import language_evaluator as lang  # noqa: E402
from app.study_os.writing_practice import semantic_evaluator as se  # noqa: E402
from app.study_os.writing_practice.content_hash import compute_content_hash  # noqa: E402


# --- fake provider client ---------------------------------------------------

class _Block:
    def __init__(self, *, type, name=None, input=None, text=None):
        self.type = type
        self.name = name
        self.input = input
        self.text = text


class _Usage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Resp:
    def __init__(self, *, content, stop_reason="tool_use", usage=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class _FakeClient:
    """Minimal stand-in exposing .messages.create."""

    def __init__(self, *, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc
        self.calls = 0

        class _Messages:
            def create(inner_self, **kwargs):  # noqa: N805
                self.calls += 1
                if self._raise is not None:
                    raise self._raise
                return self._resp

        self.messages = _Messages()


def _tool_resp(*, issues=None, source_comparison=None, confidence=0.9, refusal=False,
               in_tok=120, out_tok=30, stop_reason="tool_use"):
    payload = {
        "issues": issues or [],
        "source_comparison": source_comparison,
        "meaning_preserved_confidence": confidence,
        "refusal": refusal,
    }
    return _Resp(
        content=[_Block(type="tool_use", name=se._SEMANTIC_TOOL_NAME, input=payload)],
        stop_reason=stop_reason,
        usage=_Usage(in_tok, out_tok),
    )


def _cfg(**kw):
    base = dict(model="claude-opus-4-7", max_retries=1, backoff_base_s=0.0,
                circuit_failure_threshold=3, circuit_cooldown_s=60.0,
                confidence_threshold=0.6)
    base.update(kw)
    return se.SemanticAdapterConfig(**base)


def _adapter(client, *, config=None, breaker=None):
    return se.SemanticLanguageEvaluator(
        config=config or _cfg(),
        client_factory=lambda: client,
        breaker=breaker or se._CircuitBreaker(threshold=3, cooldown_s=60.0),
    )


# --- happy path -------------------------------------------------------------

def test_happy_path_structured_verdict_and_telemetry():
    resp = _tool_resp(
        issues=[{"issue_type": "subject_verb_agreement", "severity": "must_fix",
                 "quoted_text": "They is", "explanation": "SV disagreement."}],
        source_comparison="source_comparison_uncertain", confidence=0.82,
        in_tok=200, out_tok=40,
    )
    result = _adapter(_FakeClient(resp=resp)).evaluate(
        "They is happy.", exercise_type="sentence_correction",
        prompt_text="Fix it.", source_text="They are happy.")

    assert result.status == "succeeded"
    assert result.provider == "anthropic"
    assert result.provider_model == "claude-opus-4-7"
    assert result.prompt_version == se.PROMPT_VERSION
    assert result.confidence == 0.82
    assert result.source_comparison == "source_comparison_uncertain"
    assert len(result.issues) == 1
    assert result.input_tokens == 200
    assert result.output_tokens == 40
    assert result.total_tokens == 240
    # cost = 200/1e6*5 + 40/1e6*25 = 0.001 + 0.001 = 0.002
    assert result.estimated_cost_usd == pytest.approx(0.002)
    # SHADOW output never sets needs_human_review (would be a canonical signal).
    assert result.needs_human_review is False


def test_low_confidence_status():
    resp = _tool_resp(confidence=0.2)
    result = _adapter(_FakeClient(resp=resp)).evaluate(
        "x", exercise_type="grammar")
    assert result.status == "low_confidence"
    assert result.confidence == 0.2


def test_malformed_output_status():
    # tool_use input missing required meaning_preserved_confidence + bad shape.
    resp = _Resp(content=[_Block(type="tool_use", name=se._SEMANTIC_TOOL_NAME,
                                 input={"issues": "not-a-list"})],
                 usage=_Usage(10, 5))
    result = _adapter(_FakeClient(resp=resp)).evaluate("x", exercise_type="grammar")
    assert result.status == "malformed"
    assert result.error_code == "schema_invalid"
    assert result.issues == []


def test_no_tool_use_block_is_malformed():
    resp = _Resp(content=[_Block(type="text", text="I think it's fine")],
                 usage=_Usage(10, 5))
    result = _adapter(_FakeClient(resp=resp)).evaluate("x", exercise_type="grammar")
    assert result.status == "malformed"


def test_refusal_via_stop_reason():
    resp = _tool_resp(stop_reason="refusal")
    result = _adapter(_FakeClient(resp=resp)).evaluate("x", exercise_type="grammar")
    assert result.status == "refusal"
    assert result.issues == []


def test_refusal_via_verdict_flag():
    resp = _tool_resp(refusal=True, confidence=0.99)
    result = _adapter(_FakeClient(resp=resp)).evaluate("x", exercise_type="grammar")
    assert result.status == "refusal"


# --- resilience -------------------------------------------------------------

def test_timeout_retries_then_fails_closed():
    client = _FakeClient(raise_exc=TimeoutError("provider slow"))
    cfg = _cfg(max_retries=2)
    result = _adapter(client, config=cfg).evaluate("x", exercise_type="grammar")
    assert result.status == "timeout"
    assert result.error_code == "TimeoutError"
    # 1 initial + 2 retries = 3 attempts.
    assert client.calls == 3
    assert result.issues == []  # fail-closed: no authoritative signal


def test_non_transient_error_not_retried():
    client = _FakeClient(raise_exc=ValueError("bad request"))
    result = _adapter(client, config=_cfg(max_retries=2)).evaluate(
        "x", exercise_type="grammar")
    assert result.status == "provider_error"
    assert client.calls == 1


def test_circuit_breaker_opens_after_threshold_and_short_circuits():
    breaker = se._CircuitBreaker(threshold=3, cooldown_s=60.0)
    client = _FakeClient(raise_exc=TimeoutError("down"))
    cfg = _cfg(max_retries=0)
    adapter = _adapter(client, config=cfg, breaker=breaker)

    # 3 consecutive failures trip the breaker.
    for _ in range(3):
        r = adapter.evaluate("x", exercise_type="grammar")
        assert r.status == "timeout"
    calls_before = client.calls

    # 4th call must short-circuit: no provider call, status skipped/circuit_open.
    r = adapter.evaluate("x", exercise_type="grammar")
    assert r.status == "skipped"
    assert r.error_code == "circuit_open"
    assert client.calls == calls_before  # provider was NOT called


def test_circuit_breaker_resets_on_success():
    breaker = se._CircuitBreaker(threshold=3, cooldown_s=60.0)
    # First a failure, then a success clears the counter.
    fail_adapter = se.SemanticLanguageEvaluator(
        config=_cfg(max_retries=0), client_factory=lambda: _FakeClient(raise_exc=TimeoutError()),
        breaker=breaker)
    fail_adapter.evaluate("x", exercise_type="grammar")
    ok_adapter = se.SemanticLanguageEvaluator(
        config=_cfg(), client_factory=lambda: _FakeClient(resp=_tool_resp()),
        breaker=breaker)
    ok_adapter.evaluate("x", exercise_type="grammar")
    assert breaker.is_open() is False


# --- worker integration: shadow never affects canonical ---------------------

class _Exec:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self

    @property
    def data(self):
        return self._data


class FakeSupabase:
    def __init__(self, responses=None):
        self._responses = responses or {}
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _Exec(self._responses.get(name))

    def call_names(self):
        return [n for n, _ in self.calls]

    def params_for(self, name):
        for n, p in self.calls:
            if n == name:
                return p
        raise AssertionError(f"rpc {name} not called")


def _claim(**overrides):
    answer = overrides.get("answer_text", "They are happy.")
    base = {
        "job_id": "job-1", "claim_token": "tok",
        "answer_text": answer, "content_hash": compute_content_hash(answer),
        "exercise_type": "sentence_correction", "is_current": True,
        "user_id": "u1", "evaluation_id": "eval-1", "unit_version_id": "ver-1",
        "evaluation_revision": 1, "topic_id": "t1", "session_id": "s1",
        "microtopic_id": None, "exam_id": None,
        "active_prior_issues": [], "resolved_prior_lineages": [],
        "prompt_text": "Correct the sentence.", "source_text": "They are happy.",
    }
    base.update(overrides)
    return base


def _run_with_adapter(monkeypatch, adapter):
    monkeypatch.setenv("FF_WRITING_LLM_EVAL", "shadow")
    monkeypatch.setattr(lang, "get_semantic_shadow_evaluator", lambda: adapter)


def _baseline(monkeypatch):
    monkeypatch.delenv("FF_WRITING_LLM_EVAL", raising=False)
    sb = FakeSupabase({"ewp_claim_evaluation_job": _claim(),
                       "ewp_complete_language_evaluation": {"ok": True}})
    evaluation_worker.run_worker_pass(sb)
    return sb.params_for("ewp_complete_language_evaluation")


def test_worker_real_adapter_provider_failure_is_inert(monkeypatch):
    baseline = _baseline(monkeypatch)

    failing = _adapter(_FakeClient(raise_exc=TimeoutError("down")),
                       config=_cfg(max_retries=0))
    _run_with_adapter(monkeypatch, failing)
    sb = FakeSupabase({"ewp_claim_evaluation_job": _claim(),
                       "ewp_record_language_evaluator_run": {"ok": True, "id": "r1"},
                       "ewp_complete_language_evaluation": {"ok": True}})
    out = evaluation_worker.run_worker_pass(sb)

    assert out["status"] == "succeeded"
    completion = sb.params_for("ewp_complete_language_evaluation")
    # Canonical completion byte-identical to shadow-off.
    assert completion["p_evaluator_version"] == baseline["p_evaluator_version"]
    assert completion["p_language_result"] == baseline["p_language_result"]
    assert completion["p_issues"] == baseline["p_issues"]
    assert completion["p_needs_human_review"] == baseline["p_needs_human_review"]
    # Failure recorded as telemetry only.
    tel = sb.params_for("ewp_record_language_evaluator_run")
    assert tel["p_status"] == "timeout"


def test_worker_real_adapter_success_unchanged_canonical_and_no_raw_text(monkeypatch):
    baseline = _baseline(monkeypatch)

    resp = _tool_resp(
        issues=[{"issue_type": "subject_verb_agreement", "severity": "must_fix",
                 "quoted_text": "They are happy.",
                 "explanation": "RAW-SNIPPET-MUST-NOT-PERSIST"}],
        source_comparison="source_unchanged", confidence=0.91, in_tok=150, out_tok=25)
    ok = _adapter(_FakeClient(resp=resp))
    _run_with_adapter(monkeypatch, ok)

    sb = FakeSupabase({"ewp_claim_evaluation_job": _claim(),
                       "ewp_record_language_evaluator_run": {"ok": True, "id": "r1"},
                       "ewp_complete_language_evaluation": {"ok": True}})
    out = evaluation_worker.run_worker_pass(sb)
    assert out["status"] == "succeeded"

    completion = sb.params_for("ewp_complete_language_evaluation")
    # Shadow success STILL does not change canonical output.
    assert completion["p_evaluator_version"] == baseline["p_evaluator_version"]
    assert completion["p_language_result"] == baseline["p_language_result"]
    assert completion["p_issues"] == baseline["p_issues"]

    tel = sb.params_for("ewp_record_language_evaluator_run")
    assert tel["p_status"] == "succeeded"
    assert tel["p_provider"] == "anthropic"
    assert tel["p_provider_model"] == "claude-opus-4-7"
    assert tel["p_prompt_version"] == se.PROMPT_VERSION
    assert tel["p_semantic_confidence"] == 0.91
    assert tel["p_input_tokens"] == 150
    assert tel["p_output_tokens"] == 25
    assert tel["p_total_tokens"] == 175
    assert tel["p_estimated_cost_usd"] == pytest.approx(150/1e6*5 + 25/1e6*25)
    assert tel["p_semantic_source_comparison"] == "source_unchanged"

    # No raw learner/prompt/source text nor issue snippet anywhere in telemetry.
    serialized = repr(tel)
    assert "They are happy." not in serialized
    assert "Correct the sentence." not in serialized
    assert "RAW-SNIPPET-MUST-NOT-PERSIST" not in serialized
    # Only the hash + summary carry semantic info.
    assert tel["p_input_hash"]
    assert tel["p_result_json"]["issue_count"] == 1


# --- governance guards ------------------------------------------------------

def test_off_flag_constructs_no_adapter(monkeypatch):
    monkeypatch.delenv("FF_WRITING_LLM_EVAL", raising=False)
    assert lang.get_semantic_shadow_evaluator() is None


def test_live_flag_not_wired_to_shadow_seam(monkeypatch):
    # LIVE still returns None from the shadow seam this PR — never wired to canonical.
    monkeypatch.setenv("FF_WRITING_LLM_EVAL", "live")
    assert lang.get_semantic_shadow_evaluator() is None


def test_canonical_evaluator_stays_deterministic(monkeypatch):
    monkeypatch.setenv("FF_WRITING_LLM_EVAL", "shadow")
    assert isinstance(lang.get_language_evaluator(), lang.MockLanguageEvaluator)


def test_shadow_flag_builds_real_semantic_adapter(monkeypatch):
    monkeypatch.setenv("FF_WRITING_LLM_EVAL", "shadow")
    ev = lang.get_semantic_shadow_evaluator()
    assert isinstance(ev, se.SemanticLanguageEvaluator)


# --- EWP-SP1b: where the correction-vs-unrelated distinction actually lives ---
#
# The source-aware runtime threads prompt_text/source_text to the evaluator, but
# that alone does NOT let the system tell a meaning-preserving CORRECTION from a
# clean but UNRELATED sentence. Both are non-trivial changes to the source, and
# the deterministic layer returns `source_comparison_uncertain` for every one of
# those by design (language_evaluator.compute_source_comparison: meaning
# preservation is not deterministically decidable, and the similarity thresholds
# that would guess at it were rejected as gameable).
#
# These two tests pin that honestly: the first proves the deterministic layer
# CANNOT separate the cases, the second proves the semantic adapter is the layer
# that can — and that in SHADOW its verdict reaches telemetry only, never the
# canonical outcome. Until FF_WRITING_LLM_EVAL is LIVE (blocked on the §5.2
# promotion gates), "a correct answer passes" is not a property this system has.

_GAP_SOURCE = "He go to school every day."
_GAP_CORRECTED = "He goes to school every day."
_GAP_UNRELATED = "The monsoon arrived early this year."


def _canonical_for(answer, *, monkeypatch, adapter=None, responses=None):
    """Run one answer through the worker; return (completion params, telemetry|None)."""
    if adapter is None:
        monkeypatch.delenv("FF_WRITING_LLM_EVAL", raising=False)
    else:
        _run_with_adapter(monkeypatch, adapter)
    resp = {"ewp_claim_evaluation_job": _claim(answer_text=answer,
                                               source_text=_GAP_SOURCE),
            "ewp_complete_language_evaluation": {"ok": True}}
    resp.update(responses or {})
    sb = FakeSupabase(resp)
    out = evaluation_worker.run_worker_pass(sb)
    assert out["status"] == "succeeded"
    tel = None
    if adapter is not None:
        tel = sb.params_for("ewp_record_language_evaluator_run")
    return sb.params_for("ewp_complete_language_evaluation"), tel


def test_deterministic_layer_cannot_separate_a_correction_from_an_unrelated_sentence(monkeypatch):
    corrected, _ = _canonical_for(_GAP_CORRECTED, monkeypatch=monkeypatch)
    unrelated, _ = _canonical_for(_GAP_UNRELATED, monkeypatch=monkeypatch)

    # Both land on the same fail-closed verdict...
    for completion in (corrected, unrelated):
        assert completion["p_language_result"]["source_comparison"] == "source_comparison_uncertain"
        assert completion["p_needs_human_review"] is True

    # ...and the canonical payloads are INDISTINGUISHABLE apart from the answer
    # itself. This is the gap, asserted rather than papered over: threading
    # source_text in does not make a correct answer pass, and does not make a
    # wrong one fail any harder.
    assert corrected["p_language_result"] == unrelated["p_language_result"]
    assert corrected["p_issues"] == unrelated["p_issues"]
    assert corrected["p_needs_human_review"] == unrelated["p_needs_human_review"]


def test_shadow_adapter_separates_them_but_cannot_change_the_canonical_outcome(monkeypatch):
    # The adapter sees a meaning-preserving correction as clean...
    ok = _adapter(_FakeClient(resp=_tool_resp(source_comparison=None, confidence=0.95)))
    corrected, corrected_tel = _canonical_for(
        _GAP_CORRECTED, monkeypatch=monkeypatch, adapter=ok,
        responses={"ewp_record_language_evaluator_run": {"ok": True, "id": "r1"}})

    # ...and an unrelated sentence as not preserving the source's meaning.
    bad = _adapter(_FakeClient(resp=_tool_resp(source_comparison="meaning_not_preserved",
                                               confidence=0.95)))
    unrelated, unrelated_tel = _canonical_for(
        _GAP_UNRELATED, monkeypatch=monkeypatch, adapter=bad,
        responses={"ewp_record_language_evaluator_run": {"ok": True, "id": "r2"}})

    # The distinction EXISTS — in telemetry.
    assert corrected_tel["p_semantic_source_comparison"] is None
    assert unrelated_tel["p_semantic_source_comparison"] == "meaning_not_preserved"
    assert corrected_tel["p_semantic_source_comparison"] != unrelated_tel["p_semantic_source_comparison"]

    # The deterministic side of the SAME telemetry rows shows it did not.
    for tel in (corrected_tel, unrelated_tel):
        assert tel["p_deterministic_source_comparison"] == "source_comparison_uncertain"
        assert tel["p_deterministic_needs_human_review"] is True

    # And SHADOW changes nothing canonical: still identical, still human review.
    assert corrected["p_language_result"] == unrelated["p_language_result"]
    assert corrected["p_needs_human_review"] is True
    assert unrelated["p_needs_human_review"] is True
