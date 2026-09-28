# AGENTS.md — agent-flow AI 协作指引

> **本文件是本仓库对 AI coding agent 的唯一 canonical 指令文件**（Claude Code、Codex、Cursor、Gemini CLI 及其他 AGENTS-aware harness 通用）。`CLAUDE.md` 只是指向本文的指针，**不要把规则重复写进 `CLAUDE.md`**。人类入门读 `README.md`。
> 工作流：**先读「Cross-cutting invariants」→ 改代码 → 跑「Testing instructions」→ 按需更新「Project maintenance rules」里的文档**。
> 当前能力集：多流程可视化编排 · LLM 流式 · LangGraph 委托执行 · 真实 MCP · ReAct 节点内循环 · 模型分支路由 · **人工审批 HITL（checkpointer + 挂起/续跑）**。

---

## Project overview

macOS 桌面应用（Tauri v2 + React + Rust）做 **Agent 工作流可视化编排**：画布拖节点连线 → 一键运行 → LLM 流式 token 回流到节点。多流程管理，每个流程可独立编辑 / 发布 / 导出 / 执行。

**非目标**：不改造成 Web 应用；不引入 AG-UI 前端 SDK；不让 Python 侧写数据库。

---

### Core design

三层结构，**层与层之间只靠两个契约通信**（改任何一层前先认清契约）：

```
React 前端 (src/)                      ← 契约1: Tauri invoke 命令 + wf:event 事件
      │
Rust 引擎 (src-tauri/src/)             ← 契约2: HTTP + AG-UI 协议
      │
Python bridge (langgraph-bridge/)      ← LangGraph 执行 + SQLite 只读 + MCP 入口
```

#### 节点 `data` 字段（`src/types.ts` → `AgentNodeData`）

| 节点 | 字段 | 说明 |
|---|---|---|
| 通用 | `label` | 画布显示名 |
| `start` | `input` | 初始输入。**外部传入的非空输入优先**，节点配置的 `input` 只是兜底默认值 |
| `llm` | `system` / `prompt` / `model` | `model` 覆盖全局模型 |
| `condition` | `expression` | **分支判据（自然语言）**，如「上游内容是否在询问天气」。bridge 侧由模型在**该节点的出边**中选一条（S2）；Rust 侧仍是直通 + `warn` 告警。留空则 bridge 退回旧中文词表（`是/yes/true/可以/正确`） |
| `condition` | `model` | 判定模型覆盖。优先级 `data.model` > `cfg.router_model` > 全局 `cfg.model`；**路由建议填便宜快的小模型** |
| `tool` | `tool` | 本地 mock 回退用的工具名 |
| `tool` | `mcp_tool` | direct 模式：MCP 工具全名 `{server}__{tool}`，单工具直调 |
| `tool` | `tools` | **ReAct 模式**：工具白名单（数组；`["*"]` / `["all"]` = 全部）。**非空即启用节点内循环** |
| `tool` | `max_iterations` | ReAct 循环硬上限，默认 6 |
| `human` | `hitlPrompt` | **审批提示语**（给审核人看）。留空退回 `prompt`，再留空默认「请确认是否继续」 |
| `human` | `actions` | 候选动作数组，默认 `["approve","reject"]`。也支持自定义动作名（`format_decision` 会输出 `[动作名] ...`） |
| `human` | `allow_edit` | 是否允许审核人**改写要放行的内容**，默认 `true` |
| 运行时 | `awaiting` | `human` 节点挂起中（前端把节点染紫 + 弹审批控件） |
| 运行时 | `hitlInput` / `hitlThreadId` / `hitlDecision` / `hitlNote` | `human` 节点的审批上下文：待审文本、续跑用 threadId、人的决策与备注。**续跑必须带 `hitlThreadId`** |
| 运行时 | `status` / `output` | 不持久化为配置，由 `wf:event` 回填；`status` 多一个取值 `"awaiting"` |

> ⚠️ `human` 节点是**唯一**在「`data` 里没有配置也必须执行特殊逻辑」的节点——即使用户什么都不填，跑到它也必须挂起，不能当成空节点直通。

#### `wf:event` 内部事件（前端唯一消费模型）

| type | 关键字段 | 语义 |
|---|---|---|
| `node:start` | `id`, `nodeType`, `flowId` | 节点开始（UI 高亮 running） |
| `token` | `id`, `token` | LLM 流式分片（累加到节点 output） |
| `tool:start` | `id`, `toolCallId`, `toolName` | 工具调用开始 |
| `tool:args` | `id`, `toolCallId`, `delta` | 工具入参（增量） |
| `tool:result` | `id`, `toolCallId`, `toolName`, `content` | 工具返回 |
| `node:done` | `id`, `output`，condition 另带 `branch` / `branchLabel` / `branchReason` | 节点完成。**condition 的 branch 字段只在委托 bridge 时出现**（普通节点不带，前端据此区分） |
| `node:awaiting` | `id`, `threadId`, `prompt`, `input`, `actions`, `allowEdit`, `interruptId` | **HITL 挂起**（新增）。节点收到后置 `status="awaiting"` 并存下 `threadId`；`actions` 为空时前端补默认 `[approve, reject]` |
| `warn` | `id`, `message` | 引擎告警，只进 Console。来源：① Rust 原生路径遇出边 ≥ 2 的 `condition`；② **Rust 原生路径图中含 `human` 节点**（改走整图委托） |
| `workflow:done` | `result`, `paused` | 整流程结束（**成功与失败共用此事件**，失败时 result 带错误前缀）。`paused: true` 表示「本次 run 结束，但流程没结束，在等人」（HITL 第三态） |

#### AG-UI → `wf:event` 映射（在 `bridge.rs::translate_agui`）

| AG-UI 事件 | 结果 |
|---|---|
| `RUN_STARTED` | 忽略（不 emit）；`recursionLimit` / `hasCycle` 应用扩展字段同样被忽略，只供脚本消费 |
| `STEP_STARTED` | → `node:start` |
| `TEXT_MESSAGE_START` | **吸收**（只记 `messageId → node` 映射） |
| `TEXT_MESSAGE_CONTENT` | → `token`（用映射找回节点） |
| `TEXT_MESSAGE_END` | 吸收 |
| `TOOL_CALL_START` | → `tool:start`（记 `toolCallId → (node, name)`） |
| `TOOL_CALL_ARGS` | → `tool:args` |
| `TOOL_CALL_END` | 吸收（参数已由 ARGS 送达） |
| `TOOL_CALL_RESULT` | → `tool:result`（靠映射回查节点与工具名） |
| `STEP_FINISHED` | → `node:done`（`branch` / `branchLabel` / `branchReason` 扩展字段一并透传） |
| `RUN_PAUSED` | **→ `node:awaiting`**（HITL 自定义事件，非 AG-UI 标准）；`actions` 空则补 `["approve","reject"]`，`prompt` 空则补默认文案 |
| `RUN_FINISHED` | → `workflow:done`（`result`；**`paused` 标志一并透传**） |
| `RUN_ERROR` | → `workflow:done`（`result = "[AG-UI RUN_ERROR] {msg}"`） |
| `STATE_*` / `CUSTOM` / `RAW` / 未知 | 忽略 |

> 应用扩展字段：`STEP_FINISHED.output` 与 `RUN_FINISHED.result` 是**非 AG-UI 标准**的补充字段，用于回填内部模型。新增扩展字段请在同一处注释说明。
> **`RUN_PAUSED` 同理**：AG-UI 标准里没有「暂停」语义，故自定义；配套约定是**暂停后必补一条 `RUN_FINISHED` 且 `paused: true`**——只发 `RUN_PAUSED` 会让调用方以为流断了。

#### SQLite schema（`~/.agent-flow/agent_flow.db`）

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE flows (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    graph      TEXT NOT NULL DEFAULT '{"nodes":[],"edges":[]}',  -- JSON 文本
    status     TEXT NOT NULL DEFAULT 'draft',                    -- draft | published
    updated_at TEXT NOT NULL DEFAULT ''                          -- epoch ms 字符串
);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- kv 的 key：active_id | config | legacy_workflow
```

`Flow` 结构体（`lib.rs` 47 行）= 前端 `Flow`（`types.ts` 53 行）：`{id, name, version, graph{nodes,edges}, status, updated_at}`。

> **HITL 断点不在这个库里**：LangGraph checkpointer 写的是同目录的 `checkpoints.db`（`AsyncSqliteSaver` 管理，schema 由 langgraph-checkpoint 定义）。两张库**互不干涉**：删 `flows` 行不会清掉别人等待中的断点。

#### bridge HTTP 端点

| 端点 | 作用 | 错误 |
|---|---|---|
| `POST /run` | 整图执行，body 直接带 `{nodes, edges, input, config, mcp_servers}` | — |
| `POST /flows/{id}/resume` | **HITL 续跑**：body `{threadId, decision}`，用同一 thread 从断点继续 | 404 不存在 / **422 缺 `threadId` 或 `decision`** / 图无挂起点 → 可读错误（**注意：不检查 published**） |
| `GET /flows` | 列流程，默认只列 published，`?all=true` 含草稿 | — |
| `GET /flows/{id}` | 单流程元信息（节点/边数量） | 404 |
| `POST /flows/{id}/run` | **按 id 执行**：bridge 自取图，无需调用方知道图结构 | 404 不存在 / 409 未发布 / 422 空图 / 500 执行失败 |
| `GET /mcp/tools` | 已加载的真实 MCP 工具名 | — |
| `GET /chat.html` | 托管 `public/chat.html` 静态页——**聊天页与 API 同源，fetch 零 CORS** | 404 文件缺失 |
| `GET /health` | 健康检查（含 db 路径 + **`checkpointer` 类型**） | — |

`POST /flows/{id}/run` body（全部可选）：`input` / `config`（**不传 = 自动读 SQLite `kv['config']`**；传 `{}` = 强制零 key Mock）/ `mcp_servers` / `recursion_limit` / `stream`（默认 `true` SSE，`false` 返 JSON，返回体含 `recursionLimit` 与 `hasCycle`；**图含 `human` 时另含 `paused` / `awaiting` / `resumeUrl`**）。body 由 Pydantic 模型 `RunByIdBody` / `ResumeByIdBody` 声明（`extra="allow"`，字段全可选）——**Swagger 因此能渲染 Request body 表单**；改字段请同步改模型，别退回裸 `req.json()`。

`POST /flows/{id}/resume` body：

| 字段 | 必填 | 说明 |
|---|---|---|
| `threadId` | ✅ | 挂起时从 `RUN_PAUSED` / `awaiting.threadId` 拿到。**不传或传错都 422 / 报可读错误**，绝不静默重跑 |
| `decision` | ✅ | `{decision: "approve"\|"reject"\|自定义, value?, note?}`。**缺字段报错而非默认放行——审批的默认答案永远不能是通过** |
| `stream` | ❌ | 同上，默认 SSE |

**发布即分享（chat.html）**：`public/chat.html` 同时被 vite（dev 调试）与 bridge `GET /chat.html`（**同源托管，生产推荐**）服务。页面读 URL query `flowId` / `name`，以非流式 `POST /flows/{flowId}/run` 拿结果回填气泡，对 `ok` / `paused`（人工审批）/ `error` 三态都有处理；bridge 地址优先级 = query `bridgeUrl`（旧链接兼容）> **bridge 同源**（`location.port === "8010"`，零 CORS）> `http://127.0.0.1:8010`。前端 `togglePublish` 发布成功后调用 `get_bridge_url()` 拼出 `{bridge}/chat.html?flowId=...&name=...` 弹窗给用户。bridge 不生成页面（只托管静态文件），职责单一为 API。

> 为什么同源托管：跨源方案下 preflight 响应可能被浏览器代理/扩展劫持改写（实战出现过 `ACAO: http://localhost:8080` 导致 preflight 失败），同源请求根本不发 preflight，此类环境问题整类消失。

#### 环境变量

| 变量 | 作用 | 默认 | 读取方 |
|---|---|---|---|
| `AGENT_FLOW_HOME` | 数据目录 | `~/.agent-flow` | Rust + Python |
| `AGENT_FLOW_DB` | 直接指定 `.db` 文件（支持 `~`） | `<data_dir>/agent_flow.db` | Python |
| `AGENT_FLOW_CHECKPOINT_DB` | **HITL 断点库路径**。⚠️ **写测试必须在 import server 之前设到 tempdir**，否则会污染真实 `~/.agent-flow/` | `<data_dir>/checkpoints.db` | Python `server.py` |
| `LANGGRAPH_BRIDGE_URL` | bridge 地址（给 `.../run` 或裸 base 都会被规整） | `http://127.0.0.1:8000` | Rust `bridge.rs` + Python `flows_server.py` |
| `AGENT_FLOW_RECURSION_LIMIT` | 执行步数上限（优先级低于 body 的 `recursion_limit`，高于按图推算） | 无（按图推算：无环 `max(25, n*4)` / 有环 `max(12, n*2)`） | Python `server.py` |
| `LANGGRAPH_DEFAULT_RECURSION_LIMIT` | LangGraph 内部默认上限 | **10007** | langgraph 内部——⚠️ **本服务不再依赖它**（`_resolve_recursion_limit` 一律显式传值） |

---

## Technology stack

| 层 | 技术 | 职责 |
|---|---|---|
| 前端 | React 18 / @xyflow/react / zustand | 画布、多流程 UI、消费 `wf:event` 渲染 |
| Rust 引擎 | Tauri 2 / rusqlite | 拓扑执行、LLM 流式调用、SQLite 唯一写入口、AG-UI 翻译 |
| Python bridge | FastAPI / LangGraph / FastMCP | 整图委托执行、按 `flow_id` 执行、MCP 工具暴露、**HITL 挂起与续跑（checkpointer）** |

依赖管理：前端 npm（`package-lock.json` 锁版本）；Rust cargo（`src-tauri/Cargo.lock`）；Python **uv**（`langgraph-bridge/uv.lock`，**不要在系统 Python 里装依赖**）。CI 会以 `--frozen` 模式校验锁文件，改依赖必须同步提交锁文件。

---

## Repository layout

| 路径 | 职责 | 改它时的注意 |
|---|---|---|
| `src/App.tsx` | 画布 + 多流程生命周期 + 运行编排 | `run()` 里 **先 `listen` 再 `invoke`**，顺序不能反；事件按 `flowId` 过滤（304 行）；`onConnect`（158 行）目前无环校验；**`handleWfEvent` 供 run / resume 共用**，`resumeApproval` 负责续跑，改它时注意 `runningRef` 不可退化成 state 进 `nodeTypes` 依赖 |
| `src/lib/tauri.ts` | **所有** `invoke` / `listen` 的唯一封装 | 新增 Tauri 命令必须在此外加一行，禁止在组件里裸调 `invoke` |
| `src/types.ts` | 前端类型，**与 Rust 结构体镜像** | 改 Rust 结构体字段名必须同步改这里 |
| `src/store.ts` | zustand（config / logs / running） | — |
| `src/components/` | `FlowList` `Toolbar` `nodes` `Inspector` `Console` `Settings` | 节点视觉改 `nodes.tsx`；新增节点还得动 `Toolbar.tsx` 的 `PALETTE` |
| `public/chat.html` | **发布即分享聊天页**（独立于 React app 的静态页） | 从 URL query 读 `flowId`/`name`（`bridgeUrl` 可选，bridge 同源托管时不需要），fetch `/flows/{id}/run`；改样式/逻辑直接编辑此文件，无构建步骤 |
| `src-tauri/src/lib.rs` | Tauri 命令 + **SQLite 存储层** + 数据目录解析 | 命令要同时加进 `invoke_handler!`；数据目录逻辑改动见「Cross-cutting invariants 1」 |
| `src-tauri/src/workflow.rs` | Kahn 拓扑排序 + 节点执行 + `wf:event` 发射 | **环在这里被拦下**（67-72 行；Kahn 排不完只可能是环，孤立节点 in-degree=0 照样能排）；`condition` 直通 + `warn` 告警在 140-165 行；`tool` 节点委托 bridge 的分支在 166-193 行；**`human` 节点图级预检在 `node_map.is_empty()` 判断之后**——发现 human 就发 `warn` 并整图委托 |
| `src-tauri/src/llm.rs` | OpenAI 兼容 `/chat/completions` 流式调用 | 只发 `token` 事件的来源；SSE 逐行解析 |
| `src-tauri/src/bridge.rs` | **AG-UI → wf:event 唯一适配层**（`translate_agui`） | 新增协议事件只准在这里翻译；`TranslateState` 维护 `messageId→node` / `toolCallId→(node,name)` 映射（ReAct 多轮靠 `toolCallId` 的轮次后缀隔离）；`STEP_FINISHED` 的 `branch*` 扩展字段透传为 `node:done` 的 `branch/branchLabel/branchReason`；**`RUN_PAUSED` → `node:awaiting`（HITL）**；`bridge_origin()` 负责剥离 `LANGGRAPH_BRIDGE_URL` 的路径，**run / resume 共用 `stream_agui`** |
| `langgraph-bridge/server.py` | FastAPI：`/run`、`/flows*`、`/flows/{id}/run`、`/flows/{id}/resume`、`/mcp/tools`、`/chat.html`、`/health`；LangGraph 构图执行；AG-UI server | 构图/执行在 `_run_graph` / `_compile_graph`；**condition 判定在 `_condition_node`（647 行）**，`router` 只查表；**ReAct 循环在 `_react_tool_node`（499 行）**；**HITL 内核：`_human_node`（调 `interrupt()`）、`get_checkpointer()`、`_resume_graph()`（`Command(resume=…)` + `aget_state` 预检）、`_finalize()`（补发 `RUN_PAUSED` + `RUN_FINISHED{paused:true}`）**；环检测 `_find_cycle` / 上限 `_resolve_recursion_limit`；LLM 走 `MockChatModel` / `MockReActModel` / `MockRouterModel` / `ChatOpenAI` |
| `langgraph-bridge/flow_store.py` | Python 侧 SQLite **只读**访问层（被 server 与 flows_server 共用） | 路径解析必须与 Rust 一致；禁止在这里开写连接 |
| `langgraph-bridge/mcp_servers/flows_server.py` | stdio MCP：`list_published_flows` / `run_flow` | 执行走 `POST /flows/{id}/run`（`stream:false`），不要另写执行逻辑 |
| `langgraph-bridge/mcp_servers/demo_server.py` | demo MCP server（FastMCP stdio） | 供零 key 演示真实 MCP 调用 |
| `scripts/start.sh` | 一键启动：`gui` / `web` / `stop` 三模式 | 自动拉起 bridge（默认 8010）并写 `.bridge.pid`；`LANGGRAPH_BRIDGE_URL` 在脚本顶部 export |

**运行时数据目录**（默认 `~/.agent-flow/`，可用 `AGENT_FLOW_HOME` 覆盖）：`agent_flow.db`（设计期：流程图 / 配置 / 发布状态，**Rust 唯一写入口**）、`checkpoints.db`（运行期：HITL 断点快照，bridge 侧 `AsyncSqliteSaver` 写，含 `-wal` / `-shm` 边车）。两张库互不干涉。

---

## Build and test commands

### 前端 / 桌面

```bash
npm install                 # 首次
npm run tauri:dev           # 开发（自动起 Vite 1420 + 编译 Rust + 开窗）—— GUI 闭环只能这样验
npm run tauri:build         # 打包 .app / .dmg → src-tauri/target/release/bundle/
npm run typecheck           # tsc --noEmit —— 改完前端必跑
npm run build               # vite build → dist/
```

### Rust

```bash
cd src-tauri
cargo check                 # 快速编译校验
cargo test                  # 单测：lib（数据目录解析）3 个 + bridge（AG-UI 翻译，含 ReAct 多轮隔离 / condition 分支透传 / RUN_PAUSED 映射 / bridge_origin 归一化）10 个 = 13 个
cargo build --release
```

### Python bridge（uv 管理，**不要在系统 Python 里装依赖**）

```bash
cd langgraph-bridge
uv run uvicorn server:app --host 127.0.0.1 --port 8010    # 注意：不要用 8000，见「Platform notes」
uv run python tests/test_react.py                         # ReAct 工具节点自测（58 项断言，含整图执行）
uv run python tests/test_condition.py                     # 条件分支 / 环检测 / 执行上限自测（55 项断言）
uv run python tests/test_hitl.py                          # HITL 挂起/续跑自测（50 项断言；末尾会 close_checkpointer）
uv run python mcp_servers/flows_server.py                 # stdio MCP server（由 MCP client 拉起，别手跑，会等 stdin）
uv run python mcp_servers/demo_server.py                  # demo MCP server（stdio；由 bridge 自动拉起，手跑也只是等 stdin）

curl -s localhost:8010/health
curl -s localhost:8010/flows
curl -s localhost:8010/mcp/tools
```

一键脚本：`./scripts/start.sh`（GUI）/ `./scripts/start.sh web`（bridge + vite）/ `./scripts/start.sh stop`。

---

## Platform notes

- **仅在 macOS 上开发与验证**：Windows / Linux 未测过；GUI 闭环（画布、事件回流）headless 无法替代，必须本机 `npm run tauri:dev`。
- **端口**：bridge 用 **8010**。不要用 8000——本机已有服务绑 `*:8000`；换端口时把 `LANGGRAPH_BRIDGE_URL` 指到同端口。
- **路径不依赖 cwd**：Tauri 双击启动时进程 cwd 可能是 `/`，一切路径走 `$HOME` / 绝对路径 / 数据目录。
- **DMG 打包**：headless 环境无 `hdiutil` 依赖的显示环境会失败——只影响 `.dmg`，`.app` 不受影响。
- **构建机架构 ≠ 目标机架构**：打出的 `.app` 在别的 Mac 跑不了，在目标机上重跑 `npm run tauri:build`。
- **Release 权限 ACL**：自定义 Tauri 命令在 Release 下需在 `src-tauri/capabilities/default.json` 显式授权（Debug 自动放开）。

---

## Code style guidelines

- **匹配周边代码的既有惯例**：命名、注释密度、模块结构。注释解释 *why*，不复述 *what*。
- **小步、行为保持的改动**：不顺手重构、不做相邻功能夹带、没有真实调用方就不新增公开接口。
- **契约镜像必须同步**：`src/types.ts` 与 Rust 结构体互为镜像；改 Rust 字段名必须同步改 TypeScript，反之亦然。
- **前端不裸调 Tauri**：所有 `invoke` / `listen` 只经 `src/lib/tauri.ts` 封装；组件直接调 `invoke` 视为 bug。
- **Rust 运行时路径避免 `unwrap` / `expect`**：返回可读 `String` 错误；panic 只允许出现在测试里。
- **Python 警惕 falsy 陷阱**：默认值判断用显式 `is not None`，不要 `x.get(k) or default`（会把 `0` / `[]` / `""` 误吞，本项目已三次踩坑）。
- **哨兵值不用 `None` 判**：需要区分「明确提交了 null」与「根本没传」时，用 `_NO_RESUME = object()` 这类专用哨兵。
- **错误对人是可读的**：HTTP 层报错给可读信息而非裸异常；`GraphRecursionError` 等内部异常必须转译。
- **不提交密钥**：`.gitignore` 已排除 `.env` / `*.local`；API key 只应存在于 `~/.agent-flow/agent_flow.db` 的 `kv['config']`。

---

## Cross-cutting invariants (do not violate)

1. **数据目录常量双端一致**：`DATA_DIR_NAME = ".agent-flow"` 同时存在于 `src-tauri/src/lib.rs`（86 行）与 `langgraph-bridge/flow_store.py`（35 行）。改一处必须改另一处，`cargo test` 有专项单测守着。
2. **不依赖系统 app 数据目录**：禁止把 `~/Library/Application Support`、`%APPDATA%`、`$XDG_CONFIG_HOME` 作为日常读写位置（`legacy_config_dir()` **仅**用于一次性迁移旧数据，不得用于日常读写）。
3. **SQLite 只有一个写入口（Rust）**：Python 侧一律 `mode=ro` 只读连接。两端靠 WAL + `busy_timeout=5000` 并发，读不阻塞写。
4. **发布是状态位，不是快照**：`publish_flow` 只翻 `status` 并让 `version += 1`；「已发布」= 进入可独立触发的集合（HTTP / MCP 可见集）。
5. **每个 `wf:event` 必带 `flowId`**：前端靠它做多流程归属过滤，漏带会导致切换流程时事件串台。
6. **前端不感知 AG-UI**：AG-UI 概念只能在 `bridge.rs` 出现；换前端框架时应替换适配层，而不是让组件认识标准事件。
7. **新增 Tauri 命令三件套**：`lib.rs` 写函数 → 加进 `invoke_handler!` → `src/lib/tauri.ts` 封装。**Release 构建还需**在 `src-tauri/capabilities/default.json` 授权（Debug 自动放开，Release 会被 ACL 拦）。
8. **路径不依赖 cwd**：Tauri 双击启动时进程 cwd 可能是 `/`，一切路径走 `$HOME` / 绝对路径 / 数据目录。
9. **HITL 三原则（审批类改动必须遵守）**：① **不得有默认决策** —— resume 缺 `decision` 一律报错，绝不默认放行（「审批的默认答案永远不能是通过」）；② **不得静默跳过审批** —— Rust 原生执行器遇到 `human` 节点必须整图委托给 bridge，不能当成空节点直通；③ **不得用 `None` 判哨兵** —— `resume` 参数用 `_NO_RESUME = object()` 区分「人提交了 `null`」与「没走 resume 路径」。
10. **`interrupt()` 不抛异常**：每次 `ainvoke` 之后**必须**主动检查返回值里的 `__interrupt__`（统一走 `_interrupt_of(result)`）。忘了查 → 症状极隐蔽：流程看起来「正常跑完了」，其实后半截从未执行。
11. **checkpointer 是进程级单例，路径在 import 时定死**：写测试必须在 **import `server` 之前**设 `AGENT_FLOW_CHECKPOINT_DB`（连同 `AGENT_FLOW_HOME` / `AGENT_FLOW_DB`）到临时目录；写测试**末尾必须** `await server.close_checkpointer()`（aiosqlite 线程非 daemon，不关就进程不退出）。
12. **环路保护对称（S2 已修复）**：两条执行路径对环的拦截能力必须一致——

| 入口路径 | 环检测 | 执行上限 |
|---|---|---|
| GUI（Rust `run_flow` / `workflow.rs`） | ✅ Kahn 拓扑排序先拦（67-72 行）→ Err | 不涉及 |
| HTTP `POST /run`、`POST /flows/{id}/run` | ✅ **DFS 环检测 + 服务端日志**（`_find_cycle`，730 行；结果进 `RUN_STARTED.hasCycle`） | ✅ **一律显式** `recursion_limit`（`_resolve_recursion_limit`，769 行） |
| 前端连线（`App.tsx::onConnect`） | ⚪ 仍无校验，**有意保留**（画环是合法需求；真正该拒绝的是不支持环的执行器，它已拦） | 不涉及 |

    上限优先级：`wf.recursion_limit` > `AGENT_FLOW_RECURSION_LIMIT` 环境变量 > 按图规模推算（无环 `max(25, n*4)`；**有环收紧到 `max(12, n*2)`**）。超限时 `GraphRecursionError` 被转成可读 `RUN_ERROR`，不再抛裸异常。

    > 📌 **bridge 侧刻意不拒绝有环图**：LangGraph 原生支持循环（`condition` 回边即合法循环，这是它相对静态 DAG 引擎的核心能力）。拒绝方应该是 Rust 原生执行器。

---

## Testing instructions

**按改动面裁剪，改完必跑**：

```bash
# 前端改动
npm run typecheck                       # 必须 EXIT=0
# Rust 改动
cd src-tauri && cargo test && cargo check
# Python bridge 改动
cd langgraph-bridge && uv run python tests/test_react.py       # ReAct 节点 + 整图执行，期望 58/58
uv run python tests/test_condition.py                          # 条件分支 / 环检测 / 执行上限，期望 55/55
uv run python tests/test_hitl.py                               # HITL 挂起/续跑/人工编辑/thread 隔离/落盘，期望 50/50
uv run uvicorn server:app --port 8010 &
curl -s localhost:8010/health           # 先轮询到 ok 再发请求（SSE 空输出的头号原因就是没起来）
curl -s localhost:8010/flows
curl -sN -X POST localhost:8010/flows/<flow_id>/run \
     -H 'Content-Type: application/json' -d '{"input":"计算 3 加 5"}'   # 期望完整 AG-UI 事件流
curl -s  -X POST localhost:8010/flows/<flow_id>/run \
     -H 'Content-Type: application/json' -d '{"stream":false}'          # 期望 {ok, result, steps, recursionLimit, hasCycle}
# ReAct 专项：图的 tool 节点只应出现一次 STEP_STARTED，内部可有 N 轮 TOOL_CALL_*
curl -sN -X POST localhost:8010/flows/<flow_id>/run -H 'Content-Type: application/json' -d '{}' \
  | grep -cE 'STEP_STARTED.*n_react'        # 期望 1（循环不下沉到图层面）
# condition 专项：STEP_FINISHED 带 branch*，未选中的分支节点不应出现 STEP_STARTED
curl -sN -X POST localhost:8010/run -H 'Content-Type: application/json' -d @cond_graph.json \
  | grep -E 'RUN_STARTED|STEP_FINISHED.*n_cond'   # 期望 recursionLimit/hasCycle + branch/branchLabel/branchReason
# 含环专项：应在数秒内 RUN_ERROR（而非跑到 10007 步）
curl -s -X POST localhost:8010/flows/<loop_flow_id>/run -H 'Content-Type: application/json' -d '{"stream":false}' \
  | grep -o '"hasCycle":[a-z]*,"recursionLimit":[0-9]*'   # 期望 hasCycle:true + 收紧后的上限
# HITL 专项（4 步，缺一不可）
R=$(curl -s -X POST localhost:8010/flows/<hitl_flow_id>/run -H 'Content-Type: application/json' \
     -d '{"input":"报销 1280 元","stream":false}')
echo "$R" | grep -o '"paused":true'                    # ① 期望命中：停住了
TID=$(echo "$R" | grep -o '"threadId":"[^"]*"' | head -1 | cut -d'"' -f4)
curl -s -X POST localhost:8010/flows/<hitl_flow_id>/resume -H 'Content-Type: application/json' \
     -d "{\"threadId\":\"$TID\",\"decision\":{\"decision\":\"approve\"}}" \
  | grep -o '"steps":[0-9]*'                           # ② 期望 steps=2（start 没重跑，证明是断点续跑）
curl -s -X POST localhost:8010/flows/<hitl_flow_id>/resume -H 'Content-Type: application/json' \
     -d '{"decision":{"decision":"approve"}}' | grep -o '"status":422\|threadId'   # ③ 缺 threadId → 422
# ④ 跨进程存活：挂起后 kill 掉 uvicorn → 重启 → 用旧 TID 续跑，仍应成功
# GUI 闭环（headless 无法替代，必须本机跑）
npm run tauri:dev
```

错误码自检：草稿流程 `409`、不存在 `404`、空图 `422`、resume 缺参 `422`、无挂起点 → 可读错误（非 5xx）。

**测试编写规则**（本项目特有的坑，别重复踩）：

- **每个 bug 修复配一个回归测试**；条件分支、thread 隔离、falsy 边界（`0` / `[]` / `None`）尤其要。
- **checkpointer 相关测试**：import `server` 之前必须设好三个环境变量到 tempdir（见硬约束 11）；每个用例用独立 `thread_id`（`_tid()` uuid），固定 thread 会让 state 跨用例泄漏。
- **测试末尾 `await server.close_checkpointer()`**，否则 aiosqlite 非 daemon 线程卡死进程（全 PASS 也不退出）。
- **mock 模型只喂最小上下文**：`MockRouterModel` 喂上游内容而非完整 prompt（判据文本会被 2-gram 规则自我命中）。
- 文件系统测试一律用 tempdir，不依赖真实 `$HOME`。

**踩坑台账（troubleshooting，踩过的别重复踩）**：

| 现象 | 根因 | 处置 |
|---|---|---|
| bridge SSE 返回空 / 前端无输出 | 服务没起来就 POST | 先轮询 `GET /health` 再发请求 |
| 工具结果里出现 `[{'type': 'text', ...}]` | MCP content-block 列表未规整 | 已由 `_normalize_mcp_output()` 提取 text |
| 流程事件串台 | 事件漏带 `flowId` | 检查 emit 处是否附了 `flowId` |
| ReAct 多轮工具名串台 | 多轮复用了同一个 `toolCallId` | 每轮用 `tc_<nid>_r<轮次>_<序号>`；Rust 侧 `tool_map` 在 RESULT 时 `remove` |
| ReAct 循环疑似不收敛 | 未设 `max_iterations`，吃隐式递归上限 | 显式设轮数上限（默认 6），别依赖 LangGraph 的 10007 |
| 白名单填了却走成 direct | 名字与已加载工具不匹配 | `curl /mcp/tools` 对真实键名；服务端会打 `[ReAct] … 未匹配到任何工具` 日志 |
| `max_iterations: 0` 不生效 | `0 or 6` 的 falsy 陷阱 | 已改为显式 `is not None` 判断 + `<1` 兜底 |
| condition 判定恒定走「命中」分支 | `MockRouterModel` 拿到的是**完整 prompt**，判据文本自己命中了 2-gram 关键词 | `_condition_node` 的 mock 分支只传 `[HumanMessage(content=inp)]`（真实模型靠语义理解，不受影响） |
| `condition` 配了判据却不生效 | Rust 原生执行器只做直通，会执行**全部**出边 | 分支路由需委托 bridge；Rust 侧已主动发 `warn` 事件（不再静默） |
| 含环流程「卡住不动」 | 旧版吃 LangGraph 默认 `recursion_limit=10007` | 已修：一律显式传上限，有环收紧到 `max(12, n*2)`，1.7s 内报错 |
| 流程看起来「跑完了」，其实后半截没执行 | **`interrupt()` 不抛异常**，`ainvoke` 正常返回 | 每处 `ainvoke` 后都要查 `__interrupt__`（`_interrupt_of(result)`），否则暂停被当成正常结束 |
| resume 请求成功但流程「从头又跑了一遍」 | `threadId` 不对（换成了新 run 的 id），实际开了条新流 | 用挂起时 `RUN_PAUSED` 返回的那个 `threadId`；服务端已加 `aget_state` 预检，无挂起点会报可读错误而非静默重跑 |
| 图里明明有 `human` 节点但没挂起 | 走了 Rust 原生执行器 / 没走 bridge | Rust 侧已主动发 `warn` 并把整图委托给 bridge；确认 `LANGGRAPH_BRIDGE_URL` 可达 |
| 测试全 PASS 但进程不退出（不限 HITL） | 整图执行 → `_compile_graph` 无条件挂 checkpointer，aiosqlite 工作线程非 daemon，连接未关 | 每个测试脚本入口统一 `asyncio.run(S.close_checkpointer())`（test_react / test_condition / test_hitl 均已接入） |
| 测试把断点写进了真实 `~/.agent-flow/` | checkpointer 是**进程级单例**，在 import `server` 时按环境变量初始化 | 测试必须在 **import `server` 之前**设 `AGENT_FLOW_CHECKPOINT_DB`（连同 `AGENT_FLOW_HOME` / `AGENT_FLOW_DB`）指向 `tempfile.mkdtemp()` |
| 固定 `thread_id` 的测试用例间状态泄漏 | checkpointer（SQLite）持久化，多 run 场景 state 跨用例累积 | 每个用例用独立 `_tid()`（uuid）；需要断点续跑语义的用例在用例内部自持 thread |
| 前端画布「闪一下全重挂载」 | 依赖数组里混入了高频状态，`nodeTypes` 重建 | 高频状态（`running`）用 `ref` 读，别进 `useMemo` 依赖 |
| 传了 `mcp_servers: []` 却用上了默认 demo server | `if mcp_servers else _demo_mcp_servers()` 的 falsy 陷阱 | 已修：`servers = _demo_mcp_servers() if mcp_servers is None else mcp_servers` |
| 暂停时 Rust 报「bridge 没返回终止事件」 | 用 `result.is_empty()` 判断是否收到终止事件，暂停天然无最终输出 | 改用 `saw_terminal` 标志，只有真没收到终止事件才报 Err |
| `Markdown`/CSS 编辑后样式错乱 | 编辑时吞掉了换行/大括号 | 改 CSS 后 `grep -c '{' ` 与 `}` 计数要相等 |
| 本地验证 lint 与 CI 不一致 | `uvx` 缓存了旧版 ruff，规则集与项目锁定的版本不同 | 验证 lint 必须用 `uv run`（项目 env），别用 uvx |

> 平台相关的坑（端口、cwd、DMG、架构、Release ACL）已移至「Platform notes」。

---

## Security considerations

- **绝不提交密钥**：API key 只应存在于 `~/.agent-flow/agent_flow.db` 的 `kv['config']`；`.gitignore` 已排除 `.env` / `*.local`。提交前自查 diff。
- **HITL 鉴权缺口是已知且未修**：`/flows/{id}/resume` 当前**无审批人身份校验**，任何能访问端口的人都能替你点「通过」。上生产前必须先补鉴权 —— 这条不是「待优化」，是**上线阻塞项**。详见 `SECURITY.md`。
- **bridge 无鉴权，CORS 刻意不开通配**：`CORSMiddleware` 的 `allow_origins` 只收敛到 `localhost:1420`；放宽 origin 前先想清楚这等于把「谁都能替你审批」的暴露面扩大到任意网页。**聊天页已改为 bridge 同源托管**（见 Core design「发布即分享」），同源请求不发 preflight，不依赖 CORS 放行。
- **默认监听 `127.0.0.1`**：改绑 `0.0.0.0` 属于安全语义变更，需要同步补鉴权设计。
- **demo MCP server 仅供演示**：不要在真实执行路径里默认携带。

---

## Project maintenance rules

- **版本与变更**：`CHANGELOG.md` 按 Keep a Changelog 格式维护；**用户可见行为变更必须在同一次提交里补条目**。提交信息遵循 Conventional Commits（CI 有 lint 门禁）。
- **CI 门禁（GitHub Actions）**：`ci.yml` = 前端 typecheck/build + oxlint、`cargo test` + clippy `-D warnings` + fmt、Python 三套测试 + ruff；`security.yml` = gitleaks（密钥全历史扫）/ osv-scanner（npm/uv/cargo CVE）/ cargo-audit，每周全量复扫。push 前本地先跑对应 lint（Python 用 `uv run`，别用 uvx）。
- **依赖升级**：升级后必须回归三套测试 + `cargo test` + `npm run typecheck`（实战：vite 5→8、rustls 0.23.43→0.23.45 均为修 CVE）。osv-scanner 的豁免清单在 `src-tauri/osv-scanner.toml`（⚠️ 配置不向子目录传播，必须与被扫锁文件同目录），只允许登记「无补丁版本的 unmaintained 传递依赖」并写明理由。
- **文档同步**：改完功能**先更新 `README.md` 的「项目结构 / 工作原理 / 已知占位」三节，再回头检查本文的契约表与踩坑台账是否过期**。
- **新节点 / 新命令 / bridge 委托 / MCP / ReAct / condition / HITL 的改造步骤**见下方 recipes——改这类功能前先对号入座，别凭感觉加文件。

### Common recipes（常见改造）

**A. 新增节点类型（6+1 处）**

1. `src/types.ts` → `NodeType` 联合类型
2. `src/components/nodes.tsx` → 渲染组件 + `nodeTypes` 映射
3. `src/components/Toolbar.tsx` → `PALETTE` 加一项
4. `src/App.tsx` → `DEFAULT_DATA` 加默认 data
5. `src-tauri/src/workflow.rs` → `match ntype` 加执行分支
6. （若 bridge 也要支持）`langgraph-bridge/server.py` → `make_node` 内的 `if/elif ntype` 链

**B. 新增 Tauri 命令**

`lib.rs` 函数（标 `#[tauri::command]`，`async fn` 需要 `Result<_, String>`）→ `invoke_handler!` 注册 → `src/lib/tauri.ts` 封装 → Release 前补 capabilities 授权。

**C. 让某个流程走 bridge 委托**

`tool` 节点满足以下任一条件即委托给 langgraph-bridge（`workflow.rs` `tool` 分支）：

- `data.tool = "langgraph"`
- `data.bridge = true`
- `data.tools` 非空（ReAct 白名单，如 `["demo__add"]` 或 `["*"]`）
- `data.mcp_tool` 非空（direct 单工具，如 `demo__add`）

语义：Rust 跑完前驱后，把**整张图** POST 给 bridge 再跑一遍（演示性接管，非增量子图）。

**D. 接自己的 MCP server**

`POST` body 顶层传 `mcp_servers: [{"name":"x","transport":"stdio","command":"python","args":["/abs/path/server.py"]}]`（也支持 `sse` / `streamable_http`）；`tool` 节点 `data.mcp_tool` 填 `{server}__{tool}`（如 `demo__add`）。

> ⚠️ 命名双轨（正常现象，别当 bug）：`/mcp/tools` 列出的是**带前缀**键名（`demo__add`），而 `TOOL_CALL_START.toolCallName` 实测是 LangChain 侧的**裸名**（`add`）；前端只负责展示，做匹配时用 `mcp_tool` 的带前缀名。

**E. 用 ReAct 模式让模型自己决定调哪个工具**

`tool` 节点 `data.tools` 填**工具白名单**（数组，或逗号 / 换行分隔的字符串；`["*"]` / `["all"]` = 全部已加载工具），即进入 ReAct 模式：

| 模式 | 触发 | 行为 |
|---|---|---|
| `direct` | `data.mcp_tool` 有值、`data.tools` 空 | 单工具直调（旧行为，勿破坏） |
| `react` | `data.tools` 非空 | 模型在**节点内部**循环：决定调哪个、几次、何时收工 |

硬约束：

- `data.max_iterations` 显式限轮（默认 6；`<1` 兜底为 1）。**注意别写成 `data.get(...) or 6`** —— `0` 是 falsy 会被误吞成 6
- **循环必须封在节点内部**，不要在图上造环。对外只发一次 `STEP_STARTED` / `STEP_FINISHED`，图的形状不变，Rust 侧 Kahn 排序与环检测零改动
- 每轮 `toolCallId` 形如 `tc_<nid>_r<轮次>_<序号>`，**不要复用**，否则 Rust 侧 `tool_map` 会串（用例：`react_multi_round_tool_calls_stay_isolated`）
- 回灌上下文必须包含 assistant 轮（带 `tool_calls`）与 `ToolMessage`，否则下一轮模型看不到自己刚要求过什么
- 决策者按 key 分派：有 `api_key` → `ChatOpenAI.bind_tools` + `astream`（`AIMessageChunk` 的 `+` 合并把分散的 `tool_call_chunks` 拼回完整 `tool_calls`）；无 key → `MockReActModel`（规则推断意图，**仅用于演示与自测**）
- 自测：`cd langgraph-bridge && uv run python tests/test_react.py`（58 项断言）

**F. 让 condition 节点按判据选边（S2）**

`condition` 节点 `data.expression` 填**自然语言判据**，候选菜单 = **该节点的出边**（连线顺序即候选编号，第 0 条是「命中」默认分支）：

| 出边数 | 行为 |
|---|---|
| ≥ 2 | 模型判定（结构化 `choose_branch` 工具 + `temperature=0`） |
| 1 | 直连，**不调模型**（不白花一次调用） |
| 0 | 该分支即终点，不连边 |

硬约束：

- **判定必须在节点函数里做完**（`_condition_node`，647 行），`router` 只从 state 读出 `branch` 返回 —— `add_conditional_edges` 的 path function **需同步执行**，而模型调用是异步的，直接塞进 router 会炸。放节点内还附带好处：判定进入执行轨迹，可观测
- 判定结果写进 `outputs` 条目的 `branch` 字段；`router` 取**最后一条**匹配记录（condition 位于环上时会被执行多次）
- **`output` 必须保持上游文本直通**，判定结果只走 `STEP_FINISHED` 的 `branch*` 扩展字段 —— 把判定文字塞进 output 会污染下游节点输入
- **零 key 的 `MockRouterModel` 只能喂「上游内容」**，不能喂完整 prompt —— prompt 里含判据本身，2-gram 关键词规则会自我命中（已踩，见坑表）
- 模型优先级 `data.model` > `cfg.router_model` > `cfg.model`；路由建议用便宜快的小模型
- `expression` 留空时退回旧中文词表（`是/yes/true/可以/正确`），**旧图不回归**
- 自测：`cd langgraph-bridge && uv run python tests/test_condition.py`（55 项断言）

**G. 让流程在人类审批处挂起（HITL）**

**一句话**：加一个 `human` 节点即可，但**只有走 bridge 才生效**。Rust 原生执行器不做审批——构图前若发现 `human` 节点，`workflow.rs` 会发 `warn` 并把整图委托给 bridge（`run_via_langgraph`）。

bridge 侧三件套（缺一不可）：

| 环节 | 位置 | 要点 |
|---|---|---|
| 挂起 | `server.py::_human_node` | 调 `interrupt(payload)`；**`interrupt()` 不抛异常**，`ainvoke` 正常返回、state 里带 `__interrupt__`，必须主动检查 |
| 持久化 | `server.py::get_checkpointer` | SQLite（`langgraph-checkpoint-sqlite`）优先，构造失败回退 `InMemorySaver` 并打日志；SQLite 才能**跨进程重启存活** |
| 续跑 | `server.py::_resume_graph` | `Command(resume=decision)` + **同一 `thread_id`**；先 `aget_state` 预检 `snap.next`，无挂起点给可读错误 |

硬约束：

- **`config` 必须带 `configurable.thread_id`**，否则 `interrupt` 直接报错；`_run_config(thread_id, limit)` 统一构造，别手搓
- `threadId` 由服务端生成（`run_id` 同源），经 `RUN_PAUSED` 事件回给前端并存在节点 `data.hitlThreadId` 上。**前端不自己造 threadId**
- **哨兵值 `_NO_RESUME = object()`** 区分「审核人明确提交 `null`」与「根本不是 resume 调用」。用 `None` 判据会把前者误判成后者（本项目第三次踩 falsy/哨兵类问题）
- **decision 缺失 → 422 报错，不得默认放行**。审批的默认答案永远不能是「通过」
- **resume 不检查 `published`**：一个跑到一半的 run 不该因为流程被取消发布而无法收尾
- `format_decision` 语义：`approve` → 返回 `value`（人改过的内容）或原 `inp`；`reject` → `"[已驳回] {note}"`；其他动作 → `"[{action}] {tail}"`
- 图级预检：`workflow.rs` 在 `node_map.is_empty()` 判断之后插入 human 检测。**`human` 节点必须走整图委托，不能直通**——直通 = 静默跳过审批 = 安全事故
- 前端 `nodeTypes` 稳定性：`running` 状态**不要**放进 `resumeApproval` 的依赖数组，否则 `nodeTypes` 重建 → 画布节点整体重挂载。用 `runningRef`
- 自测：`cd langgraph-bridge && uv run python tests/test_hitl.py`（50 项断言）。**测试末尾必须 `await server.close_checkpointer()`**，否则 aiosqlite 非 daemon 线程会让进程不退出（测试全绿但卡死）

---

## Documentation map

| 文件 | 读者 | 写什么 |
|---|---|---|
| `README.md` | 人（首次接触） | 是什么、怎么装、怎么跑、功能特性、项目结构 |
| `AGENTS.md`（本文） | AI agent | 改哪里、别改坏什么、契约表、坑、验证清单 |
| `langgraph-bridge/README.md` | 人 + agent（bridge 专题） | bridge 协议细节、端点、AG-UI 事件样例、验证状态 |
| `CHANGELOG.md` | 人 + agent | 版本历史（Keep a Changelog 格式；用户可见行为变更须在同一次提交里补条目） |
| `SECURITY.md` | 人 | 安全边界、已知缺口（无鉴权）、漏洞上报流程 |
| `LICENSE` | 人 | MIT |

改完功能后的文档同步顺序见「Project maintenance rules」。
