"""Tests for batch_extractor — fake batches client, no real API calls."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from wechat_daily import batch_extractor as bx
from wechat_daily.config import debug_dir_for
from wechat_daily.llm_extractor import ExtractionError


# ── Fakes ───────────────────────────────────────────────────────────────────────


class _Msg:
    """Raw chat message stub (fingerprint fields only)."""

    def __init__(self, t, typ, wxid, content, link_context=""):
        self.create_time = t
        self.local_type = typ
        self.sender_wxid = wxid
        self.content = content
        self.link_context = link_context


class _Block:
    def __init__(self, type, text="", thinking=""):
        self.type = type
        self.text = text
        self.thinking = thinking


class _Message:
    def __init__(self, markdown="正文", stop_reason="end_turn", thinking="思考", usage=None):
        self.stop_reason = stop_reason
        self.content = [
            _Block("thinking", thinking=thinking),
            _Block("text", text=markdown),
        ]
        self.usage = usage


class _Err:
    def __init__(self, type):
        self.type = type


class _Result:
    """One entry from batches.results(): .custom_id + .result.{type,message,error}."""

    def __init__(self, custom_id, type, message=None, error_type=None):
        self.custom_id = custom_id
        self.result = _ResultInner(type, message, error_type)


class _ResultInner:
    def __init__(self, type, message, error_type):
        self.type = type
        self.message = message
        self.error = _Err(error_type) if error_type else None


class _Batch:
    def __init__(self, id, processing_status="ended", counts=None):
        self.id = id
        self.processing_status = processing_status
        self.request_counts = counts


class _FakeBatches:
    def __init__(self):
        self.created: list[dict] = []          # kwargs of each create()
        self.retrieve_script: list = []        # popped per retrieve(); last repeats
        self.results_map: dict[str, list] = {} # batch_id → result list
        self.cancelled: list[str] = []
        self._n = 0

    def create(self, *, requests):
        self._n += 1
        batch_id = f"msgbatch_{self._n:03d}"
        self.created.append({"id": batch_id, "requests": requests})
        return _Batch(batch_id)

    def retrieve(self, batch_id):
        if self.retrieve_script:
            item = self.retrieve_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _Batch(batch_id, "ended")

    def results(self, batch_id):
        yield from self.results_map.get(batch_id, [])

    def cancel(self, batch_id):
        self.cancelled.append(batch_id)


class _FakeClient:
    def __init__(self):
        self.messages = type("M", (), {})()
        self.messages.batches = _FakeBatches()


@pytest.fixture()
def debug_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


# ── State file ──────────────────────────────────────────────────────────────────


def _state(**over):
    base = dict(
        batch_id="msgbatch_abc",
        date="2026-07-02",
        submitted_at="2026-07-02T21:00:00+08:00",
        raw_msg_count=412,
        raw_msg_sha256="deadbeef",
        requests={"main": "claude-opus-4-6", "compare": "claude-fable-5"},
    )
    base.update(over)
    return bx.BatchState(**base)


def test_state_roundtrip(debug_dir):
    state = _state()
    bx.save_state(state)
    loaded = bx.load_state("2026-07-02")
    assert loaded == state
    assert loaded.version == bx.STATE_VERSION
    # On-disk JSON carries the version field explicitly.
    raw = json.loads(bx.state_path("2026-07-02").read_text(encoding="utf-8"))
    assert raw["version"] == bx.STATE_VERSION


def test_load_state_missing_returns_none(debug_dir):
    assert bx.load_state("2026-07-02") is None


def test_load_state_unknown_version_raises(debug_dir):
    bx.save_state(_state())
    path = bx.state_path("2026-07-02")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(bx.BatchStateError, match="99"):
        bx.load_state("2026-07-02")


def test_load_state_corrupt_json_raises(debug_dir):
    path = bx.state_path("2026-07-02")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(bx.BatchStateError):
        bx.load_state("2026-07-02")


def test_load_state_ignores_unknown_future_fields(debug_dir):
    """Same-version files with extra fields load fine (forward-tolerant)."""
    bx.save_state(_state())
    path = bx.state_path("2026-07-02")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["some_future_hint"] = "x"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert bx.load_state("2026-07-02").batch_id == "msgbatch_abc"


def test_mark_consumed_persists(debug_dir):
    state = _state()
    bx.save_state(state)
    bx.mark_consumed(state)
    assert bx.load_state("2026-07-02").consumed is True


# ── Content snapshot ───────────────────────────────────────────────────────────


_BLOCKS = [
    {"type": "text", "text": "<chat_log>\n[10:00] 沉稳的大象: 你好\n"},
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}},
    {"type": "text", "text": "\n</chat_log>\n指令"},
]


def test_content_snapshot_roundtrip(debug_dir):
    bx.save_content_snapshot("2026-07-02", _BLOCKS)
    assert bx.load_content_snapshot("2026-07-02") == _BLOCKS


def test_load_content_snapshot_missing_or_corrupt_returns_none(debug_dir):
    assert bx.load_content_snapshot("2026-07-02") is None
    path = bx.content_snapshot_path("2026-07-02")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    assert bx.load_content_snapshot("2026-07-02") is None


def test_snapshot_debug_text_matches_build_semantics():
    # Text blocks concatenated, images contribute nothing — mirrors
    # build_extract_user_content's debug_text.
    assert bx.snapshot_debug_text(_BLOCKS) == (
        "<chat_log>\n[10:00] 沉稳的大象: 你好\n\n</chat_log>\n指令"
    )
    assert bx.snapshot_debug_text("flat string") == "flat string"


def test_submit_writes_snapshot_and_consume_removes_it(debug_dir):
    client = _FakeClient()
    state = bx.submit_batch(
        client, "2026-07-02", {"main": "claude-opus-4-6"}, _BLOCKS, (1, "sha"),
    )
    assert bx.load_content_snapshot("2026-07-02") == _BLOCKS
    bx.mark_consumed(state)
    assert not bx.content_snapshot_path("2026-07-02").exists()
    # State survives the cleanup.
    assert bx.load_state("2026-07-02").consumed is True


# ── Fingerprint ─────────────────────────────────────────────────────────────────


def test_fingerprint_stable_and_ignores_link_context():
    msgs = [_Msg(1, 1, "a", "hello"), _Msg(2, 1, "b", "world")]
    count, sha = bx.raw_messages_fingerprint(msgs)
    assert count == 2
    # Enrichment mutates link_context only — fingerprint must not move.
    msgs[0].link_context = "【网页摘要】随机内容"
    assert bx.raw_messages_fingerprint(msgs) == (count, sha)


def test_fingerprint_changes_on_new_message():
    msgs = [_Msg(1, 1, "a", "hello")]
    _, sha1 = bx.raw_messages_fingerprint(msgs)
    msgs.append(_Msg(2, 1, "b", "world"))
    count, sha2 = bx.raw_messages_fingerprint(msgs)
    assert count == 2
    assert sha1 != sha2


def test_fingerprint_changes_on_content_edit():
    _, sha1 = bx.raw_messages_fingerprint([_Msg(1, 1, "a", "hello")])
    _, sha2 = bx.raw_messages_fingerprint([_Msg(1, 1, "a", "hello!")])
    assert sha1 != sha2


# ── Suffix convention ───────────────────────────────────────────────────────────


def test_debug_suffix_convention():
    assert bx.debug_suffix_for("main", "claude-opus-4-6") == ""
    # Matches the streaming path's hardcoded ".fable-5".
    assert bx.debug_suffix_for("compare", "claude-fable-5") == ".fable-5"


# ── Submit ──────────────────────────────────────────────────────────────────────


def test_submit_batch_persists_state_and_builds_requests(debug_dir):
    client = _FakeClient()
    state = bx.submit_batch(
        client, "2026-07-02",
        {"main": "claude-opus-4-6", "compare": "claude-fable-5"},
        "user content", (412, "deadbeef"),
    )
    assert state.batch_id == "msgbatch_001"
    assert state.raw_msg_count == 412
    assert bx.load_state("2026-07-02") == state

    reqs = client.messages.batches.created[0]["requests"]
    assert [r["custom_id"] for r in reqs] == ["main", "compare"]
    for r in reqs:
        params = r["params"]
        assert params["max_tokens"] == 128_000
        assert params["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert params["messages"][0]["content"] == "user content"
    assert reqs[0]["params"]["model"] == "claude-opus-4-6"
    assert reqs[1]["params"]["model"] == "claude-fable-5"


# ── Poll ────────────────────────────────────────────────────────────────────────


def test_poll_survives_connection_errors_then_ends():
    """Sleep/wake resilience: transient network failures retry, not raise."""
    client = _FakeClient()
    client.messages.batches.retrieve_script = [
        httpx.ConnectError("network down (laptop asleep)"),
        _Batch("b", "in_progress"),
        httpx.ReadTimeout("flaky"),
        _Batch("b", "ended"),
    ]
    notes = []
    batch = bx.poll_until_ended(
        client, "b", status_cb=lambda b, e, n: notes.append((b, n)),
    )
    assert batch.processing_status == "ended"
    error_notes = [n for b, n in notes if b is None]
    assert len(error_notes) == 2 and "重试" in error_notes[0]


def test_poll_raises_batch_not_found():
    import anthropic
    resp = httpx.Response(404, request=httpx.Request("GET", "http://x"))
    client = _FakeClient()
    client.messages.batches.retrieve_script = [
        anthropic.NotFoundError("nope", response=resp, body=None),
    ]
    with pytest.raises(bx.BatchNotFound):
        bx.poll_until_ended(client, "gone")


def test_poll_times_out():
    client = _FakeClient()
    # Always in_progress; max_wait 0 → second iteration trips the cap.
    client.messages.batches.retrieve_script = [_Batch("b", "in_progress")] * 5

    def _retrieve(batch_id):
        return _Batch(batch_id, "in_progress")

    client.messages.batches.retrieve = _retrieve
    with pytest.raises(bx.BatchTimeout):
        bx.poll_until_ended(client, "b", max_wait_s=0.0)


# ── Results ─────────────────────────────────────────────────────────────────────


def test_fetch_results_keyed_by_custom_id_out_of_order():
    client = _FakeClient()
    client.messages.batches.results_map["b"] = [
        _Result("compare", "succeeded", _Message("对比版")),
        _Result("main", "succeeded", _Message("主版")),
    ]
    results = bx.fetch_results(client, "b")
    assert set(results) == {"main", "compare"}
    assert results["main"].result.message.content[1].text == "主版"


def test_process_results_success_writes_sidecars_and_usage(debug_dir):
    usage_calls = []
    outcome, retryable = bx.process_results(
        "2026-07-02", "the input",
        {"main": "claude-opus-4-6", "compare": "claude-fable-5"},
        {
            "main": _Result("main", "succeeded", _Message("# 主版", usage={"input_tokens": 5})),
            "compare": _Result("compare", "succeeded", _Message("# 对比版")),
        },
        usage_cb=lambda cid, model, usage: usage_calls.append((cid, model, usage)),
    )
    assert not retryable and not outcome.errors
    assert outcome.reports["main"].markdown == "# 主版"
    assert outcome.reports["compare"].markdown == "# 对比版"

    day = debug_dir_for("2026-07-02")
    assert (day / "extract.md").read_text(encoding="utf-8") == "# 主版"
    assert (day / "extract.fable-5.md").read_text(encoding="utf-8") == "# 对比版"
    assert (day / "extract.thinking.md").exists()
    assert ("main", "claude-opus-4-6", {"input_tokens": 5}) in usage_calls


def test_process_results_refusal_is_terminal_error(debug_dir):
    outcome, retryable = bx.process_results(
        "2026-07-02", "input",
        {"main": "claude-opus-4-6"},
        {"main": _Result("main", "succeeded", _Message("", stop_reason="refusal"))},
    )
    assert not retryable
    assert "main" in outcome.errors
    assert (debug_dir_for("2026-07-02") / "extract.FAILED.json").exists()


def test_process_results_server_error_and_expired_are_retryable(debug_dir):
    outcome, retryable = bx.process_results(
        "2026-07-02", "input",
        {"main": "claude-opus-4-6", "compare": "claude-fable-5"},
        {
            "main": _Result("main", "errored", error_type="api_error"),
            "compare": _Result("compare", "expired"),
        },
    )
    assert retryable == {"main": "claude-opus-4-6", "compare": "claude-fable-5"}
    assert not outcome.errors


def test_process_results_invalid_request_is_terminal(debug_dir):
    outcome, retryable = bx.process_results(
        "2026-07-02", "input",
        {"main": "claude-opus-4-6"},
        {"main": _Result("main", "errored", error_type="invalid_request")},
    )
    assert not retryable
    assert "不可重试" in outcome.errors["main"]


# ── run_batch orchestration ─────────────────────────────────────────────────────


def _wire_success(client, batch_id, requests):
    client.messages.batches.results_map[batch_id] = [
        _Result(cid, "succeeded", _Message(f"# {cid}")) for cid in requests
    ]


def test_run_batch_fresh_submit_success(debug_dir):
    client = _FakeClient()
    requests = {"main": "claude-opus-4-6", "compare": "claude-fable-5"}
    _wire_success(client, "msgbatch_001", requests)

    outcome = bx.run_batch(
        client=client, date_str="2026-07-02", debug_text="input",
        user_content="uc", fingerprint=(2, "sha"), requests=requests,
    )
    assert set(outcome.reports) == {"main", "compare"}
    state = bx.load_state("2026-07-02")
    assert state.consumed is True
    assert state.batch_id == "msgbatch_001"
    # The bulky content snapshot is cleaned up once results are consumed.
    assert not bx.content_snapshot_path("2026-07-02").exists()


def test_run_batch_resume_uses_state_requests_and_skips_create(debug_dir):
    client = _FakeClient()
    state = _state(batch_id="msgbatch_777", requests={"main": "claude-opus-4-6"})
    bx.save_state(state)
    client.messages.batches.results_map["msgbatch_777"] = [
        _Result("main", "succeeded", _Message("# 续接")),
    ]

    outcome = bx.run_batch(
        client=client, date_str="2026-07-02", debug_text="input",
        user_content="uc", fingerprint=(412, "deadbeef"),
        # Caller's requests are IGNORED on resume — state's set wins.
        requests={"main": "claude-opus-4-6", "compare": "claude-fable-5"},
        state=state,
    )
    assert not client.messages.batches.created  # no new submission
    assert set(outcome.reports) == {"main"}
    assert bx.load_state("2026-07-02").consumed is True


def test_run_batch_retries_server_errors_once(debug_dir):
    client = _FakeClient()
    requests = {"main": "claude-opus-4-6", "compare": "claude-fable-5"}
    # Original batch: main ok, compare server-errored.
    client.messages.batches.results_map["msgbatch_001"] = [
        _Result("main", "succeeded", _Message("# main")),
        _Result("compare", "errored", error_type="api_error"),
    ]
    # Retry batch: compare succeeds.
    client.messages.batches.results_map["msgbatch_002"] = [
        _Result("compare", "succeeded", _Message("# compare 重试")),
    ]

    outcome = bx.run_batch(
        client=client, date_str="2026-07-02", debug_text="input",
        user_content="uc", fingerprint=(2, "sha"), requests=requests,
    )
    assert outcome.reports["compare"].markdown == "# compare 重试"
    assert len(client.messages.batches.created) == 2
    retry_reqs = client.messages.batches.created[1]["requests"]
    assert [r["custom_id"] for r in retry_reqs] == ["compare"]
    # State keeps pointing at the ORIGINAL batch throughout.
    assert bx.load_state("2026-07-02").batch_id == "msgbatch_001"


def test_run_batch_retry_still_failing_lands_in_errors(debug_dir):
    client = _FakeClient()
    requests = {"main": "claude-opus-4-6"}
    client.messages.batches.results_map["msgbatch_001"] = [
        _Result("main", "errored", error_type="api_error"),
    ]
    client.messages.batches.results_map["msgbatch_002"] = [
        _Result("main", "errored", error_type="api_error"),
    ]
    outcome = bx.run_batch(
        client=client, date_str="2026-07-02", debug_text="input",
        user_content="uc", fingerprint=(1, "sha"), requests=requests,
    )
    assert not outcome.reports
    assert "main" in outcome.errors


def test_run_batch_cancels_retry_batch_on_interrupt(debug_dir):
    client = _FakeClient()
    requests = {"main": "claude-opus-4-6"}
    client.messages.batches.results_map["msgbatch_001"] = [
        _Result("main", "errored", error_type="api_error"),
    ]

    real_retrieve = client.messages.batches.retrieve

    def _retrieve(batch_id):
        if batch_id == "msgbatch_002":
            raise KeyboardInterrupt
        return real_retrieve(batch_id)

    client.messages.batches.retrieve = _retrieve
    with pytest.raises(KeyboardInterrupt):
        bx.run_batch(
            client=client, date_str="2026-07-02", debug_text="input",
            user_content="uc", fingerprint=(1, "sha"), requests=requests,
        )
    assert client.messages.batches.cancelled == ["msgbatch_002"]
    # Original batch stays resumable.
    assert bx.load_state("2026-07-02").consumed is False
