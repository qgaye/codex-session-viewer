# Codex Session Viewer

一个用于浏览 Codex 本地 session 记录的纯前端可视化工具。仓库当前没有构建流程、后端服务或外部依赖，所有界面、样式和功能逻辑都集中在 [`index.html`](./index.html)。

## 功能特性

- 扫描本机 `.codex/sessions` 目录，按项目、时间列出 session。
- 展示 session 的事件时间线，包括消息、工具调用、工具输出、上下文、token 使用和任务状态等记录。
- 按 turn 分组浏览，并在侧边栏显示 turn 索引、事件类型统计和事件类型说明。
- 支持按事件类型过滤，以及在 message payload 中搜索并高亮匹配内容。
- 针对常见 Codex 事件提供结构化详情面板，便于查看用户消息、助手消息、工具输入输出、MCP 调用和 patch 内容。
- 可根据 `call_id` 在工具调用输入和输出之间跳转。
- 支持导出脱敏后的 JSONL，默认会遮盖常见 token、Bearer authorization、JWT 和邮箱地址。
- 提供本地 Responses WebSocket 抓包命令，可在不修改 Codex 源码的情况下记录模型 WebSocket 帧，见 [`WS_CAPTURE_PROXY.md`](./WS_CAPTURE_PROXY.md)。
- 支持像浏览 session 一样浏览 WebSocket capture JSONL，按连接分组查看上行请求、下行响应、帧 payload 和原始记录。

## 快速开始

直接用浏览器打开 `index.html` 即可：

```bash
open index.html
```

推荐使用 Chrome 或 Edge。工具会优先使用浏览器的目录选择能力读取 `.codex` 目录；如果当前浏览器不支持，会退回到目录上传选择。

首次使用时：

1. 点击 `Choose .codex Directory`。
2. 选择用户目录下的隐藏目录 `.codex`，或直接选择其中的 `sessions` 目录。
3. 在弹出的 session 列表中选择要查看的记录。
4. 使用顶部搜索、事件类型筛选、左侧 turn 索引和中间时间线定位内容。
5. 需要分享或排查时，可点击 `Export Redacted` 导出脱敏 JSONL。

## 数据来源

Codex session 通常保存在：

```text
~/.codex/sessions/
```

工具会识别 `.jsonl`、`.json` 和 `.txt` 文件，并从文件前部抽取 session id、时间、项目路径和用户提示预览，用于生成 session 选择列表。

## 隐私说明

这个工具只在浏览器本地读取文件，不会主动上传 session 内容。目录权限由浏览器管理，已选择的目录句柄会保存在 IndexedDB 中，方便下次重新打开时继续使用。

导出功能会做基础脱敏，但脱敏规则不可能覆盖所有敏感信息。分享导出的 `.redacted.jsonl` 前，仍建议人工检查其中是否包含项目路径、命令输出、业务数据或其他私密内容。

## 开发说明

项目是单文件应用：

```text
index.html
```

修改时直接编辑该文件即可。主要结构如下：

- `<style>`：完整页面布局和组件样式。
- `<body>`：顶部工具栏、session 选择弹窗、左侧统计、中间时间线和右侧详情面板。
- `<script>`：session 目录扫描、JSONL 解析、事件归一化、过滤搜索、详情渲染、脱敏导出等逻辑。

由于没有构建步骤，保存后刷新浏览器即可验证变更。

## 注意事项

- Codex 的 reasoning 内容通常是加密或不可见的；本工具重点展示可见消息、工具调用、工具输出和可搜索的执行信号。
- 不同版本 Codex 的 session 事件结构可能变化。未知事件仍会以通用 payload 方式展示。
- 目录读取能力依赖浏览器实现；如果无法选择隐藏目录，可以尝试直接选择 `sessions` 目录，或换用 Chrome/Edge。
