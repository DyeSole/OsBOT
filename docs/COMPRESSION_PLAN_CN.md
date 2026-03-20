# 聊天历史压缩方案（最新）

## 目标
- 控制上下文长度，避免超出模型上下文上限。
- 保留长期记忆（摘要）和短期细节（固定窗口消息）。
- 存储结构尽量简单，便于后续扩展。

## 标识键约定
- 保留 `时间+channel_id` 作为会话键。

## 触发策略（按时间）
- 以每日 `00:00`或晚安（优先级更高） 为分界做归档判断。
- 对 `00:00` 之前的历史做压缩评估，在’晚安‘触发后定时4h，未再次发信息则开始压缩。
- 当该部分估算达到阈值左右（可配，默认 `TRANSCRIPT_MAX_TOKENS=20000`）时触发压缩。

## 压缩策略
- 允许“全量旧历史压缩”为摘要（不必复杂拆分文件）。
- 摘要记录必须包含时间覆盖范围：
  - `start_time`
  - `end_time`
- 摘要正文：`summary_text`
- 建议附带：`generated_at`

## 发送给 API 的上下文
- 使用两部分拼接：
  system prompt
历史摘要
冻结窗口消息
当前待回复消息批次

  
## 更新策略
- 
## 摘要格式（暂时）
    channel_id
start_time
end_time
message_count
source_token_estimate
summary_text
keywords
generated_at
version
source_hash


## 固定窗口消息规则
- 使用 Discord 消息查询能力（DC message）获取固定 30 条。
- 可选两种固定方式：
  - 按锚点冻结：以某个锚点消息为基准，固定取对应 30 条。
- 需求倾向：不要随着后续聊天滚动到“最近30条”。

## 是否删除旧历史
- 不删除原始历史。

## 发送前处理格式（建议）
- 摘要段：`[start_time ~ end_time] summary_text`

