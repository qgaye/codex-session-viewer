# 项目说明

本项目是一个独立的 Codex session JSONL 文件查看器，用于可视化本地 Codex session 记录，包括可见消息、工具调用、工具输出、MCP 事件、patch 以及其他 session payload。

全部逻辑都集中在 `index.html`，没有构建步骤——修改后直接刷新浏览器即可验证。

这些 JSONL 文件如何写入，其事实源（source of truth）是 upstream Codex 实现：

- 仓库：https://github.com/openai/codex
- 本地优先使用的 checkout：`../codex`

当你要修改解析逻辑、事件分类、schema 标签，或任何依赖 Codex session 结构的 UI 细节时，请先查阅 upstream Codex 源码。在可以查证 writer 实现的情况下，不要仅凭观察到的样本文件去推断 JSONL 语义。

如果本地没有 upstream checkout，请在做 schema 相关改动前先克隆最新的 `openai/codex` 仓库（在本仓库根目录下执行，确保 `../codex` 落到正确位置）：

```sh
git clone https://github.com/openai/codex.git ../codex
```

如果 checkout 已存在，直接将其作为参考源使用。仅在任务对时效性有要求时才 pull/fetch upstream，并且不要把 upstream Codex 的源码改动混入本查看器仓库。
