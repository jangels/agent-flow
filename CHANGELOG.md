# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-28

### Added

- **可视化画布编排**：React Flow 画布，六类节点（开始 / LLM 调用 / 条件分支 / 工具调用 / 人工审批 / 结束），连线即流程。
- **多流程管理**：流程 CRUD / 发布 / 导出，SQLite 存储（WAL 模式），已发布流程可按 `flow_id` 通过 HTTP 直调。
- **LangGraph 外挂执行**：`langgraph-bridge`（FastAPI + LangGraph）子项目承接整图执行，结果以 AG-UI 标准协议回流，Rust 薄适配层翻译为内部事件，前端零感知。
- **真实 MCP 接入**：bridge 通过 `langchain-mcp-adapters` 连接标准 MCP server（默认内置 stdio demo），`tool` 节点声明 `mcp_tool` 即真实调用。
- **ReAct 工具循环**：`tool` 节点内模型自主决定调哪个工具、调几次、何时收工（`max_iterations` 显式限轮），循环封在节点内、图的形状不变。
- **模型分支路由**：`condition` 节点填自然语言判据，由模型（结构化输出 + `temperature=0`）选边，判定结果进 state 与执行轨迹。
- **人工审批（HITL）**：`human` 节点运行期 `interrupt()` 挂起，SQLite checkpointer 落盘断点（跨进程重启可恢复），`POST /flows/{id}/resume` 携决策续跑，已执行节点不重跑。
- **发布即分享聊天页**：流程发布后生成链接，浏览器打开即是自包含聊天页（`public/chat.html`），访客输入即运行流程；挂起时页面内嵌审批卡片，可直接批准 / 驳回 / 人工改值。
- **一键启动脚本**：`scripts/start.sh`（gui / web / stop 三模式），bridge 健康检查 + 环境变量自动对齐下游。

### Security

- 当前所有入口（HTTP / 聊天页 / resume）均无鉴权，仅适合本机使用；详见 [SECURITY.md](./SECURITY.md)。
