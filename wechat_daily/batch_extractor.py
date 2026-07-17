"""Batch API report extraction with crash/sleep-safe resume.

The daily report's two generation calls (main Fable + Opus compare) go into
ONE Message Batch — every token bills at 50% of standard prices. Batches
usually finish within minutes to tens of minutes (hard server cap 24h), so
this module is built around three interruption scenarios:

- **机器休眠**: the batch runs server-side; polling just resumes after wake.
  Every poll iteration is individually fault-tolerant (network errors back
  off and retry forever, capped by active-poll count so sleep doesn't count);
  a detected wake-up or a run of failures rebuilds the dead connection pool.
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
# 计的是"活跃轮询次数"（MAX_WAIT_S / poll_interval，≈3000 次），不是墙钟——
# 合盖睡一夜期间根本不轮询，那段时间不该算进上限（详见 poll_until_ended）。
MAX_WAIT_S = 25 * 3600
# retrieve 响应很小，轮询里单独用这个更短的超时快速失败（见 _retrieve_batch）。
POLL_RETRIEVE_TIMEOUT_S = 30.0


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
    return "".join(b.get("text", "") for b in user_content if b.get("type") == "text")


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
            f"{m.create_time}\x00{m.local_type}\x00{m.sender_wxid}\x00{m.content}\x1e".encode(
                "utf-8"
            )
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

    create（大请求）和 results（要下完整输出）走 SDK 默认重试 + 这里的 120s——
    宽裕即可，长等待发生在*我们*的轮询循环里，不在单个 HTTP 请求里。轮询里的
    retrieve 另有一套快速失败配置（见 :func:`_retrieve_batch`），不受此处影响。
    """
    import anthropic

    return anthropic.Anthropic(
        api_key=api_key,
        timeout=httpx.Timeout(120.0, connect=15.0),
    )


def _retrieve_batch(client, batch_id: str):
    """轮询专用的 retrieve：禁用 SDK 内部重试、把超时压到 30s。

    合盖睡醒后 httpx 连接池里全是死连接，而 SDK 默认自带 2 次内部退避重试、
    读超时 120s——一次 retrieve 会经历"死连接→读超时→内部退避→又摸到死连接"，
    单次调用能挂 6–8 分钟，期间 status_cb 完全不更新，界面像假死。retrieve 的
    响应很小，这里 max_retries=0 + 30s 超时让死连接尽快暴露成一个可重试错误，
    交给外层轮询循环处理（必要时重建整个连接池）。

    create（大请求）和 results（要下完整输出）不走这里，保持 SDK 默认语义。
    """
    with_options = getattr(client, "with_options", None)
    if with_options is not None:
        client = with_options(
            max_retries=0,
            timeout=httpx.Timeout(POLL_RETRIEVE_TIMEOUT_S, connect=15.0),
        )
    return client.messages.batches.retrieve(batch_id)


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
        submitted_at=datetime.datetime.now(datetime.timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds"),
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
    rebuild_client: Callable[[], object] | None = None,
):
    """Poll until ``processing_status == "ended"``; return the final batch.

    Sleep/wake-safe by construction. Every retrieve is fast-failing
    (:func:`_retrieve_batch`) and wrapped so connection errors — exactly what a
    wake-up or a flaky network produces — just wait one interval and try again.
    Only client-side errors (e.g. 404 = the batch id no longer exists) raise
    immediately, as :class:`BatchNotFound`.

    两处睡眠加固（*rebuild_client* 提供时才生效——它造一个全新的
    ``anthropic.Anthropic``，即"新进程=新连接池"那招，唯一能让死连接池自愈的动作）：

    - 两轮迭代的实际墙钟间隔 > 5×poll_interval：判定刚从系统睡眠恢复，旧连接池
      多半全是死连接，主动丢弃重建；
    - 连续失败 ≥2 轮：连接池不会自愈，重建。

    超时上限按"活跃轮询次数"（max_wait_s/poll_interval）计，不看墙钟——睡眠期间不
    轮询、自然不计入，长挂机不会误触发 :class:`BatchTimeout`。

    *status_cb(batch_or_none, elapsed_s, note)* fires once per iteration —
    ``batch_or_none`` is ``None`` when the iteration failed or a rebuild fired
    (note carries the error/rebuild text).
    """
    import anthropic

    max_iterations = max(1, int(max_wait_s / poll_interval))
    start = time.time()
    last_tick = start  # 上一轮迭代的墙钟时刻，用来识别系统睡眠造成的大间隔
    iterations = 0
    consecutive_failures = 0

    def _rebuild(note: str) -> None:
        nonlocal client
        if rebuild_client is None:
            return
        try:
            client.close()
        except Exception:
            pass  # best-effort：旧连接可能已经死了，close 报错无所谓
        client = rebuild_client()
        if status_cb:
            status_cb(None, time.time() - start, note)

    while True:
        iterations += 1
        if iterations > max_iterations:
            raise BatchTimeout(
                f"批次 {batch_id} 已活跃轮询 {max_iterations} 次"
                f"（约 {max_wait_s / 3600:.0f} 小时）仍未结束"
            )
        now = time.time()
        gap = now - last_tick
        last_tick = now
        if iterations > 1 and gap > 5 * poll_interval:
            _rebuild(f"检测到系统休眠恢复（间隔 {gap / 60:.0f} 分钟），已重建连接")

        elapsed = now - start
        try:
            batch = _retrieve_batch(client, batch_id)
        except anthropic.NotFoundError as e:
            raise BatchNotFound(f"批次 {batch_id} 不存在（可能已删除或换了 API Key）") from e
        except Exception as e:  # noqa: BLE001 — classified below
            if not _is_retryable(e):
                raise
            consecutive_failures += 1
            if status_cb:
                status_cb(
                    None,
                    elapsed,
                    f"网络错误（连续第 {consecutive_failures} 次），"
                    f"{poll_interval:.0f}s 后重试：{e}",
                )
            if consecutive_failures >= 2:
                _rebuild(f"连续 {consecutive_failures} 次轮询失败，已重建连接")
            time.sleep(poll_interval)
            continue

        consecutive_failures = 0
        if status_cb:
            status_cb(batch, elapsed, "")
        if batch.processing_status == "ended":
            return batch
        time.sleep(poll_interval)


def fetch_results(
    client,
    batch_id: str,
    *,
    max_attempts: int = 5,
    note_cb: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Fetch all results, keyed by custom_id (stream order is NOT request
    order — always key, never index). Network errors retry a few times;
    a partially-consumed iterator is re-fetched from scratch.

    *note_cb* 报告每次重试（默认下载走 SDK 全套重试 + 120s 超时，故这里的
    重试属于兜底）——否则重试完全静默，用户只看到界面卡着不动。
    """
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return {r.custom_id: r for r in client.messages.batches.results(batch_id)}
        except Exception as e:  # noqa: BLE001 — classified below
            if not _is_retryable(e):
                raise
            last = e
            if attempt < max_attempts:
                if note_cb:
                    note_cb(
                        f"取结果失败（第 {attempt}/{max_attempts} 次），"
                        f"{POLL_INTERVAL_S:.0f}s 后重试：{e}"
                    )
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
                    date_str,
                    debug_text,
                    message,
                    suffix=suffix,
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
    rebuild_client: Callable[[], object] | None = None,
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

    # poll_until_ended 重建连接时（睡眠恢复/连续失败），必须同步 run_batch 自己的
    # client 引用：否则轮询结束后的 fetch_results / 重试轮 create 仍握着那个已被
    # close 的旧 client，会抛 RuntimeError（不在 _is_retryable 名单里）直接崩掉——
    # 而重建恰恰发生在批次即将完成、马上要取结果的时刻，比不重建更糟。
    def _swap_client():
        nonlocal client
        client = rebuild_client()
        return client

    swap = _swap_client if rebuild_client is not None else None

    batch = poll_until_ended(client, state.batch_id, status_cb=status_cb, rebuild_client=swap)
    results = fetch_results(client, batch.id, note_cb=note_cb)
    outcome, retryable = process_results(
        date_str,
        debug_text,
        requests,
        results,
        usage_cb=usage_cb,
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
            poll_until_ended(client, retry_batch.id, status_cb=status_cb, rebuild_client=swap)
            retry_results = fetch_results(client, retry_batch.id, note_cb=note_cb)
        except BaseException:
            # Includes KeyboardInterrupt: don't leave the orphan retry batch
            # billing away — the state file still points at the original.
            cancel_batch(client, retry_batch.id)
            raise
        retry_outcome, still_failing = process_results(
            date_str,
            debug_text,
            retryable,
            retry_results,
            usage_cb=usage_cb,
        )
        outcome.reports.update(retry_outcome.reports)
        outcome.errors.update(retry_outcome.errors)
        for cid, model in still_failing.items():
            outcome.errors[cid] = "重试后仍失败（服务端错误/过期）"

    mark_consumed(state)
    return outcome
