"""每日日报的「覆盖水位线」记录：日报实际报道到了哪条消息（哪个时间戳）。

为什么记录、而不是事后从 DB 反查：用户常用 ``--allow-incomplete`` 在当天 21:00
就提前生成日报，日报只覆盖到当时最后一条消息；但 DB 之后会补全整天消息，事后
无法从 DB 得知「提交那一刻覆盖到哪」。必须在提交时刻把水位线定格下来。

下游用途：次日 ``chat_extractor.extract_messages`` 用前一天的水位线做重叠窗口的
锚点——保证昨天 21:00–24:00 这段从未被报道的尾巴进入今天的输入。缺记录时下游
退化为「从午夜倒数」，无害。

存放位置沿用 per-day debug 产物约定：``debug/YYYY/MM/DD/coverage.json``。
"""

from __future__ import annotations

import datetime
import json

from wechat_daily import config

_FILENAME = "coverage.json"


def _path(date_str: str):
    return config.debug_dir_for(date_str) / _FILENAME


def record(date_str: str, last_message_ts: int) -> None:
    """把 *date_str* 日报覆盖到的最后一条消息时间戳定格到磁盘。

    ``last_message_at`` 是同一时间戳的本地 ISO 表示，纯给人看；程序只读 ts。
    """
    path = _path(date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    local_iso = datetime.datetime.fromtimestamp(last_message_ts).astimezone().isoformat()
    path.write_text(
        json.dumps(
            {"last_message_ts": last_message_ts, "last_message_at": local_iso},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def last_covered_ts(date_str: str) -> int | None:
    """返回 *date_str* 日报覆盖到的最后一条消息时间戳；缺失/损坏/字段缺失均返回 None。

    fallback 无害：下游把 None 当作「假设前一天覆盖到了午夜」，重叠窗口退化为
    从午夜倒数。
    """
    try:
        data = json.loads(_path(date_str).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("last_message_ts")
    return ts if isinstance(ts, int) else None
