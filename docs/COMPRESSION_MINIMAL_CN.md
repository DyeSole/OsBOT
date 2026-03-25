# 聊天历史压缩

## 存储结构

### 活跃消息
- 路径：`data/chat_history/{channel_id}.jsonl`
- 未压缩的消息，顺序追加。

```json
{"role": "user", "username": "Dee", "time": "2026-03-12 00:30:15", "content": "晚安。"}
```

### 压缩摘要段
- 路径：`data/memory/{channel_id}/segments/{start_time}_{end_time}.json`

```json
{
  "start_time": "2026-03-11 20:00:00",
  "end_time": "2026-03-12 00:30:15",
  "summary_text": "......",
  "keywords": ["晚安", "工作压力", "撒娇"]
}
```

### 原始归档
- 路径：`data/memory/{channel_id}/raw/{start_time}_{end_time}.jsonl`
- 被压缩的原始消息备份，不删除。

## 压缩流程

1. 读取活跃文件中 marker 之后的全部消息。
2. 发给 LLM 生成 `summary_text` + `keywords`。
3. 保存摘要段和原始归档。
4. 在活跃文件中写入 marker，保留最近 30 条。

## 上下文拼接顺序

```
system prompt
[历史摘要] — 所有摘要段按时间排列
活跃消息（marker 之后）
当前 pending 消息
```

## 触发策略（待实现）

- **晚安触发**：检测到"晚安"后启动 4h 倒计时，期间无新消息则执行压缩。优先级高于定时触发。
- **Token 阈值**：活跃消息估算超过 `TRANSCRIPT_MAX_TOKENS`（默认 20000）时触发。
- **手动触发**：`python scripts/compress_history.py <channel_id>`

## 冻结窗口（待实现）

当前实现：压缩后保留尾部 30 条，新消息继续追加，窗口会滚动。

设计目标：以压缩锚点为基准冻结 30 条，不随后续聊天滚动。可通过 Discord 消息查询按锚点 message_id 取固定窗口。需要在消息记录中引入 `message_id` 字段。
