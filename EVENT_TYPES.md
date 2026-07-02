# Codex Session EventType 学习文档

本文档说明 Codex session `.jsonl`（rollout 文件）里每一种 `eventType` 的作用、关键字段，以及它在 Codex 实际执行链路中的位置。

事实源是 upstream Codex 源码（本地 `../codex`）：

- `RolloutItem` / `EventMsg` / 各事件 struct：`codex-rs/protocol/src/protocol.rs`
- `ResponseItem` / `ResponseInputItem`：`codex-rs/protocol/src/models.rs`
- `TurnItem`：`codex-rs/protocol/src/items.rs`
- 持久化策略（决定哪些事件会写进 rollout）：`codex-rs/rollout/src/policy.rs`

> 注意：本查看器里的 `eventType` = `payload.type || record.type`（见 `index.html` 的 `normalizeEvent`）。要准确理解一条记录，必须先看外层 `record.type`（即 `RolloutItem` 的 tag），再看 `payload.type`。

---

## 0. 顶层结构与三层模型

rollout 文件每一行是一个 `RolloutLine`：

```json
{ "timestamp": "...", "type": "<RolloutItem tag>", "payload": { "type": "...", ... } }
```

外层 `type`（`RolloutItem`）只有这 8 类（`codex-rs/protocol/src/protocol.rs` `enum RolloutItem`）：

| record.type | 含义 |
| --- | --- |
| `session_meta` | 会话元数据 |
| `response_item` | Responses API 模型可见 item（恢复上下文的核心） |
| `inter_agent_communication` | legacy agent 间通信 item |
| `inter_agent_communication_metadata` | agent 间通信本地元数据 |
| `compacted` | 上下文压缩记录 |
| `turn_context` | 每轮 turn 的环境快照 |
| `world_state` | world-state diff 恢复状态 |
| `event_msg` | legacy/UI 事件（`EventMsg`） |

因此 `eventType` 大致分三层：

1. **持久化结构层**：`session_meta` / `turn_context` / `world_state` / `compacted` / `inter_agent_communication(_metadata)` —— 不是对话内容，而是恢复/重放上下文用的状态。
2. **模型可见 item 层（`response_item`）**：`message` / `agent_message` / `reasoning` / `function_call` / `function_call_output` / `custom_tool_call(_output)` / `tool_search_call/output` / `web_search_call` / `image_generation_call` / `local_shell_call` / `compaction` / `context_compaction` —— 这些是真正发给模型、也用于恢复历史的 source of truth。
3. **legacy/UI 事件层（`event_msg`）**：`user_message` / `agent_message` / `agent_reasoning(_raw_content)` / `task_started` / `task_complete` / `token_count` / `mcp_tool_call_end` / `patch_apply_end` / `web_search_end` / `image_generation_end` / `entered/exited_review_mode` / `thread_goal_updated` / `context_compacted` / `thread_rolled_back` / `turn_aborted` / `sub_agent_activity` 等 —— 主要供事件流/UI 回放，是从模型 item 投影出来的展示事件。

### 持久化（很关键）

并非所有协议事件都会写进 rollout。`codex-rs/rollout/src/policy.rs` 决定持久化白名单：

- **`event_msg` 中会被持久化的**：`user_message`、`agent_message`、`agent_reasoning`、`agent_reasoning_raw_content`、`patch_apply_end`、`token_count`、`thread_goal_updated`、`context_compacted`、`entered_review_mode`、`exited_review_mode`、`mcp_tool_call_end`、`thread_rolled_back`、`turn_aborted`、`task_started`、`task_complete`、`web_search_end`、`image_generation_end`、`sub_agent_activity`，以及 `item_completed`（仅当内部 item 是 `Plan` 或 `Sleep`）。
- **不会被持久化的（只存在于实时事件流/UI）**：`mcp_tool_call_begin`、`exec_command_begin/end`、`web_search_begin`、`image_generation_begin`、`patch_apply_begin`、`plan_update`、`raw_response_item`、`stream_error`、各种 `*_delta`、approval/elicitation 请求等。
- **`response_item` 中会被持久化的**：`message`、`agent_message`、`reasoning`、`local_shell_call`、`function_call`、`tool_search_call`、`function_call_output`、`tool_search_output`、`custom_tool_call`、`custom_tool_call_output`、`web_search_call`、`image_generation_call`、`compaction`、`context_compaction`。`additional_tools`、`compaction_trigger`、`other` 不持久化。

---

## 1. 持久化结构层

### `session_meta`
- **作用**：rollout 的会话元数据，通常是 JSONL 第一行（`RolloutItem::SessionMeta(SessionMetaLine)`）。
- **关键字段**：`id`/`session_id`、`cwd`、`source`、`originator`、`cli_version`、`model_provider`、`base_instructions`、`dynamic_tools`、`git`、`forked_from_id`/`parent_thread_id`（fork/子线程时）。
- **链路**：session 启动时由 recorder 写入，描述这个 thread 的来源和启动环境，不是一条对话消息。

### `turn_context`
- **作用**：每个真实用户 turn 的持久化环境快照（`RolloutItem::TurnContext(TurnContextItem)`）。
- **关键字段**：`turn_id`、`cwd`、`workspace_roots`、`current_date`/`timezone`、`approval_policy`、`sandbox_policy`、`permission_profile`、`network`、`model`、`effort`、`collaboration_mode`、`multi_agent_*`。
- **链路**：每个用户 turn 计算完模型可见上下文后写一次；mid-turn 压缩重建上下文后会再写一次。resume/fork 重放时据此恢复当时的执行上下文基线。

### `world_state`
- **作用**：用于恢复 world-state diff 的持久化状态（`RolloutItem::WorldState(WorldStateItem)`）。
- **关键字段**：`full`（`true`=全量快照，建立新基线；`false`=基于上一份 world state 的 merge patch）、`state`。
- **链路**：内部上下文差异恢复机制，不是用户/助手消息。

### `compacted`
- **作用**：上下文压缩记录（`RolloutItem::Compacted(CompactedItem)`）。
- **关键字段**：`message`（压缩摘要）、`replacement_history`（压缩后替代旧上下文的 `ResponseItem` 列表，可选）、`window_number`/`first_window_id`/`previous_window_id`/`window_id`（上下文窗口链）。
- **链路**：自动或手动压缩时写入。恢复历史时用它替代被压缩掉的旧上下文。注意它与 `response_item` 里的 `compaction`/`context_compaction` 不同：后者是模型侧加密压缩 item。

### `inter_agent_communication`
- **作用**：legacy 格式的 agent 间通信记录（`RolloutItem::InterAgentCommunication`），会被重建为模型可见的 `agent_message`。
- **关键字段**：`author`、`recipient`、`other_recipients`、`content`/`encrypted_content`、`trigger_turn`。
- **链路**：多 agent 通信。新路径更多直接持久化成 `response_item(type=agent_message)` 加 metadata。

### `inter_agent_communication_metadata`
- **作用**：agent 间通信的本地附加元数据（`RolloutItem::InterAgentCommunicationMetadata`）。
- **关键字段**：`trigger_turn`（这条通信是否应触发接收方 turn）。
- **链路**：本地投递元数据，不属于 Responses API item。

---

## 2. 模型可见 item 层（`record.type = response_item`）

这些来自 `ResponseItem`（`codex-rs/protocol/src/models.rs`），是发给模型、也用于恢复历史的 source of truth。

### `message`
- **作用**：Responses API/message-history 层的模型可见消息（`ResponseItem::Message`）。
- **关键字段**：`role`（`assistant`/`user`/`developer`/`system`）、`content`、`phase`、`id`。
- **链路**：必须结合 `role` 判断方向：
  - `role=assistant`：模型输出的上下文 source of truth；
  - `role=user`：可能是用户真实输入，也可能是 Codex 注入的环境/规则/hook/context 内容（查看器会用 `isInjectedUserContext` 区分 `<environment_context>` 等注入块）；
  - `role=developer`/`system`：运行时注入的指令上下文。

### `agent_message`（一词多义，取决于 `record.type`）
- 若 `record.type=response_item`：`ResponseItem::AgentMessage`，多 agent 通信的模型可见 item。字段：`author`、`recipient`、`content`、`internal_chat_message_metadata_passthrough`。会作为模型历史 item 持久化。
- 若 `record.type=event_msg`：`EventMsg::AgentMessage`，legacy/UI 展示事件。字段：`message`、`phase`、`memory_citation`。是从 `TurnItem::AgentMessage` 投影出的纯文本，经常与 `message(role=assistant)` 内容相同，但不保留完整结构化 content/id/role。

### `reasoning`
- **作用**：Responses API 的 reasoning item（`ResponseItem::Reasoning`）。
- **关键字段**：`summary`（可能可见）、`content`/`encrypted_content`（取决于模型和配置，很多 rollout 不可读）。
- **链路**：模型推理记录，恢复上下文时回传给模型。

### `function_call`
- **作用**：Responses API 的 function_call 模型输出项（`ResponseItem::FunctionCall`）。
- **关键字段**：`name`、`namespace`、`call_id`、`arguments`（源码中按**字符串**保存，内容通常是 JSON，需再解析）。
- **链路**：模型下发工具调用。结果在 `function_call_output` 里，通过 `call_id` 配对。

### `function_call_output`
- **作用**：function_call 对应的工具返回（`ResponseItem::FunctionCallOutput`）。
- **关键字段**：`call_id`、`output`（`FunctionCallOutputPayload`：wire 上可能是纯字符串 `content`，也可能是结构化 `content_items`）。
- **链路**：返回给模型的工具结果。注意 MCP 工具的输出在持久化前会被转换成 `function_call_output`（`ResponseInputItem::McpToolCallOutput` → `ResponseItem::FunctionCallOutput`，见 `models.rs` 的 `From` 实现）。

### `custom_tool_call`
- **作用**：Responses API 的 custom/freeform tool 调用项（`ResponseItem::CustomToolCall`）。
- **关键字段**：`name`、`call_id`、`input`（自由文本，不一定是 JSON）、`status`。
- **链路**：模型下发自定义工具调用，`apply_patch`、exec、代码片段等可能走这里（取决于当前工具定义）。

### `custom_tool_call_output`
- **作用**：custom_tool_call 的返回（`ResponseItem::CustomToolCallOutput`）。
- **关键字段**：`call_id`、`name`、`output`（编码与 `function_call_output` 相同）。
- **链路**：返回给模型的自定义工具结果。

### `tool_search_call` / `tool_search_output`
- **作用**：模型发起的工具检索请求及其结果（`ResponseItem::ToolSearchCall` / `ToolSearchOutput`）。
- **关键字段**：`call_id`、`execution`、`arguments`（call）；`tools`（检索到的工具列表）、`status`、`execution`（output）。
- **链路**：模型请求"有哪些可用工具"，不是业务工具本身。通过 `call_id` 配对。

### `web_search_call`
- **作用**：模型触发 web search 的 Responses API item（`ResponseItem::WebSearchCall`）。
- **关键字段**：`status`、`action`（如 `{type:"search", query:"..."}`）。
- **链路**：模型下发网页检索动作；完成的展示事件是 `event_msg(type=web_search_end)`。

### `image_generation_call`
- **作用**：模型触发图片生成的 Responses API item（`ResponseItem::ImageGenerationCall`）。
- **关键字段**：`status`、`revised_prompt`、`result`（生成结果引用或编码数据）。
- **链路**：模型下发图片生成动作；完成事件是 `event_msg(type=image_generation_end)`。

### `local_shell_call`
- **作用**：Responses API 的 local_shell_call item（`ResponseItem::LocalShellCall`）。
- **关键字段**：`call_id`、`status`、`action`。
- **链路**：模型侧记录到的本地 shell 动作，不等同于完整终端 stdout/stderr。

### `compaction` / `context_compaction`
- **作用**：Responses API 的模型侧压缩 item（`ResponseItem::Compaction` / `ContextCompaction`）。
- **关键字段**：`encrypted_content`（compaction 必有；context_compaction 可选）、`id`。
- **链路**：服务端/模型侧压缩状态，和顶层 `compacted` 一起服务于上下文窗口恢复。注意与顶层 `compacted` 摘要不是一回事。

---

## 3. legacy/UI 事件层（`record.type = event_msg`）

来自 `EventMsg`（`codex-rs/protocol/src/protocol.rs`）。这些是从模型 item 投影出的展示/状态事件，仅白名单内的会被持久化。

### `user_message`
- **作用**：legacy/UI 事件层的用户消息（`EventMsg::UserMessage`）。
- **关键字段**：`message`、`images`/`local_images`（及对应 detail）、`text_elements`、`client_id`。
- **链路**：识别用户实际提交/追加的需求与 turn 边界。它不等同于完整模型上行上下文（完整上下文通常还含 `message(role=user/developer/system)` 的注入内容）。

### `agent_message`（event_msg 形态）
- 见上文"模型可见 item 层"中的 `agent_message` 二义性说明。这里是 `EventMsg::AgentMessage`，纯文本展示事件。

### `agent_reasoning` / `agent_reasoning_raw_content`
- **作用**：legacy/UI 的 reasoning 摘要与 raw reasoning（`EventMsg::AgentReasoning` / `AgentReasoningRawContent`）。
- **关键字段**：`text`。
- **链路**：可展示的推理文本，来自 reasoning item 或流式 reasoning 的兼容投影。raw content 只有配置允许时才有内容；敏感/加密内容不应假设可读。

### `task_started` / `turn_started`
- **作用**：一次 turn 开始。源码 Rust 变体是 `TurnStarted`，wire 名为 `task_started`，`turn_started` 是 v2 互通的反序列化别名（`#[serde(rename="task_started", alias="turn_started")]`）。
- **关键字段**：`turn_id`、`trace_id`、`started_at`、`model_context_window`、`collaboration_mode_kind`。
- **链路**：turn 边界，和 `task_complete` 配对，是恢复/分段的重要锚点。新文件写 `task_started`；旧文件若出现 `turn_started` 按 `task_started` 理解。

### `task_complete` / `turn_complete`
- **作用**：一次 turn 完成。Rust 变体 `TurnComplete`，wire 名 `task_complete`，别名 `turn_complete`。
- **关键字段**：`turn_id`、`last_agent_message`、`completed_at`、`duration_ms`、`time_to_first_token_ms`。
- **链路**：turn 完成边界，不是模型消息本身。

### `token_count`
- **作用**：token 使用量与限流状态遥测（`EventMsg::TokenCount`）。
- **关键字段**：totals、last-turn 用量、rate-limit 信息（`Optional` 为 `None` 时表示未知，不应展示）。
- **链路**：用于分析会话为何长/贵，或是否接近限流。

### `mcp_tool_call_end`
- **作用**：MCP 工具调用结束事件（`EventMsg::McpToolCallEnd`）。是 rollout 中**实际持久化**的 MCP 工具结果事件。
- **关键字段**：`call_id`、`invocation`（`server`/`tool`/`arguments`）、`duration`、`result`（`Ok(CallToolResult)` 或 `Err(String)`）。
- **链路**：因为 `mcp_tool_call_begin` 默认不持久化，rollout 中通常只看到 end，用 `call_id` 自身定位即可。

### `mcp_tool_call_begin`（通常不在 rollout 中）
- **作用**：MCP 工具调用开始事件（`EventMsg::McpToolCallBegin`）。
- **关键字段**：`call_id`、`invocation`（`server`/`tool`/`arguments`）。
- **链路**：`policy.rs` 默认**不持久化**它（只持久化 end），正常 rollout 文件不会出现；若出现多半来自实时事件流或非标准导出。

### `mcp_tool_call`（不是 RolloutItem 顶层类型）
- **作用**：`TurnItem::McpToolCall`（`codex-rs/protocol/src/items.rs`），通常嵌在 `item_completed.payload.item` 内。
- **关键字段**：`id`（注意只有 `id`，没有 `call_id`）、`server`、`tool`、`arguments`、`status`、`result`/`error`、`duration`。
- **链路**：由于 `McpToolCall` 不在 `item_completed` 的 Plan/Sleep 持久化白名单内，它一般不会作为顶层 `payload.type` 出现在 rollout 中；真正持久化的 MCP 结果是 `mcp_tool_call_end` 以及转换后的 `function_call_output`。

### `patch_apply_end`
- **作用**：apply_patch/文件修改流程完成事件（`EventMsg::PatchApplyEnd`）。
- **关键字段**：`success`/`status`、`stdout`/`stderr` 或错误字段。
- **链路**：判断补丁是否实际应用成功。`patch_apply_begin`/`patch_apply_updated` 不持久化。

### `web_search_end` / `image_generation_end`
- **作用**：web 搜索 / 图片生成完成事件（`EventMsg::WebSearchEnd` / `ImageGenerationEnd`）。
- **链路**：用于 UI/回放展示动作结束；具体调用记录在对应的 `response_item(web_search_call / image_generation_call)`。`*_begin` 不持久化。

### `entered_review_mode` / `exited_review_mode`
- **作用**：进入/退出 code review 模式（`EventMsg::EnteredReviewMode(ReviewRequest)` / `ExitedReviewMode`）。
- **关键字段**：进入时 payload 是 `ReviewRequest`；退出时可带 `review_output`。
- **链路**：理解本轮是否切到代码审查流程及是否有最终结果。

### `thread_goal_updated`
- **作用**：长期 thread goal 状态更新（`EventMsg::ThreadGoalUpdated`）。
- **关键字段**：`objective`/`status`/`budget` 等。
- **链路**：了解当前线程目标是否完成或阻塞。

### `context_compacted`
- **作用**：上下文压缩完成的 legacy/UI 事件（`EventMsg::ContextCompacted`）。
- **链路**：展示"发生了压缩"；真正恢复上下文还要看顶层 `compacted` 和 `replacement_history`。

### `thread_rolled_back`
- **作用**：thread rollback 事件（`EventMsg::ThreadRolledBack`）。
- **关键字段**：`num_turns`。
- **链路**：恢复历史时据此丢弃最后 N 个用户 turn。

### `turn_aborted`
- **作用**：turn 被中断/取消（`EventMsg::TurnAborted`）。
- **关键字段**：`turn_id`/`reason`/`message`。
- **链路**：恢复与 UI 判断某轮未正常完成。

### `sub_agent_activity`
- **作用**：v2 path-based sub-agent 活动事件（`EventMsg::SubAgentActivity`）。
- **关键字段**：agent path、`activity`/`status`/`message`。
- **链路**：父线程看到的子 agent 进度。

### `item_completed`
- **作用**：新式 turn item 完成事件（`EventMsg::ItemCompleted`）。
- **链路**：rollout policy 当前只持久化没有等价 `response_item` 的 `Plan`/`Sleep` 这类 `item_completed`；其它会被过滤或转换为 legacy 事件。

### `raw_response_item` / `plan_update`（通常不在 rollout 中）
- **作用**：`EventMsg::RawResponseItem`（完整 ResponseItem 的事件封装）/ `EventMsg::PlanUpdate`（计划更新）。
- **链路**：协议层支持，但 rollout policy 默认**不持久化**。要看持久化的计划完成，关注 `item_completed` 中的 `Plan`；真正可恢复的模型 item 作为顶层 `response_item` 写入。

---

## 4. 典型一轮 turn 的事件链路（示例顺序）

```
session_meta                     ← 仅文件首部一次
turn_context                     ← 本轮环境快照
event_msg / user_message         ← 用户输入（UI 层）
response_item / message(user)    ← 真正发给模型的上下文（可能含注入内容）
event_msg / task_started         ← turn 开始边界
response_item / reasoning        ← 模型推理
response_item / function_call    ← 模型下发工具调用
response_item / function_call_output ← 工具返回（MCP 输出也转成这个）
event_msg / mcp_tool_call_end    ← MCP 工具的展示结果（若用 MCP）
event_msg / patch_apply_end      ← 若有 apply_patch
response_item / message(assistant) ← 模型最终回复（source of truth）
event_msg / agent_message        ← 回复的 UI 投影
event_msg / token_count          ← 本轮用量
event_msg / task_complete        ← turn 完成边界
```

排查问题时的实用经验：

- 想知道"模型实际看到/输出了什么" → 看 `response_item`（尤其 `message`、`reasoning`、`*_call/*_output`）。
- 想做 UI/时间线回放 → 看 `event_msg`（`user_message`/`agent_message`/`*_end`/`task_*`）。
- 想理解恢复/fork 行为 → 看 `turn_context`、`compacted`、`world_state`、`thread_rolled_back`。
- 工具调用配对 → 用 `call_id` 在 `*_call` 与 `*_output` 之间跳转（MCP 用 `mcp_tool_call_end` 的 `call_id`）。
- 看不到 `*_begin`/`*_delta`/approval 类事件是正常的：它们默认不写进 rollout。
