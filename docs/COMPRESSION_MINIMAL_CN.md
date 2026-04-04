# 聊天历史压缩最小实现草案

## 目标
- 先落一个可运行的最小压缩骨架。
- 不一次做完完整的自动压缩方案。
- 原始消息和压缩归档分层存储。
- 为后续的关键词检索 / 文件检索工具预留结构。

## 第一阶段不做的内容
- 不做 `00:00` 自动压缩。
- 不做“晚安 + 4h 无消息”自动压缩。
- 不做 Discord 侧正式的锚点窗口读取。
- 不做 `recent window` 正式实现，只保留伪代码。
- 不做多级摘要。
- 不做分段链表。
- 不做自动重算摘要。

## 存储总览

### 1. 活跃原始消息
- 路径：
  - `data/chat_history/{channel_id}.jsonl`
- 只存“还没有被压缩”的消息。
- 新消息继续顺序追加到这个文件。

原始消息结构保持简单：

```json
{
  "role": "user",
  "username": "Dee",
  "time": "2026-03-12 00:30:15",
  "content": "晚安。"
}
```

说明：
- 当前阶段不引入 `message_id`。
- 当前阶段用 `time` 作为压缩边界和锚点。
- 前提假设：
  - 同一个频道内，消息写入顺序可信。
  - 如果出现同一秒多条消息，按文件中的顺序认定最后一条。

### 2. 压缩摘要段
- 路径：
  - `data/memory/{channel_id}/segments/{source_id}.json`

### 3. 被压缩的原始归档
- 路径：
  - `data/memory/{channel_id}/raw/{source_id}.jsonl`

### 4. 索引文件
- 路径：
  - `data/memory/{channel_id}/index.json`

## 为什么当前阶段不用 `message_id`
- 你现在的压缩触发点是“这天对话结束时的最后一条消息”。
- 这个场景只需要一个稳定的边界值，不需要长期保存每条消息的 Discord id。
- 当前原始存档已经有完整时间戳，先用 `time` 能把结构做简单。
- 后面如果真的要直接从 Discord 做精确锚点读取，再补 `message_id` 也来得及。

注意：
- 这不是说 `message_id` 没价值。
- 而是当前最小实现里先不引入它，减少改动面。

## 摘要段结构


单个摘要段文件示例：

```json
{
  "segment_id": "seg_20260312_001",
  "source_id": "1234567890:2026-03-11 20:00:00:2026-03-12 00:30:15",
  "start_time": "2026-03-11 20:00:00",
  "end_time": "2026-03-12 00:30:15",
  "message_count": 128,
  "summary_text": "......",
  "keywords": ["晚安", "工作压力", "撒娇"],
  "generated_at": "2026-03-12 04:35:00",
  "source_hash": "sha256:xxxx",
  "version": 1
}
```

字段说明：
- `segment_id`
  - 摘要段自己的 id。
- `source_id`
  - 摘要来源 id。
  - 当前建议格式：`{channel_id}:{start_time}:{end_time}`
- `start_time` / `end_time`
  - 这段摘要覆盖的原始消息范围。
- `message_count`
  - 这段摘要覆盖的原始消息条数。
- `summary_text`
  - 摘要正文。
- `keywords`
  - 保留，用于后续检索或 RAG 召回。
  - 建议始终使用数组。
- `generated_at`
  - 摘要生成时间。
- `source_hash`
  - 这段原始消息内容的 hash。
  - 后面判断是否需要重算摘要时会用到。
- `version`
  - 摘要结构版本。

## index.json 结构

示例：

```json
{
  "channel_id": 1234567890,
  "anchor_time": "2026-03-12 00:30:15",
  "segment_ids": [
    "seg_20260312_001"
  ],
  "segments": [
    {
      "segment_id": "seg_20260312_001",
      "source_id": "1234567890:2026-03-11 20:00:00:2026-03-12 00:30:15",
      "start_time": "2026-03-11 20:00:00",
      "end_time": "2026-03-12 00:30:15",
      "keywords": ["晚安", "工作压力", "撒娇"]
    }
  ],
  "version": 1
}
```

字段说明：
- `anchor_time`
  - 当前冻结锚点。
  - 含义是“本次压缩批次里最后一条消息的时间”。
- `segment_ids`
  - 当前频道已有摘要段的 id 列表。
- `segments`
  - 轻量索引信息。
  - 这部分是为了后面的关键词检索 / 文件检索工具准备的。

## 第一阶段的压缩逻辑

当前阶段采用最简单的模式：
- 读取活跃原始消息文件中的全部消息。
- 整批压缩。
- 生成一个摘要段。
- 摘要文件按频道目录分开存data/memory/{channel_id}作为root
- 把这批原始消息归档到 `root/raw/{source_id}.jsonl`。
- 把摘要段写到 `root/segments/{source_id}.json`。
- 更新 `root/index.json`。
- 清空或重建当前频道的活跃消息文件。

也就是说：
- 每次压缩处理的都是“当前活跃文件里的全部消息”。
- 压缩完成后，这批消息不再留在活跃文件里。

## 最小实现中的核心函数

### 1. `compress_history(channel_id)`
- 手动触发一次压缩。
- 第一阶段不接自动调度。

需要完成的步骤：
- 读取当前频道活跃文件里的全部原始消息。
- 用全部原始消息生成摘要。
- 保存摘要段。
- 保存被压缩的原始归档。
- 更新 `index.json`。
- 清空活跃消息文件。

伪代码：

```python
def compress_history(channel_id: int) -> None:
    messages = load_all_entries(channel_id)
    if not messages:
        return

    transcript = format_messages_for_summary(messages)
    summary_text, keywords = generate_summary(transcript)

    start_time = messages[0]["time"]
    end_time = messages[-1]["time"]
    source_id = build_source_id(channel_id, start_time, end_time)
    segment_id = build_segment_id()

    save_raw_archive(
        channel_id=channel_id,
        source_id=source_id,
        messages=messages,
    )

    save_summary_segment(
        channel_id=channel_id,
        source_id=source_id,
        segment_id=segment_id,
        start_time=start_time,
        end_time=end_time,
        message_count=len(messages),
        summary_text=summary_text,
        keywords=keywords,
        generated_at=now(),
        source_hash=hash_messages(messages),
        version=1,
    )

    update_index(
        channel_id=channel_id,
        anchor_time=end_time,
        segment_id=segment_id,
        source_id=source_id,
        start_time=start_time,
        end_time=end_time,
        keywords=keywords,
    )

    reset_active_history(channel_id)
```

### 2. `build_context_for_api(channel_id, pending_messages)`
- 构造当前要发给模型的上下文。
- 第一阶段只拼“摘要 +锚点前30条信息+ 活跃原始消息 + 当前消息”。

职责：
- 读取摘要索引和摘要段。
- 读取当前活跃文件中的消息。
- 拼接成一份最终上下文。

伪代码：

```python
def build_context_for_api(channel_id: int, pending_messages: list[dict[str, str]]) -> list[dict[str, str]]:
    segments = load_summary_segments(channel_id)
    live_messages = load_all_entries(channel_id)

    summary_block = render_summary_segments(segments)
    live_block = render_raw_messages(live_messages)
    pending_block = render_raw_messages(pending_messages)

    final_text = join_non_empty_blocks([
        summary_block,
        live_block,
        pending_block,
    ])

    return [{"role": "user", "content": final_text}]
```

说明：
- 这个函数的作用不是“存储数据”。
- 它的作用是统一决定“本次到底把哪些内容发给模型”。
- 以后如果要切换别的模型或别的 API，这一层仍然可以复用。

## `recent window` 预留伪代码
- 第一阶段不实现。
- 只保留接口，第二阶段再接 Discord 读取。

```python
def load_frozen_window(channel_id: int, anchor_time: str, limit: int = 30) -> list[dict[str, str]]:
    # Phase 1:
    # 暂不实现
    # Phase 2:
    # 从 Discord 读取 anchor_time 之前的 30 条消息
    return []
```

## 摘要什么时候需要重算
- 第一阶段不做自动重算。
- 只定义条件。

建议的重算条件：
- 原始归档内容变化，`source_hash` 不一致。
- 摘要结构版本变化，`version` 升级。
- 摘要质量不够，需要手动重算。
- 关键词质量不够，需要手动重算。

## 第一阶段交付物
- 保持 `data/chat_history/{channel_id}.jsonl` 作为活跃消息文件。
- 新增摘要段存储：
  - `data/memory/{channel_id}/segments/{source_id}.json`
- 新增原始归档存储：
  - `data/memory/{channel_id}/raw/{source_id}.jsonl`
- 新增索引文件：
  - `data/memory/{channel_id}/index.json`
- 新增 `compress_history(channel_id)` 手动压缩入口。
- 新增 `build_context_for_api(channel_id, pending_messages)` 上下文构造入口。
- `recent window` 只保留伪代码。

## 当前建议
- 先按这份最小草案落代码。
- 先跑通一次“活跃原文 -> 摘要段 -> 原始归档 -> index.json -> 用摘要拼上下文”的完整链路。
- 等链路稳定后，再接自动触发和 Discord 锚点窗口。
