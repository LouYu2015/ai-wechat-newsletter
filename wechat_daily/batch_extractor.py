"""Batch API report extraction with crash/sleep-safe resume.

The daily report's two generation calls (main Fable + Opus compare) go into
ONE Message Batch — every token bills at 50% of standard prices. Batches
usually finish within minutes to tens of minutes (hard server cap 24h), so
this module is built around three interruption scenarios:

- **机器休眠**: the batch runs server-side; polling just resumes after wake.
  Every poll iteration is individually fault-tolerant (network errors back
  off and retry forever within the wall-clock cap).
- **进程退出（Ctrl-C / 崩溃）**: the batch id is persisted to
  ``debug/{date}/batch_state.json`` right after submission. Re-running the
  same command resumes polling instead of resubmitting.
- **已消费后重跑**: results stay retrievable server-side for 29 days;
  a consumed state file can be re-fetched at zero cost.

State file schema (``version`` bumps on breaking changes so future code can
convert or reject old files explicitly)::

    {
      "version": 1,
      "batch_id": "msgbatch_…",
      "date": "2026-07-03",
      "submitted_at": "2026-07-03T21:47:03+08:00",
      "raw_msg_count": 412,
      "raw_msg_sha256": "…",
      "requests": {"main": "claude-fable-5", "compare": "claude-opus-4-6"},
      "consumed": false
    }

The fingerprint is taken over the *raw* extracted messages (before link
enrichment): DeepSeek link summaries are nondeterministic, so hashing the
final prompt would never match across runs. ``raw_msg_count`` is redundant
with the hash for equality checks — it exists purely so the mismatch warning
can say "提交时 412 条 → 现在 450 条" and be instantly interpretable.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import pathlib
import time
from typing import Callable

import httpx

from wechat_daily import config, llm_extractor, models

STATE_VERSION = 1
POLL_INTERVAL_S = 30.0
# Server-side cap is 24h; give one extra hour of polling slack before giving up.
MAX_WAIT_S = 25 * 3600


class BatchStateError(Exception):
    """State file exists but is unusable (unknown version / corrupt JSON)."""


class BatchNotFound(Exception):
    """The persisted batch id no longer resolves server-side (deleted/wrong org)."""


class BatchTimeout(Exception):
    """Batch didn't reach ``ended`` within MAX_WAIT_S."""


# ── State file ───────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class BatchState:
    """Persisted snapshot of one in-flight (or consumed) report batch."""

    batch_id: str
    date: str
    submitted_at: str
    raw_msg_count: int
    raw_msg_sha256: str
    requests: dict[str, str]  # custom_id → model
    consumed: bool = False
    version: int = STATE_VERSION


def state_path(date_str: str) -> pathlib.Path:
    return config.debug_dir_for(date_str) / "batch_state.json"


def content_snapshot_path(date_str: str) -> pathlib.Path:
    """Full submitted ``user_content`` (block list incl. base64 images).

    Written at submit time so a resumed run's RETRY round can replay input
    byte-identical to the original submission — the text-only
    ``batch_input.txt`` audit sidecar can't serve that purpose (images are
    reduced to placeholders there). Large (base64 images), therefore deleted
    by :func:`mark_consumed`; it only lives while the batch is in flight.
    """
    return config.debug_dir_for(date_str) / "batch_content.json"


def save_content_snapshot(date_str: str, user_content) -> None:
    path = content_snapshot_path(date_str)
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(
        json.dumps(user_content, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def load_content_snapshot(date_str: str):
    """Return the submitted user_content, or ``None`` if absent/corrupt
    (caller falls back to a local rebuild without link summaries)."""
    path = content_snapshot_path(date_str)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def snapshot_debug_text(user_content) -> str:
    """Reconstruct the flat debug text from a content snapshot.

    Mirrors ``build_extract_user_content``'s *debug_text*: the concatenation
    of all text blocks (images contribute nothing). Accepts the flat-string
    form too, for completeness.
    """
    if isinstance(user_content, str):
        return user_content
    return "".join(
        b.get("text", "") for b in user_content if b.get("type") == "text"
    )


def load_state(date_str: str) -> BatchState | None:
    """Load the date's batch state; ``None`` if absent.

    Raises :class:`BatchStateError` on corrupt JSON or a version this code
    doesn't understand — the CLI surfaces that and offers a fresh submission
    (the file is never silently ignored, so a typo'd upgrade can't cause
    double-billing without the user seeing it).
    """
    path = state_path(date_str)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BatchStateError(f"状态文件损坏（{path}）：{e}") from e
    version = data.get("version")
    if version != STATE_VERSION:
        raise BatchStateError(
            f"状态文件版本 {version!r} 与当前程序支持的 {STATE_VERSION} 不符（{path}）。"
            "请升级程序或删除该文件后重新提交。"
        )
    known = {f for f in BatchState.__dataclass_fields__}
    return BatchState(**{k: v for k, v in data.items() if k in known})


def save_state(state: BatchState) -> None:
    path = state_path(state.date)
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_text(
        json.dumps(dataclasses.asdict(state), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def mark_consumed(state: BatchState) -> None:
    """Flag the batch as consumed and drop the bulky content snapshot.

    The snapshot exists only to make retries byte-identical while the batch
    is in flight; once results are processed it's dead weight (megabytes of
    base64 images). The text audit sidecar (``batch_input.txt``) stays.
    """
    state.consumed = True
    save_state(state)
    content_snapshot_path(state.date).unlink(missing_ok=True)


# ── Raw-message fingerprint ─────────────────────────────────────────────────────


def raw_messages_fingerprint(messages: list) -> tuple[int, str]:
    """``(count, sha256)`` over the raw extracted messages.

    Uses only fields that are stable across runs for the same day
    (``create_time`` / ``local_type`` / ``sender_wxid`` / ``content``) —
    NOT ``link_context``, which link enrichment fills with nondeterministic
    LLM output. Compute-order note: content is untouched by enrichment, so
    calling this before or after enrichment yields the same value; the CLI
    still calls it first thing for clarity.
    """
    h = hashlib.sha256()
    for m in messages:
        h.update(
            f"{m.create_time}\x00{m.local_type}\x00{m.sender_wxid}\x00{m.content}\x1e"
            .encode("utf-8")
        )
    return len(messages), h.hexdigest()


# ── Debug-suffix convention ─────────────────────────────────────────────────────


def debug_suffix_for(custom_id: str, model: str) -> str:
    """Sidecar suffix per request: main is canonical (un-suffixed, feeds
    next-day continuity); every other id gets ``.{model minus claude-}`` —
    matching the streaming path's hardcoded ``.opus-4-6``."""
    if custom_id == "main":
        return ""
    return "." + model.removeprefix("claude-")


# ── Client plumbing ─────────────────────────────────────────────────────────────


def make_client(api_key: str):
    """Anthropic client tuned for batch control-plane calls.

    Individual calls (create/retrieve/results) are small; 120s is generous.
    The long wait happens in *our* poll loop, not in one HTTP request.
    """
    import anthropic
    return anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(120.0, connect=15.0),
    )


def _is_retryable(exc: Exception) -> bool:
    """Poll-loop error classification: network blips and server-side trouble
    retry forever (sleep/wake resilience); client errors surface immediately."""
    import anthropic
    if isinstance(exc, (httpx.TransportError, anthropic.APIConnectionError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


# ── Batch operations ────────────────────────────────────────────────────────────


def submit_batch(
    client,
    date_str: str,
    requests: dict[str, str],
    user_content,
    fingerprint: tuple[int, str],
) -> BatchState:
    """Create the batch and persist the state file; returns the new state.

    *requests* maps custom_id → model. All requests share *user_content*
    (identical prompt; only the model differs — same invariant as the
    streaming AB-test path).
    """
    batch = client.messages.batches.create(
        requests=[
            {
                "custom_id": custom_id,
                "params": llm_extractor.build_request_params(model, user_content),
            }
            for custom_id, model in requests.items()
        ]
    )
    # Snapshot the exact submitted content so a resumed run's retry round can
    # replay it byte-identical (deleted again by mark_consumed).
    save_content_snapshot(date_str, user_content)
    count, sha = fingerprint
    state = BatchState(
        batch_id=batch.id,
        date=date_str,
        submitted_at=datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        raw_msg_count=count,
        raw_msg_sha256=sha,
        requests=dict(requests),
    )
    save_state(state)
    return state


def cancel_batch(client, batch_id: str) -> None:
    """Best-effort cancel (used by --resubmit and retry-cleanup); never raises."""
    try:
        client.messages.batches.cancel(batch_id)
    except Exception:
        pass


def poll_until_ended(
    client,
    batch_id: str,
    *,
    poll_interval: float = POLL_INTERVAL_S,
    max_wait_s: float = MAX_WAIT_S,
    status_cb: Callable[[object | None, float, str], None] | None = None,
):
    """Poll until ``processing_status == "ended"``; return the final batch.

    Sleep/wake-safe by construction: elapsed time uses the wall clock
    (``time.time()``), and every retrieve is wrapped so connection errors —
    exactly what a wake-up or a flaky network produces — just wait one
    interval and try again. Only client-side errors (e.g. 404 = the batch id
    no longer exists) raise immediately, as :class:`BatchNotFound`.

    *status_cb(batch_or_none, elapsed_s, note)* fires once per iteration —
    ``batch_or_none`` is ``None`` when the iteration failed (note carries the
    error text).
    """
    import anthropic

    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > max_wait_s:
            raise BatchTimeout(
                f"批次 {batch_id} 轮询超过 {max_wait_s / 3600:.0f} 小时仍未结束"
            )
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except anthropic.NotFoundError as e:
            raise BatchNotFound(f"批次 {batch_id} 不存在（可能已删除或换了 API Key）") from e
        except Exception as e:  # noqa: BLE001 — classified below
            if not _is_retryable(e):
                raise
            if status_cb:
                status_cb(None, elapsed, f"网络错误，{poll_interval:.0f}s 后重试：{e}")
            time.sleep(poll_interval)
            continue

        if status_cb:
            status_cb(batch, elapsed, "")
        if batch.processing_status == "ended":
            return batch
        time.sleep(poll_interval)


def fetch_results(client, batch_id: str, *, max_attempts: int = 5) -> dict[str, object]:
    """Fetch all results, keyed by custom_id (stream order is NOT request
    order — always key, never index). Network errors retry a few times;
    a partially-consumed iterator is re-fetched from scratch."""
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return {r.custom_id: r for r in client.messages.batches.results(batch_id)}
        except Exception as e:  # noqa: BLE001 — classified below
            if not _is_retryable(e):
                raise
            last = e
            if attempt < max_attempts:
                time.sleep(POLL_INTERVAL_S)
    raise last  # type: ignore[misc]


# ── Orchestration ───────────────────────────────────────────────────────────────


@dataclasses.dataclass
class BatchOutcome:
    """What one full batch round produced, per custom_id."""

    reports: dict[str, models.DailyReport] = dataclasses.field(default_factory=dict)
    errors: dict[str, str] = dataclasses.field(default_factory=dict)  # custom_id → reason


def process_results(
    date_str: str,
    debug_text: str,
    requests: dict[str, str],
    results: dict[str, object],
    *,
    usage_cb: Callable[[str, str, object], None] | None = None,
) -> tuple[BatchOutcome, dict[str, str]]:
    """Turn raw batch results into reports + a retryable-failure subset.

    Returns ``(outcome, retryable)`` where *retryable* maps custom_id → model
    for requests that failed with a server-side error or expired (safe to
    resubmit). Validation failures and refusal/truncation/empty responses are
    terminal — they land in ``outcome.errors``.

    *usage_cb(custom_id, model, usage)* fires per succeeded request.
    """
    outcome = BatchOutcome()
    retryable: dict[str, str] = {}

    for custom_id, model in requests.items():
        result = results.get(custom_id)
        suffix = debug_suffix_for(custom_id, model)
        if result is None:
            # Shouldn't happen (server returns one result per request) —
            # treat as retryable rather than dying.
            retryable[custom_id] = model
            continue

        rtype = result.result.type
        if rtype == "succeeded":
            message = result.result.message
            try:
                markdown = llm_extractor.finalize_response(
                    date_str, debug_text, message, suffix=suffix,
                )
            except llm_extractor.ExtractionError as e:
                outcome.errors[custom_id] = str(e)
                continue
            if usage_cb:
                usage_cb(custom_id, model, getattr(message, "usage", None))
            outcome.reports[custom_id] = models.DailyReport(date=date_str, markdown=markdown)
        elif rtype == "errored":
            err = result.result.error
            err_type = getattr(getattr(err, "error", err), "type", "")
            if err_type == "invalid_request":
                outcome.errors[custom_id] = f"请求校验失败（不可重试）：{err}"
            else:
                retryable[custom_id] = model
        elif rtype == "expired":
            retryable[custom_id] = model
        elif rtype == "canceled":
            outcome.errors[custom_id] = "请求已被取消"
        else:
            outcome.errors[custom_id] = f"未知结果类型：{rtype}"

    return outcome, retryable


def run_batch(
    *,
    client,
    date_str: str,
    debug_text: str,
    user_content,
    fingerprint: tuple[int, str],
    requests: dict[str, str],
    state: BatchState | None = None,
    status_cb: Callable[[object | None, float, str], None] | None = None,
    usage_cb: Callable[[str, str, object], None] | None = None,
    note_cb: Callable[[str], None] | None = None,
) -> BatchOutcome:
    """Submit (or resume) → poll → fetch → finalize, with one retry round.

    *state* is a pre-loaded resumable state (same date, caller already
    decided to continue it); ``None`` submits a fresh batch. The state file
    is marked consumed after results are processed.

    Retry semantics: server-errored/expired requests are resubmitted ONCE as
    a fresh batch. The state file keeps pointing at the ORIGINAL batch —
    results are re-fetchable for 29 days, so a crash during the retry round
    resumes cleanly from the original (already-succeeded requests
    re-finalize idempotently; failed ones retry again). The ephemeral retry
    batch is cancelled best-effort if the retry round itself dies.
    """
    if state is None:
        state = submit_batch(client, date_str, requests, user_content, fingerprint)
        if note_cb:
            note_cb(f"已提交批次 {state.batch_id}（{len(requests)} 个请求，5 折计费）")
    else:
        if note_cb:
            note_cb(f"续接批次 {state.batch_id}（提交于 {state.submitted_at}）")
        requests = dict(state.requests)

    batch = poll_until_ended(client, state.batch_id, status_cb=status_cb)
    results = fetch_results(client, batch.id)
    outcome, retryable = process_results(
        date_str, debug_text, requests, results, usage_cb=usage_cb,
    )

    if retryable:
        if note_cb:
            note_cb(
                "以下请求服务端失败/过期，重提交一次："
                + ", ".join(f"{cid}({m})" for cid, m in retryable.items())
            )
        retry_batch = client.messages.batches.create(
            requests=[
                {
                    "custom_id": cid,
                    "params": llm_extractor.build_request_params(model, user_content),
                }
                for cid, model in retryable.items()
            ]
        )
        try:
            poll_until_ended(client, retry_batch.id, status_cb=status_cb)
            retry_results = fetch_results(client, retry_batch.id)
        except BaseException:
            # Includes KeyboardInterrupt: don't leave the orphan retry batch
            # billing away — the state file still points at the original.
            cancel_batch(client, retry_batch.id)
            raise
        retry_outcome, still_failing = process_results(
            date_str, debug_text, retryable, retry_results, usage_cb=usage_cb,
        )
        outcome.reports.update(retry_outcome.reports)
        outcome.errors.update(retry_outcome.errors)
        for cid, model in still_failing.items():
            outcome.errors[cid] = "重试后仍失败（服务端错误/过期）"

    mark_consumed(state)
    return outcome
