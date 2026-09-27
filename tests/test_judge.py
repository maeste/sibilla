"""Judge: typed contract, wire client, packer scheduling, per-source routing."""

from __future__ import annotations

import pytest
from conftest import FakeBackend

from sibilla.judge import configs as jcfgs
from sibilla.judge.base import Answer, JudgeError, Question, parse_answer
from sibilla.judge.clm import ClmBackend
from sibilla.judge.packer import Packer
from sibilla.judge.typesafe import TypesafeBackend

# --------------------------------------------------------------- typed contract


def test_parse_score_clamps_to_scale():
    q = Question("relevance", "score", "how relevant", scale_max=10)
    a = parse_answer(q, {"value": 42, "confidence": 0.9})
    assert a.value == 10.0  # the model never invents structure — clamp to scale
    with pytest.raises(JudgeError):
        parse_answer(q, {"value": "very relevant"})


def test_parse_noul_probability_bounds():
    q = Question("novelty", "noul", "new?")
    assert parse_answer(q, {"probability": 1.7}).value == 1.0
    assert parse_answer(q, {"p": -0.2}).value == 0.0


def test_parse_choice_never_leaves_the_taxonomy():
    q = Question("kind", "choice", "what?", options=("tooling", "meme"))
    assert parse_answer(q, {"choice": "tooling", "probabilities": {"tooling": 0.9}}).value == "tooling"
    # out-of-taxonomy label falls back to the distribution argmax
    assert parse_answer(q, {"choice": "gossip", "probabilities": {"meme": 0.7, "tooling": 0.3}}).value == "meme"
    with pytest.raises(JudgeError):
        parse_answer(q, {"choice": "gossip"})


# ------------------------------------------------------------------ wire client


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)
        self.ok = status < 400

    def json(self):
        return self._payload


def _wire_payload(items, questions):
    def per_item(_text):
        out = {}
        for q in questions:
            if q.primitive == "score":
                out[q.id] = {"value": 5.0, "confidence": 0.8}
            elif q.primitive == "noul":
                out[q.id] = {"probability": 0.5, "confidence": 0.8}
            else:
                out[q.id] = {"choice": q.options[0], "probabilities": {q.options[0]: 1.0}, "confidence": 0.8}
        return out

    return {"answers": [per_item(t) for t in items], "usage": {"input_tokens": 640, "output_tokens": 0}}


def test_http_backend_wire_roundtrip(monkeypatch):
    backend = ClmBackend(base_url="http://127.0.0.1:8700", session=_Session(_wire_payload, monkeypatch_target=None))
    questions = jcfgs.get_config("arxiv").questions
    answers = backend.system_one("state text", "item text", questions)
    assert answers["relevance"].value == 5.0
    assert answers["novelty"].value == 0.5
    assert backend.last_call.input_tokens == 640
    assert backend.cost_usd(640) == 0.0  # local CLM: marginal cost $0

    jev = TypesafeBackend(base_url="https://api.typesafe.ai", api_key="k", session=_Session(_wire_payload, None))
    assert jev.cost_usd(1_000_000) == pytest.approx(0.042)  # $0.042/Mtok, output free


class _Session:
    """Minimal requests.Session stand-in recording posts."""

    last_post = None

    def __init__(self, responder, monkeypatch_target=None):
        self.responder = responder

    def post(self, url, data=None, headers=None, timeout=None):
        import json as _json

        _Session.last_post = {"url": url, "body": _json.loads(data), "headers": headers}
        body = _Session.last_post["body"]
        return _FakeResp(self.responder(body["items"], [Question(**{**_unwire(q)}) for q in body["questions"]]))

    def get(self, url, timeout=None):
        return _FakeResp({"ok": True})


def _unwire(q):
    return {
        "id": q["id"],
        "primitive": q["type"],
        "text": q["question"],
        "scale_min": q.get("min", 0.0),
        "scale_max": q.get("max", 10.0),
        "options": tuple(q.get("options", ())),
    }


def test_wire_request_shape():
    """A request written for one backend replays on the other (docs/03)."""
    questions = jcfgs.get_config("arxiv").questions
    session = _Session(_wire_payload)
    backend = ClmBackend(base_url="http://x", session=session)
    backend.system_one_batch("STATE", ["a", "b"], questions)
    body = _Session.last_post["body"]
    assert body["state"] == "STATE"
    assert len(body["items"]) == 2
    assert body["questions"][0]["type"] == "score"
    assert _Session.last_post["url"].endswith("/v1/system_one")


# ------------------------------------------------------------------- scheduling


def test_packer_fanout_and_truncation():
    backend = FakeBackend()
    from sibilla.judge.packer import JudgeJob

    job = JudgeJob(source="arxiv", config=jcfgs.get_config("arxiv"))
    long_text = "x" * 9000
    job.add("arxiv:1", long_text)
    result = Packer(backend, ledger=lambda **kw: None).run("S", [job])
    assert "arxiv:1" in result.answers_by_item
    assert result.truncated_by_item["arxiv:1"] is True  # flagged, docs/01 failure table
    assert len(job.texts[0]) == jcfgs.get_config("arxiv").char_budget


def test_packer_failure_isolation():
    """One poisoned item cannot take down the cycle (docs/03)."""
    backend = FakeBackend(fail_ids={"poison"})
    from sibilla.judge.packer import JudgeJob

    job = JudgeJob(source="arxiv", config=jcfgs.get_config("arxiv"))
    job.add("arxiv:ok1", "clean item one")
    job.add("arxiv:bad", "poison item")
    job.add("arxiv:ok2", "clean item two")
    ledger: list[dict] = []
    result = Packer(backend, ledger=lambda **kw: ledger.append(kw)).run("S", [job])
    assert set(result.answers_by_item) == {"arxiv:ok1", "arxiv:ok2"}
    assert "poison" in result.errors_by_item["arxiv:bad"]
    assert any(c.get("error") for c in ledger)  # the failure hit the cost ledger too


def test_packer_packed_shape_batches():
    backend = FakeBackend(packs=True)
    from sibilla.judge.packer import JudgeJob

    job = JudgeJob(source="arxiv", config=jcfgs.get_config("arxiv"))
    for i in range(5):
        job.add(f"arxiv:{i}", f"item {i}")
    Packer(backend, ledger=lambda **kw: None, pack_size=2).run("S", [job])
    # 5 items / pack 2 → 3 packed requests, items never mixed across sources
    assert [c["items"] for c in backend.calls] == [2, 2, 1]


# ----------------------------------------------------------------- routing rules


def _answers(source: str, relevance: float, novelty=0.8, kind=None, substance=None, confidence=0.95):
    out = {"relevance": Answer("relevance", "score", relevance, confidence)}
    qids = {q.id for q in jcfgs.get_config(source).questions}
    if "novelty" in qids:
        out["novelty"] = Answer("novelty", "noul", novelty, confidence)
    if "substance" in qids:
        out["substance"] = Answer("substance", "noul", substance if substance is not None else 0.9, confidence)
    if "kind" in qids and kind:
        out["kind"] = Answer("kind", "choice", kind, confidence)
    return out


def test_arxiv_composite_weights_applicability():
    # 0.7*relevance + 0.3*applicability (docs/03: applicability is the READ boost)
    answers = _answers("arxiv", 7.0)
    answers["applicability"] = Answer("applicability", "score", 10.0, 0.95)
    assert jcfgs.composite_score("arxiv", answers) == pytest.approx(7.9)
    quad, _ = jcfgs.classify("arxiv", answers)
    assert quad == "READ"  # boosted over the skim cut


def test_hn_drama_never_reads():
    quad, reason = jcfgs.classify("hackernews", _answers("hackernews", 10.0, kind="drama"))
    assert quad == "KILL"
    assert "drama" in reason


def test_reddit_meme_and_question_auto_kill():
    for kind in ("meme", "question"):
        quad, _ = jcfgs.classify("reddit", _answers("reddit", 9.0, kind=kind))
        assert quad == "KILL"


def test_x_meta_commentary_auto_kill_and_aggressive_cuts():
    assert jcfgs.classify("x", _answers("x", 9.0, kind="meta-commentary"))[0] == "KILL"
    # aggressive thresholds: 7.5 relevance is not READ on X (cut at 8)
    assert jcfgs.classify("x", _answers("x", 7.5, kind="release"))[0] == "SKIM"


def test_substance_kill_override():
    quad, reason = jcfgs.classify("hackernews", _answers("hackernews", 10.0, kind="announcement", substance=0.2))
    assert quad == "KILL"
    assert "substance" in reason


def test_confidence_min_across_answers():
    answers = _answers("arxiv", 8.0, confidence=0.9)
    answers["applicability"] = Answer("applicability", "score", 5.0, 0.4)
    assert jcfgs.min_confidence(answers) == pytest.approx(0.4)


def test_registry_versions_are_per_source():
    for src in ("arxiv", "hackernews", "reddit", "x"):
        cfg = jcfgs.get_config(src)
        assert cfg.source == src and cfg.version and cfg.char_budget > 0 and cfg.questions


# ------------------------------------------------- backend defaults & health


def test_typesafe_config_without_base_url_reaches_typesafe(monkeypatch):
    """Dogfooding regression: an omitted base_url must NOT inherit the CLM
    loopback default — a typesafe config pointed at localhost:8700 reports
    'backend down' while really pointing at the wrong host."""
    from sibilla.config import Config, JudgeConfigSettings
    from sibilla.pipeline import make_backend

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cfg = Config.__new__(Config)  # minimal shell; make_backend only reads cfg.judge
    cfg.judge = JudgeConfigSettings(backend="typesafe", api_key_env="TYPESAFE_API_KEY")
    backend = make_backend(cfg)
    assert backend.base_url == "https://api.typesafe.ai"  # the class default wins
    assert backend.api_key is None  # env var absent — health will say exactly that


def test_backend_defaults_by_class(monkeypatch):
    from sibilla.config import Config, JudgeConfigSettings
    from sibilla.pipeline import make_backend

    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    cfg = Config.__new__(Config)
    cfg.judge = JudgeConfigSettings(backend="typesafe", model="jev-latest", api_key_env="TYPESAFE_API_KEY")
    ts = make_backend(cfg)
    assert ts.base_url == "https://api.typesafe.ai" and ts.api_key == "k" and ts.model == "jev-latest"

    cfg.judge = JudgeConfigSettings(backend="clm")  # CLM default: loopback
    clm = make_backend(cfg)
    assert clm.base_url == "http://127.0.0.1:8700"


def test_health_detail_distinguishes_auth_from_outage():
    """'backend down' must say WHY — missing key, 401, or unreachable."""
    import requests as rq

    class _Resp:
        def __init__(self, status):
            self.status_code = status
            self.ok = status < 400

    class _Sess:
        def __init__(self, behavior):
            self.behavior = behavior

        def get(self, url, timeout=None):
            if self.behavior == "refused":
                raise rq.ConnectionError("refused")
            return _Resp(self.behavior)

    jev_missing_key = TypesafeBackend(session=_Sess(200))  # no api_key
    ok, reason = jev_missing_key.health_detail()
    assert not ok and "api key missing" in reason and "TYPESAFE_API_KEY" in reason

    jev_401 = TypesafeBackend(api_key="bad", session=_Sess(401))
    ok, reason = jev_401.health_detail()
    assert not ok and "auth failed" in reason and "401" in reason

    jev_down = TypesafeBackend(api_key="k", session=_Sess("refused"))
    ok, reason = jev_down.health_detail()
    assert not ok and "unreachable" in reason

    jev_up = TypesafeBackend(api_key="k", session=_Sess(200))
    ok, reason = jev_up.health_detail()
    assert ok and reason == ""

    # a 404 on /health still falls back to / (playground) — old semantics kept
    jev_404_health = TypesafeBackend(api_key="k", session=_Sess(404))
    ok, reason = jev_404_health.health_detail()
    assert not ok and "404" in reason


def test_run_report_backend_reason_in_summary():
    from sibilla.pipeline import RunReport

    report = RunReport(
        date_label="2026-09-27", backend_down=True, backend_reason="api key missing — export TYPESAFE_API_KEY"
    )
    assert "api key missing" in report.summary()
    assert "BACKEND DOWN" in report.summary()
