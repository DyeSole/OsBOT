# 聊天历史压缩

## 存储结构

### 活跃消息
- 路径：`data/chat_history/{channel_id}.jsonl`
- 还没被压缩的消息，新消息顺序追加。

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
- 被压缩的原始消息备份。

## 压缩流程

1. 读取活跃文件中 marker 之后的全部消息。
2. 发给 LLM 生成 `summary_text` + `keywords`。
3. 保存摘要段和原始归档。
4. 在活跃文件中写入 marker，保留最近 30 条。

## 上下文构造

`build_context_for_api` 拼接顺序：
1. `[历史摘要]` — 所有摘要段按时间排列
2. 活跃消息
3. 当前 pending 消息
