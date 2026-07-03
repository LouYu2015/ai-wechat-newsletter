#!/usr/bin/env python3
"""命令行调用 DeepSeek，方便在生成日报时用 DeepSeek 润色文章。

读取 `.env` 里的 `DEEPSEEK_API_KEY`，把 prompt 发给 DeepSeek 的
OpenAI 兼容接口（https://api.deepseek.com），把回复打印到 stdout。
仅用标准库（urllib），无需额外依赖。

用法示例：
    # 直接传 prompt
    python scripts/ask_deepseek.py "用更口语的语气润色这段话：……"

    # 从 stdin 读入正文（适合管道 / 长文）
    cat data/draft/today.md | python scripts/ask_deepseek.py \
        --system "你是中文编辑，润色但不改变事实" -

    # 关闭流式、指定经济款模型
    python scripts/ask_deepseek.py --model deepseek-v4-flash --no-stream "证明……"

参数：
    prompt            用户消息；传 "-" 或省略则从 stdin 读取
    --system TEXT     system prompt（可选）
    --model NAME      默认 deepseek-v4-pro（经济款用 deepseek-v4-flash）
    --temperature F   默认 1.0
    --no-thinking     关闭 thinking 模式（默认开启 V4 Pro 推理）
    --no-stream       一次性返回（默认流式输出，边生成边打印）

回复正文打印到 stdout；运行信息（用量等）打印到 stderr。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from wechat_daily.config import get_deepseek_key

API_URL = "https://api.deepseek.com/chat/completions"


def build_messages(prompt: str, system: str | None) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def request_stream(payload: dict, key: str) -> str:
    """流式请求：边收边打印到 stdout，返回完整正文。"""
    payload = {**payload, "stream": True}
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    chunks: list[str] = []
    with urllib.request.urlopen(req) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"]
            except (json.JSONDecodeError, KeyError, IndexError):
                continue
            piece = delta.get("content") or ""
            if piece:
                chunks.append(piece)
                sys.stdout.write(piece)
                sys.stdout.flush()
    sys.stdout.write("\n")
    return "".join(chunks)


def request_once(payload: dict, key: str) -> str:
    """非流式请求：返回完整正文。"""
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    usage = body.get("usage")
    if usage:
        print(f"[usage] {usage}", file=sys.stderr)
    text = body["choices"][0]["message"]["content"]
    print(text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="命令行调用 DeepSeek 并查看回复（用于日报润色）。",
    )
    parser.add_argument(
        "prompt", nargs="?", default="-", help="用户消息；'-' 或省略则从 stdin 读取"
    )
    parser.add_argument("--system", help="system prompt（可选）")
    parser.add_argument(
        "--model",
        default="deepseek-v4-pro",
        help="模型，默认 deepseek-v4-pro（经济款用 deepseek-v4-flash）",
    )
    parser.add_argument("--temperature", type=float, default=1.0, help="采样温度，默认 1.0")
    parser.add_argument(
        "--no-thinking", action="store_true", help="关闭 thinking 模式（默认开启 V4 Pro 推理）"
    )
    parser.add_argument("--no-stream", action="store_true", help="一次性返回（默认流式）")
    args = parser.parse_args()

    key = get_deepseek_key()
    if not key:
        print("错误：.env 里缺少 DEEPSEEK_API_KEY", file=sys.stderr)
        return 1

    prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt.strip():
        print("错误：prompt 为空", file=sys.stderr)
        return 1

    payload = {
        "model": args.model,
        "messages": build_messages(prompt, args.system),
        "temperature": args.temperature,
    }
    payload["thinking"] = {"type": "disabled" if args.no_thinking else "enabled"}

    try:
        if args.no_stream:
            request_once(payload, key)
        else:
            request_stream(payload, key)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        print(f"HTTP {e.code} 错误：{detail}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"网络错误：{e.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
