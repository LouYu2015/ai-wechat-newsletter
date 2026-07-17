# 2026-07-17 跨日重叠下限 + 覆盖范围记录

让跨日重叠段既保证"够长"（安静夜晚也能接上话题），又保证"不漏"（提前生成的日报，未报道的尾巴一定进入次日）。

## 一、问题与动机

日报按天生成。`chat_extractor.extract_messages(date_str)` 的提取窗口原本固定为 `[D 00:00−1h, D+1 00:00+1h]`：开头那 1 小时与前一天重叠，用于跨日话题衔接与去重（配合 `prompts.py` 的「关于跨日延续与去重」规则）。两个问题：

1. **安静的晚上重叠段太短**：±1h 里可能只有两三条消息，跨天话题的上下文接不上，模型看不到前因。
2. **提前生成留下永久缺口**：用户常用 `--allow-incomplete` 在当天 21:00 就跑当日日报，日报只覆盖到当时最后一条消息；但次日窗口仍从固定的 23:00（次日 00:00−1h）开始，于是 **21:00–23:00 之间的消息两天日报都不会报**，永久丢失。且"实际覆盖到哪"事后无法从 DB 反查——DB 后来会补全整天消息，21:00 那一刻的水位线不留痕迹，必须在提交时刻记录下来。

## 二、设计

### 覆盖水位线（`coverage.py`）

每日日报记录"实际报道到了哪条消息"的时间戳，存 `debug/YYYY/MM/DD/coverage.json`：

```json
{"last_message_ts": 1789xxxxx, "last_message_at": "2026-07-16T20:58:12+08:00"}
```

`last_message_at` 是同一时间戳的本地 ISO 表示，纯给人看；程序只读 ts。`last_covered_ts` 在文件缺失 / JSON 损坏 / 字段缺失时一律返回 `None`（fallback 无害，见下）。

### 窗口起点算法（`extract_messages`）

新增 `config.OVERLAP_MIN_MESSAGES = 20`。结尾不变（次日 00:00+1h），起点：

```
default_start = D 00:00 − 1h                       # 现状
anchor    = coverage.last_covered_ts(D−1)；缺失时 = D 00:00 的 ts
candidate = anchor 之前（含 anchor，create_time <= anchor）倒数第 20 条的 create_time
start     = min(default_start, candidate)          # candidate 为 None 时用 default_start
start     = max(start, (D−1) 00:00 的 ts)           # 回溯上限
```

`candidate` 的查询跨两个消息库（`message_0` / `message_1`），每库 `... WHERE create_time <= ? ORDER BY create_time DESC LIMIT 20`，合并后取第 20 新的那条（不足 20 条取最旧的，一条都没有返回 `None`）。容错沿用 `extract_messages` 现有写法：库缺失 / 表缺失跳过、损坏库 warn 跳过。写成模块内 helper `_nth_recent_ts`。

语义：重叠保证 ≥ max(1 小时, 20 条)。anchor 用前一天的覆盖记录意味着——若昨天日报 21:00 提前生成，今天窗口从「21:00 往前数 20 条」开始，昨天 21:00–24:00 从未被报道的尾巴全部进入今天的输入。

### 日期分界线（`privacy.py`）

窗口两端各伸入相邻天，模型需要知道某段消息属于哪一天。`format_tokenized_messages`（纯文本）和 `format_tokenized_messages_blocks`（content blocks）在第一条消息之前、以及每次本地日历日期变化时插入：

```
——— 以下消息发生在 YYYY-MM-DD ———
```

（破折号为三个 U+2014。）以**实际输出的消息**为准判断日期变化——被跳过的消息（`_format_one_line` 返回 `None`）不触发分界线。分界线只陈述日期，去重 / 补报规则集中在 prompt。

### Prompt 规则同步（`prompts.py`）

「关于跨日延续与去重」第 1 条从"开头和结尾各有约 1 小时重叠"改为「重叠时段去重与补报」：说明重叠段"至少约 1 小时或 20 条，可能更长"、解释分界线，并把开头的前一日消息分两种处理——已完整写过的跳过，前一天日报漏掉的（生成较早时）照常补报并用「昨晚」标明时间。

## 三、实现

- **`coverage.py`**（新）：`record` / `last_covered_ts`。
- **`config.py`**：`OVERLAP_MIN_MESSAGES = 20`。
- **`chat_extractor.py`**：`_nth_recent_ts` helper + `extract_messages` 起点算法。
- **`privacy.py`**：`_date_divider` + 两个 format 函数插入分界线。
- **`prompts.py`**：第 1 条规则替换。
- **覆盖记录写入时机**（见决策记录 Q2）：
  - 批量路径写在 `batch_extractor.submit_batch`（与 `save_state` 同时），参数 `last_message_ts` 由 `cli._run_db_pipeline`（`messages[-1].create_time`）→ `_run_batch_extraction` → `run_batch` → `submit_batch` 传下；`run_batch` 的重试轮不涉及。
  - 流式路径（`--no-batch`）：`cli._run_db_pipeline` 在调用 `_run_streaming_extraction` 前直接 `coverage.record`。
- **空日守卫**（`cli.py`）：窗口变长后"当天没消息但重叠段有 20 条"成为可能，跳过条件从 `if not messages` 改为「没有任何消息落在 `[D 00:00, D+1 00:00)`」。

## 四、决策记录

### Q1：为何记录覆盖水位线，而非事后反查？

`--allow-incomplete` 的覆盖点不可反查：日报只覆盖提交时刻的快照，但 DB 之后会补全整天消息，21:00 那一刻覆盖到哪，事后从 DB 无从得知。只能在**提交时刻**把水位线定格到磁盘。

### Q2：为何写在 `submit_batch`？（resume 免疫）

"提交时刻"正是快照定格的时刻，覆盖水位线应等于提交时的最后一条消息。写在 `submit_batch` 里，resume / 复用路径天然不覆写（它们不经过 `submit_batch`）。逐条核对三种 resume 交互（`_decide_batch_state`）：

- **[c] 续接未完成批次**：不调 `submit_batch`，覆盖记录保持提交时刻的值。若此刻重新提取的消息比提交时多（DB 已补全），覆写就会把水位线推到"其实没进本期日报"的消息上——即虚报。不写，正确。
- **[r] 重新提交**：取消旧批次、`state=None` 走 `submit_batch`，覆盖记录被这次提交的新水位线正确覆盖（本期日报确实覆盖到新水位线）。
- **[u] 复用已消费批次**：不调 `submit_batch`，沿用当初提交时写下的水位线。日报内容就是那次的结果，水位线也应是那次的，正确。

retry 轮（服务端失败重提交）直接 `client.messages.batches.create`，不经 `submit_batch`，也不碰覆盖记录——retry 只是重跑同一份已提交的输入，水位线不变。

### Q3：回溯上限为何是前一天 00:00？

再往前的内容 `<previous_reports>` 已经覆盖（默认喂前 3 天完整日报），重叠窗口拉过前一天午夜只会让模型重复读到早已写过的内容、徒增 token。20 条在极稀疏时可能跨到大前天，钳在前一天 00:00 即止。

### Q4：fallback 语义

缺覆盖记录（文件不存在 / 损坏 / 字段缺失）时 anchor 退回 `D 00:00`，等价于"假设前一天覆盖到了午夜"——即老行为（重叠段从午夜倒数）。无害降级：正常按天完整生成的日报本就覆盖到午夜附近，缺记录不会漏报，只是不享受"提前生成尾巴补报"这一增益。

### Q5：分界线只陈述日期

分界线不写"这段是重叠/去重段"之类的指令，只写日期。判断哪些该跳过、哪些该补报是随场景变化的策略，集中在 prompt 里维护；`privacy.py` 只提供客观的日期锚点，两边职责清晰。

## 五、已知副作用与过渡期

- **旧批次指纹 mismatch**：窗口变化改变消息集合，改动上线后第一次跑若存在未完成旧批次，`raw_msg_sha256` 会 mismatch，走现有的续接/重提交互（`_decide_batch_state`），预期行为。
- **过渡期缺口**：上线前用 `--allow-incomplete` 生成的 incomplete 日报没有 coverage 记录，其次日窗口只能走 fallback（从午夜倒数），那一次的"提前生成尾巴"仍会漏。上线后新生成的日报才开始留记录，此后不再有此类缺口。
