"""Caption WeChat group images with Gemini for the DeepSeek (text-only) path.

DeepSeek V4 Pro has no vision, so in the AB-test compare report images would
otherwise reach it as bare ``[图片]`` placeholders (see
``llm_extractor.extract_report_deepseek``). This module decodes each image
once, asks a vision model (Gemini 3 Flash) to describe it *with surrounding
chat context*, and returns ``{image_md5: caption}``. The caller injects those
into the flat chat history via ``format_tokenized_messages(.., captions=..)``
so they appear as ``[图片：…]`` — DeepSeek-only; the Claude path keeps native
inline images untouched.

Captions are deduped and cached by ``image_md5`` within a single run (an md5
is the decrypted-file identity, so the same image always maps to the same
caption). Privacy rules mirror the link-summary prompt: real names, WeChat
nicknames, phone numbers, QR codes and ID documents are omitted/generalized.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import GEMINI_CAPTION_MODEL
from .message_parser import MSG_IMAGE, MSG_SYSTEM, MSG_TAP, Message


# (current, total, label) — fires once per image as its caption resolves.
ProgressCB = Callable[[int, int, str], None]
# (usage_dict, duration_s, input_chars) — one per successful Gemini call.
UsageCB = Callable[[dict, float, int], None]

# Empty / unrecognizable images: the model is told to reply with a lone "-".
_SKIP_TOKEN = "-"

_CAPTION_PROMPT = """\
你正在为「微信 AI 技术讨论群日报」描述一张群里分享的图片，供日报作者理解图片内容。

<chat_context> 是图片前后的群聊消息（发送者已匿名为 token），帮你判断图片在讨论什么；图片本身在随附的图像里。

任务：用一段简洁中文描述这张图片，纯文本一段，不超过 150 字，无 Markdown。
- 文章/帖子截图：概括标题与核心观点、关键数据、结论。
- 产品界面/演示截图：说明是什么产品、在演示什么功能或结果。
- 聊天记录截图：概括在讨论什么主题（不要照搬人名）。
- 表情包/梗图：简述图意即可。
- 紧扣图片本身，不要复述 <chat_context> 里的群友发言、不要评价聊天。

隐私：图片中若出现真实人名、微信昵称、手机号、二维码、身份证件等隐私信息，一律省略或泛化，不要写进描述。
如果图片无法识别或没有有效信息，只回复一个减号「{skip}」。

<chat_context>
{context}
</chat_context>

直接输出图片描述。
"""


@dataclass
class CaptionStats:
    total: int = 0        # unique images (by md5) seen
    captioned: int = 0    # produced a usable caption
    skipped: int = 0      # model returned "-" / empty (unrecognizable)
    failed: int = 0       # decode or API error


def caption_images(
    messages: list[Message],
    decoder,                    # ImageDecoder; duck-typed `.decode(md5) -> Path | None`
    api_key: str,
    *,
    progress_cb: ProgressCB | None = None,
    usage_cb: UsageCB | None = None,
    max_workers: int = 5,
) -> tuple[dict[str, str], CaptionStats]:
    """Return ``({image_md5: caption}, stats)`` for images in *messages*.

    Each unique ``image_md5`` is captioned at most once. Images that fail to
    decode, error out, or come back unrecognizable are simply absent from the
    returned dict — the caller then renders a bare ``[图片]`` for them.
    Failures are contained per image; one bad image never aborts the batch.
    """
    targets = _collect_image_targets(messages)
    stats = CaptionStats(total=len(targets))
    if not targets:
        return {}, stats

    from google import genai as google_genai
    from google.genai import types

    client = google_genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=120_000),
    )

    captions: dict[str, str] = {}

    def _one(target: tuple[str, int]) -> tuple[str, str | None, object, float, int]:
        """Decode + caption a single image. Returns (md5, caption|None, usage, dur, chars)."""
        import time

        md5, host_idx = target
        jpeg = decoder.decode(md5)
        if jpeg is None:
            return md5, None, None, 0.0, 0

        context = _build_context(messages, host_idx)
        prompt = _CAPTION_PROMPT.format(context=context or "（无相邻聊天）", skip=_SKIP_TOKEN)

        t0 = time.perf_counter()
        # Contain per-image failures (API error, safety block that makes
        # `.text` raise): one bad image must not abort the whole batch — it's
        # reported as failed (caption=None) and the chat keeps a bare [图片].
        try:
            response = client.models.generate_content(
                model=GEMINI_CAPTION_MODEL,
                contents=[
                    types.Part.from_bytes(data=jpeg.read_bytes(), mime_type="image/jpeg"),
                    types.Part.from_text(text=prompt),
                ],
                config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=1024),
            )
            text = (response.text or "").strip()
            usage = _usage_dict(getattr(response, "usage_metadata", None))
        except Exception:
            return md5, None, None, 0.0, 0
        duration_s = time.perf_counter() - t0
        return md5, text, usage, duration_s, len(prompt)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for md5, text, usage, duration_s, chars in pool.map(_one, targets):
            done += 1
            label = _label(messages, md5)
            try:
                if text is None:
                    stats.failed += 1
                elif not text or text.strip(" 。.-") == "" or text.strip() == _SKIP_TOKEN:
                    stats.skipped += 1
                else:
                    captions[md5] = text
                    stats.captioned += 1
                    if usage_cb and usage is not None:
                        usage_cb(usage, duration_s, chars)
            finally:
                if progress_cb:
                    progress_cb(done, stats.total, label)

    return captions, stats


def count_image_targets(messages: list[Message]) -> int:
    return len(_collect_image_targets(messages))


def _collect_image_targets(messages: list[Message]) -> list[tuple[str, int]]:
    """Unique ``(image_md5, first_host_index)`` for image messages, in order."""
    targets: list[tuple[str, int]] = []
    seen: set[str] = set()
    for idx, msg in enumerate(messages):
        if msg.local_type == MSG_IMAGE and msg.image_md5 and msg.image_md5 not in seen:
            seen.add(msg.image_md5)
            targets.append((msg.image_md5, idx))
    return targets


def _build_context(messages: list[Message], host_idx: int, window: int = 10) -> str:
    """Format ±window non-system messages around the image, senders as tokens.

    Senders are already anonymized tokens by this stage, so no extra letter
    mapping is needed. The host image line is marked ``[本图]``.
    """
    start = max(0, host_idx - window)
    end = min(len(messages), host_idx + window + 1)
    lines: list[str] = []
    for j in range(start, end):
        m = messages[j]
        if m.local_type in (MSG_SYSTEM, MSG_TAP):
            continue
        content = (m.content or "").strip()
        if not content:
            continue
        ts = datetime.fromtimestamp(m.create_time).strftime("%H:%M")
        marker = "[本图] " if j == host_idx else ""
        if not m.sender_wxid:
            lines.append(f"[{ts}] {marker}{content}")
        else:
            lines.append(f"[{ts}] {m.sender_wxid}: {marker}{content}")
    return "\n".join(lines)


def _label(messages: list[Message], md5: str) -> str:
    """Short progress label: the token + time of the image's first occurrence."""
    for m in messages:
        if m.local_type == MSG_IMAGE and m.image_md5 == md5:
            ts = datetime.fromtimestamp(m.create_time).strftime("%H:%M")
            who = m.sender_wxid or "?"
            return f"{ts} {who}"
    return md5[:8]


def _usage_dict(usage_metadata) -> dict | None:
    """Coerce Gemini ``usage_metadata`` → plain dict for cost_tracker.

    Keeps the native field names (prompt_token_count / candidates_token_count /
    cached_content_token_count) so ``cost_tracker.usage_to_dict`` maps them.
    """
    if usage_metadata is None:
        return None
    return {
        "prompt_token_count": getattr(usage_metadata, "prompt_token_count", 0) or 0,
        "candidates_token_count": getattr(usage_metadata, "candidates_token_count", 0) or 0,
        "cached_content_token_count": getattr(usage_metadata, "cached_content_token_count", 0) or 0,
    }
