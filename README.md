# AgentFlow · Agent Orchestrator

基于 **Tauri v2 + React + Rust** 的 Agent 工作流编排桌面应用。在画布上拖拽「开始 / LLM 调用 / 条件分支 / 工具调用 / **人工审批** / 结束」节点，连线组成工作流，一键运行；LLM 节点通过 **OpenAI 兼容 API** 流式调用，token 实时回流到节点与日志面板。支持**多流程管理**：每个流程可独立编辑、发布、导出、单独执行；已发布流程可通过 **HTTP 按 `flow_id` 直调**，或经 **MCP** 暴露给任意 AI agent。**支持 Human-in-the-Loop**：流程跑到「人工审批」节点会**挂起**，人在节点上给出决策（通过 / 驳回 / 改内容）后从断点续跑，跨进程重启不丢。

> 本工程已通过 `tsc` 类型检查、`vite build` 生产构建，以及 `cargo test`（13/13 单测）、`langgraph-bridge` 的 `pytest`（HITL 50/50）与 `cargo build --release` Rust 编译验证。直接 `npm run tauri:dev` 即可在 macOS 上启动；`npm run tauri:build` 已验证可产出 `AgentFlow.app`。

### 📖 文档分工

| 文档 | 读者 | 内容 |
|---|---|---|
| **README.md**（本文） | 人（首次接触） | 是什么、怎么装、怎么跑、功能与结构 |
| **[AGENTS.md](./AGENTS.md)** | AI coding agent | 改哪里、硬约束、事件/数据契约、已知坑、验证清单、**未决风险** |
| **[langgraph-bridge/README.md](./langgraph-bridge/README.md)** | 人 + agent | LangGraph 桥接服务专题：AG-UI 协议、端点、MCP 接入、实测记录 |

### 构建产物与运行说明（重要）
- 本仓库在 **x86_64** 环境下 `tauri:build` 产出的 `AgentFlow.app` 为 **x86_64** 架构。若你的 Mac 是 **Apple Silicon（arm64）**，请在本机重新执行 `npm run tauri:build`，会自动编出 arm64 原生包（或跨平台需加 `--target aarch64-apple-darwin`）。
- 默认构建为 **未签名** 应用。首次运行需解除 quarantine 并放行 Gatekeeper：
  ```bash
  xattr -cr src-tauri/target/release/bundle/macos/AgentFlow.app
  open src-tauri/target/release/bundle/macos/AgentFlow.app
  ```
- `tauri:build` 的 **DMG 打包步骤** 依赖 `hdiutil`，在无显示器的 CI/沙箱中可能失败；这仅影响发行镜像（`.dmg`），**不影响 `.app` 本身**。本地有桌面环境的 Mac 上通常可正常生成 DMG。

---

## 功能特性

- 🎨 **可视化编排**：基于 `@xyflow/react` 的节点画布，拖拽、连线、缩放、小地图。
- 🗂 **多流程管理**：左侧流程列表，可新建 / 复制 / 删除 / 切换；每个流程是独立 DAG。
- 🚀 **单独发布 / 导出**：每个流程可独立发布（`Draft ↔ Published`，发布时版本自增），并可导出为 JSON。
- ▶ **单独执行**：按 `flow_id` 执行当前流程；所有运行事件带 `flowId` 归属，多流程互不干扰。
- 🤖 **LLM 节点**：调用任意 OpenAI 兼容端点（OpenAI / DeepSeek / 通义 / 自建网关），**流式输出**，支持节点级 `model` 覆盖全局模型。
- 🔀 **多节点类型**：开始、LLM 调用、条件分支、工具调用、**人工审批**、结束。
- 🧩 **LangGraph 外挂执行**：`tool` 节点可把整图委托给仓库内的 **langgraph-bridge**（FastAPI + LangGraph）执行，结果以 **AG-UI 标准协议**回流，Rust 侧薄适配层翻译后前端零改动。
- 🔌 **真实 MCP 接入**：bridge 通过 `langchain-mcp-adapters` 连标准 MCP server（默认内置 stdio demo server），`tool` 节点声明 `mcp_tool` 即真实调用。
- 🔁 **ReAct 工具循环**：`tool` 节点填 `tools` 白名单后，模型在**节点内部**自主循环——决定调哪个工具、调几次、何时收工（`max_iterations` 显式限轮）。循环封在节点内，**图的形状不变**，Rust 引擎零改动。
- ◆ **模型分支路由**：`condition` 节点填**自然语言判据**（`expression`），候选菜单就是它的出边，由模型选出走哪条（结构化输出 `choose_branch` + `temperature=0`）。判定在节点内完成、结果进 state 与执行轨迹，节点上直接回显「→ 分支名」。**委托 bridge 执行时生效**；Rust 原生执行器下会主动发 `warn` 提醒「不按判据选边、会走全部出边」。
- ✋ **人工审批（HITL）**：`human` 节点让流程**挂起等人**。执行到该节点时 LangGraph 调 `interrupt()` 存下断点，前端节点变紫并弹出审批控件（通过 / 驳回 + **可直接编辑要放行的内容**）；人提交后经 `POST /flows/{id}/resume` 用同一 `thread_id` **从断点续跑**，已执行节点不重跑。断点落 **SQLite checkpointer**，实测**杀掉服务进程重启后仍能用原 threadId 续跑**。
- ⏱ **执行上限显式化**：bridge 一律显式设置 `recursion_limit`（不再吃内部默认 10007），构图前做环检测，含环图自动收紧上限。实测含环流程 **1.7 秒**报错，而非挂死数小时。
- 🌐 **HTTP 按 id 直调**：`POST /flows/{flow_id}/run` —— 外部系统只需一个 `flow_id`，无需知道图结构。
- 🗨 **发布即分享聊天页**：点「发布」后自动生成链接 `http://localhost:1420/chat.html?flowId=<id>&bridgeUrl=<bridge>` —— 浏览器打开即是一个**独立聊天页**（聊天框 + 气泡），访客主动输入内容后页面以非流式 `POST /flows/{flow_id}/run` 拿结果回填。复用全部运行期能力（LLM / 工具 / 分支 / HITL），无额外后端状态；遇人工审批节点会显示「⏸ 等待人工审批」提示回编辑器处理。
- 🛰 **MCP 入口**：已发布流程经 stdio MCP server 暴露为工具（`list_published_flows` / `run_flow`），任意 MCP client 可触发。
- 💾 **持久化（SQLite）**：全部流程与配置存于 `~/.agent-flow/agent_flow.db`（rusqlite，**WAL 模式**）。数据目录**刻意不使用系统 app 数据目录**（macOS `~/Library/Application Support` 等），而是固定在用户主目录下的 `.agent-flow/`，一处可查、跨平台一致、可被 bridge / MCP server / `sqlite3` 直接读取。可用环境变量 `AGENT_FLOW_HOME` 覆盖整个目录。旧版 `flows.json` / `workflow.json` / `config.json` 在首次启动时**自动迁移**入 SQLite，原文件改名为 `*.migrated` 保留备查。
- ⚙ **API 配置**：Base URL / API Key / 模型 / 温度，持久化到 SQLite 的 `kv` 表。

---

## 环境要求（macOS）

- Rust 工具链：`rustup` / `cargo`（≥ 1.70）
- Node.js ≥ 18（推荐 20+）
- Xcode Command Line Tools：`xcode-select --install`
- macOS 10.15+（Tauri v2 要求）
- （可选，仅在用 bridge 时）Python ≥ 3.11 + [`uv`](https://docs.astral.sh/uv/)

---

## 快速开始

```bash
cd agent-flow
npm install            # 安装前端依赖 + Tauri CLI
npm run tauri:dev      # 启动开发模式（自动起 Vite + 编译 Rust + 打开窗口）
```

首次运行会自动打开 AgentFlow 窗口，并创建一个「默认流程」。点击右上角 **⚙ 设置** 填入你的 API Base URL 与 Key（默认 `https://api.openai.com/v1`，模型 `gpt-4o-mini`），然后：

1. 左侧 **流程** 面板管理流程（`+ 新建` / 复制 / 删除 / 点击切换）；
2. 从工具栏点击 **+ LLM 调用** 等按钮添加节点，拖动节点圆点连线；
3. 选中节点在右侧属性面板编辑 Prompt、System 等；
4. 点击 **▶ 运行** 执行当前流程，观察节点高亮与右下角日志（**保存** / **发布** / **导出** 在工具栏）。

打包成独立 `.app`：

```bash
npm run tauri:build   # 产物在 src-tauri/target/release/bundle/
```

### （可选）启动 LangGraph 桥接服务

```bash
cd langgraph-bridge
uv run uvicorn server:app --host 127.0.0.1 --port 8010   # 本机 8000 常被占用，建议换端口
curl -s localhost:8010/health
```

零 API key 即可跑通：LLM 走内置 `MockChatModel`，MCP 走本地真实 stdio demo server。详见 [langgraph-bridge/README.md](./langgraph-bridge/README.md)。

---

## 项目结构

```
agent-flow/
├── README.md                  # 本文
├── AGENTS.md                  # 面向 AI coding agent 的工作指引（硬约束 / 契约 / 坑）
├── index.html
├── package.json
├── vite.config.ts
├── tsconfig.json
├── src/                       # React 前端
│   ├── main.tsx
│   ├── App.tsx                # 画布 / 多流程生命周期 / 运行编排
│   ├── styles.css
│   ├── types.ts               # 节点 / 配置 / 流程（Flow）类型
│   ├── store.ts               # zustand 全局状态（配置 / 日志 / running）
│   ├── lib/tauri.ts           # Tauri invoke / 事件封装（含多流程 API）
│   └── components/
│       ├── FlowList.tsx       # 多流程列表（新建 / 复制 / 删除 / 发布）
│       ├── Toolbar.tsx        # 节点面板 + 运行 / 保存 / 发布 / 导出 / 设置
│       ├── nodes.tsx          # 6 类节点渲染（含 human 内联审批控件）
│       ├── Inspector.tsx      # 选中节点属性编辑
│       ├── Console.tsx        # 运行日志
│       └── Settings.tsx       # API 配置弹窗
├── src-tauri/                 # Rust 后端
│   ├── Cargo.toml
│   ├── build.rs
│   ├── tauri.conf.json
│   ├── capabilities/default.json
│   ├── icons/                 # 应用图标（已生成）
│   └── src/
│       ├── main.rs
│       ├── lib.rs             # Tauri 命令入口 + SQLite 存储层 + 多流程（Flow CRUD / 发布 / 执行 / 导出 / resume_flow）
│       ├── llm.rs             # OpenAI 兼容流式调用（SSE 解析）
│       ├── workflow.rs        # 拓扑排序 + 节点执行 + 事件发射（带 flowId，含环路拦截 / human 节点委托）
│       └── bridge.rs          # LangGraph 桥：AG-UI → wf:event 薄适配层（run / resume 共用）
└── langgraph-bridge/          # Python 子项目：LangGraph 外挂执行 + MCP 入口
    ├── server.py              # FastAPI：/run、/flows*、/flows/{id}/resume、/mcp/tools、/health；AG-UI server
    ├── flow_store.py          # Python 侧 SQLite 只读访问层（server 与 mcp_servers 共用）
    ├── tests/test_hitl.py     # HITL 覆盖：挂起 / 续跑 / 人工编辑 / thread 隔离 / 落盘（50 用例）
    ├── pyproject.toml
    ├── uv.lock
    ├── README.md              # bridge 专题文档
    └── mcp_servers/
        ├── flows_server.py    # stdio MCP：把【已发布】流程暴露为工具
        └── demo_server.py     # 内置 demo MCP server（add / get_time / echo）
```

---

## 工作原理

1. 前端把画布序列化写回对应流程，调用 Rust 命令 **`run_flow(flow_id)`** 单独执行该流程（旧接口 `run_workflow` 仍保留做兼容）。所有 `wf:event` 都会附 `flowId`，前端按流程归属过滤。
2. `workflow.rs` 用 **Kahn 算法**做拓扑排序（**有环直接报错退出**），按依赖顺序执行每个节点：
   - `start`：输出初始输入文本；
   - `llm`：调用 `llm.rs` 的流式接口，token 通过 `app.emit("wf:event", …)` 实时推回前端；
   - `condition`：Rust 侧为**直通**（会执行全部出边）；**委托 bridge 时**由模型按 `expression` 判据在出边中选一条，选中结果随事件回流并在节点上显示。出边 ≥ 2 时 Rust 侧会发一条 `warn`，避免误以为判据在这条路径上生效；
   - `tool`：若配置了 `data.tool = "langgraph"` / `data.bridge = true` / 非空 `data.tools`（ReAct 白名单）/ 非空 `data.mcp_tool`（direct 单工具），就把**整张图**委托给 `langgraph-bridge` 执行（AG-UI 协议回流）；否则为占位实现，亦可在 `workflow.rs` 直接 HTTP 调 MCP / 业务服务；
   - `human`：**Rust 原生执行器不做审批**（静默跳过审批等于安全事故）。构图前若发现图中含 `human` 节点，会发一条 `warn` 并把**整张图**委托给 bridge —— 由 LangGraph 在节点内部 `interrupt()` 挂起、等人决策后续跑；
   - `end`：汇聚上游输出作为最终结果。
3. 前端通过 `listen("wf:event")` 接收 `node:start` / `token` / `tool:start` / `tool:args` / `tool:result` / `node:awaiting` / `node:done` / `workflow:done` 事件，更新节点状态、流式填充输出、写入日志。工具调用细节会落在 Console，形如 `→ 调用工具 add` / `← add「计算 3 加 5」返回: 3 + 5 = 8`。
4. 委托 bridge 时协议边界是 **AG-UI 标准事件**：后端发出 `RUN_STARTED / STEP_STARTED / TEXT_MESSAGE_* / TOOL_CALL_* / STEP_FINISHED / RUN_FINISHED / RUN_ERROR`，以及 HITL 用的**自定义扩展** `RUN_PAUSED`；`bridge.rs` 的 `translate_agui()` 翻译成内部 `wf:event` 后 emit（`RUN_PAUSED` → `node:awaiting`）—— **前端不感知 AG-UI**，未来换任意 AG-UI 前端只需替换这一层适配器。
5. **暂停不是结束**：`RUN_PAUSED` 之后 bridge 会补发一条 `RUN_FINISHED` 且带 `paused: true`，表示「本次 run 结束，但流程没结束」——这个第三态是 HITL 的关键，缺少它前端无法区分「跑完了」与「在等人」。

完整的「事件 / 数据 / 端点」契约表见 [AGENTS.md § 4](./AGENTS.md)。

---

## 多流程 / 发布 / 导出 / MCP 入口

- **多流程模型**：一个 `Flow = { id, name, version, graph, status, updated_at }`，存于 SQLite 表 `flows`（`<data_dir>/agent_flow.db`，默认 `~/.agent-flow/agent_flow.db`）。Rust 命令：`list_flows` / `get_active_flow` / `create_flow` / `save_flow` / `delete_flow` / `set_active_flow`。
- **存储层**：SQLite（rusqlite，bundled — 无系统依赖）。表 `flows` 存流程、`kv` 存 `active_id` / API 配置 / 兼容旧接口的单流程 graph。CRUD 为**行级原子写**（UPSERT），不再全量重写文件；桌面 app 与 MCP server 是两个进程并发访问，靠 **WAL + `busy_timeout=5000`** 保证「读不阻塞写、写自动等锁」。
- **单独发布**：`publish_flow(id, published)` 翻转 `draft ↔ published`（发布时 `version` 自增）。发布是一个**状态**，代表该流程进入「可被独立触发」集合。
- **单独执行**：`run_flow(id)` 取出该流程的 graph，复用同一套引擎执行；执行事件带 `flowId`，天然支持多流程互不串联。
- **导出**：`export_flow(id)` 把流程以 JSON 落盘到 `<data_dir>/exports/<name>-<id>.json`（默认 `~/.agent-flow/exports/`）。
- **MCP 入口**：子项目 [`langgraph-bridge/mcp_servers/flows_server.py`](./langgraph-bridge/mcp_servers/flows_server.py) 把【已发布】流程暴露为标准 MCP 工具（`list_published_flows` / `run_flow`），任意 MCP client（Claude Desktop / 任意 MCP 网关 / 自研 agent）均可**单独触发**某个已发布流程。即：**发布 = 进入 MCP 可见集；单独执行 = 外部调 `run_flow`**。
- **HTTP 直调（不经 MCP）**：bridge 另提供 `GET /flows` 与 `POST /flows/{id}/run`——外部系统（CI / 业务服务 / curl）**只要传一个 `flow_id`** 就能执行该已发布流程，**无需知道图结构**：bridge 自己从 SQLite 取图，并在请求未带 `config` 时自动使用 app 里配好的 LLM 配置。默认返回流式 AG-UI SSE，`stream:false` 返回 JSON。也就是说 **MCP 不是执行流程的必要条件**，它只是给 AI agent 用的可选「发现入口」（且它内部同样调这个 HTTP 端点）。详见 [langgraph-bridge/README.md](./langgraph-bridge/README.md)。

---

## 数据与配置

### 数据目录

固定在 `~/.agent-flow/`（**刻意不用系统 app 数据目录**，便于外部工具直接读取）：

```
~/.agent-flow/
├── agent_flow.db        # SQLite：flows + kv（WAL 模式）
├── agent_flow.db-wal    # WAL 边车文件（运行时出现）
├── agent_flow.db-shm
├── checkpoints.db       # LangGraph checkpointer：HITL 的挂起点存档（bridge 侧写）
├── checkpoints.db-wal   # 同上边车（运行时出现）
├── checkpoints.db-shm
└── exports/             # export_flow 导出的流程 JSON
```

> `checkpoints.db` 与 `agent_flow.db` **是两回事**：前者是**运行期状态**（谁的图停在哪一步、state 快照），后者是**设计期数据**（图画什么、是否发布）。分开存的好处是「删流程」不会连累别人正在等待的审批；代价是孤儿断点需要另行清理。

> 旧版 JSON 存储（`flows.json` / `workflow.json` / `config.json`）位于**旧的系统配置目录**（macOS `~/Library/Application Support/com.agentflow.app/`）。首次启动读入 SQLite 后，这些文件在**原目录**被改名为 `*.migrated` 保留备查 —— 也就是说 `.migrated` 文件不在 `~/.agent-flow/` 下。若旧目录里已有 `agent_flow.db`，会被整体复制过来（含 `-wal` / `-shm` 边车文件）。

### SQLite 表

```sql
flows(id PK, name, version, graph /* JSON 文本 */, status /* draft|published */, updated_at)
kv(key PK, value)          -- active_id | config | legacy_workflow
```

> HITL 的挂起点**不在**这两张表里，而在同目录的 `checkpoints.db`（由 LangGraph 的 `AsyncSqliteSaver` 管理，表结构由 langgraph-checkpoint 定义）。

### 环境变量

| 变量 | 作用 | 默认 | 读取方 |
|---|---|---|---|
| `AGENT_FLOW_HOME` | 数据目录 | `~/.agent-flow` | Rust + Python |
| `AGENT_FLOW_DB` | 直接指定 `.db` 文件（支持 `~`） | `<data_dir>/agent_flow.db` | Python |
| `AGENT_FLOW_CHECKPOINT_DB` | HITL 断点库路径（**测试必改**，否则会往真实目录写） | `<data_dir>/checkpoints.db` | Python |
| `LANGGRAPH_BRIDGE_URL` | bridge 地址（`.../run` 或裸 base 均可） | `http://127.0.0.1:8000` | Rust + Python |

### 外部调用速查

```bash
# 1. 看有哪些已发布流程（?all=true 含草稿）
curl -s http://127.0.0.1:8010/flows

# 2. 传 flow_id 直接执行 —— 流式（AG-UI SSE）
curl -N -X POST http://127.0.0.1:8010/flows/<flow_id>/run \
     -H 'Content-Type: application/json' -d '{"input":"计算 3 加 5"}'

# 3. 传 flow_id 直接执行 —— 非流式（脚本 / CI 友好）
curl -s -X POST http://127.0.0.1:8010/flows/<flow_id>/run \
     -H 'Content-Type: application/json' -d '{"input":"计算 3 加 5","stream":false}'
# → {"ok":true,"flowId":"...","steps":4,"result":"...","usedStoredConfig":true}
# 若图里含 human 节点，会在这里停下并多返回：
# → {"ok":true,...,"paused":true,"awaiting":{"nodeId":"h1","prompt":"...","actions":["approve","reject"]},
#    "resumeUrl":"http://127.0.0.1:8010/flows/<flow_id>/resume"}

# 4. 【HITL】从断点续跑 —— 必须带挂起时返回的 threadId
curl -s -X POST http://127.0.0.1:8010/flows/<flow_id>/resume \
     -H 'Content-Type: application/json' \
     -d '{"threadId":"<挂起时返回的 threadId>","decision":{"decision":"approve"}}'
# 人工改内容后放行（value 会覆盖该节点向下游传递的文本）：
#   -d '{"threadId":"...","decision":{"decision":"approve","value":"发票已核验，金额 1280 元"}}'
# 驳回（可带理由）：
#   -d '{"threadId":"...","decision":{"decision":"reject","note":"发票缺失，请补充后重提"}}'

# 6. 【发布即分享】打开聊天页（浏览器访问，访客可主动输入内容运行流程）
open "http://localhost:1420/chat.html?flowId=<flow_id>&bridgeUrl=http%3A%2F%2F127.0.0.1%3A8010&name=<流程名>"
#   聊天页是前端 public/chat.html，由 Vite 静态资源服务提供；
#   页面从 URL 读取 flowId/bridgeUrl/name，以非流式 POST 到 bridge 的 /flows/{id}/run，
#   对 ok / paused(人工审批) / error 三态都有处理；无需额外后端状态。

# 5. 直接查库（不启动 app 也能看流程）
sqlite3 ~/.agent-flow/agent_flow.db "SELECT id,name,version,status FROM flows"
```

错误语义：流程不存在 `404` / 未发布 `409` / 图为空 `422` / 执行失败 `500`；resume 另加 **缺 `threadId` 或 `decision` → `422`**、**该 thread 上没有等待中的挂起点 → 可读错误**（不是静默重跑，也不是默认放行）。

---

## 自定义与扩展

> 面向 AI agent 的完整改造 recipe（新增节点类型要动哪 6 处、新增 Tauri 命令三件套等）见 **[AGENTS.md § 5](./AGENTS.md)**。

### 新增节点类型

1. `src/components/nodes.tsx`：新增渲染组件并加入 `nodeTypes` 映射；
2. `src/components/Toolbar.tsx` 的 `PALETTE` 增加一项；
3. `src/App.tsx` 的 `DEFAULT_DATA` 增加默认 `data`；
4. `src/types.ts` 的 `NodeType` 增加联合类型成员；
5. `src-tauri/src/workflow.rs` 的 `match ntype` 分支补充该类型的执行逻辑；
6. （若 bridge 也要支持）`langgraph-bridge/server.py` 的 `make_node` 内 `if/elif ntype` 链。

> 这 6 处是**必改**的；`human` 是个例外样本——它额外牵动 checkpointer、`RUN_PAUSED` 事件、resume 端点与前端等待态，因为它的本质是「执行期要停下」，而不只是「多画一个框」。

### 接入人工审批（HITL）

最小可用配置：往画布加一个 **人工审批** 节点，把上游连进来、下游连出去，然后**确保图能走 bridge**（任一 `tool` 节点配置非空 `tools` 白名单 / `mcp_tool` / `tool = "langgraph"`，或干脆按下面的「整图委托」方式跑）。节点属性面板可配三件事：

| 配置项 | `data` 键 | 说明 |
|---|---|---|
| 审批提示语 | `hitlPrompt` | 给审核人看的问句，如「本次报销金额 1280 元，是否放行？」 |
| 候选动作 | `actions` | 默认 `["approve","reject"]`；也可加 `edit` 等自定义动作 |
| 允许修改内容 | `allow_edit` | 默认 `true`；关掉则审核人只能原样通过 / 驳回 |

运行到该节点时：节点变紫、弹出审批控件（**可直接编辑要放行的文本**），Console 打印挂起信息。点「通过」→ 走 `resume_flow` 从断点续跑，**前面的节点不会重跑**；点「驳回」→ 向下游传递 `[已驳回] <理由>`。审核人手动改过的内容会成为 `value`，替代原输入传给下游。

### 接入真实工具 / MCP

`tool` 节点在 Rust 侧有两条路径：

- **路径 A（已落地，推荐先试）**：`tool` 节点配置非空 `data.tools`（ReAct 白名单）/ `data.mcp_tool`（direct 单工具）/ `data.tool = "langgraph"` / `data.bridge = true`，即把整张图 POST 给子项目 [`langgraph-bridge`](./langgraph-bridge/README.md)，由 LangGraph 编译执行。该服务**已接真实 MCP**——`tool` 节点 `data.mcp_tool`（如 `demo__add`）即触发对标准 MCP server（默认 `demo`，stdio）的真实调用，结果以 AG-UI 协议流式回流，Rust 薄适配层翻译为 `wf:event` 推前端，前端零改动。
- **路径 B（Rust 原生）**：在 `workflow.rs` 的 `tool` 分支直接 HTTP 调你的 MCP / 业务服务（任意标准 MCP server、内部网关或 REST 服务均可），将结果写入 `output` 并沿边传递。

走路径 A 时，`tool` 节点有**两种模式**，由 `data` 里有没有 `tools` 白名单决定：

| 模式 | 配置 | 行为 |
|---|---|---|
| `direct` | `data.mcp_tool = "demo__add"` | 单工具直调：上游文本 → 调这一个工具 → 返回 |
| **`react`** | `data.tools = ["demo__add","demo__get_time"]`（或 `["*"]`） | 模型在**节点内部**自主循环：决定调哪个、几次、何时收工 |

ReAct 模式要点：`max_iterations` 显式限轮（默认 6）；留空 `tools` 即回到 direct，**旧配置完全兼容**；循环封在节点内 → 对外只有一次 `STEP_STARTED` / `STEP_FINISHED`，**图的形状不变**，Rust 侧拓扑排序与环检测零改动（`cargo test` 13/13 通过，含新增的暂停事件映射与 URL 归一化用例）。

**`condition` 节点的分支路由**（走路径 A 时生效）：`data.expression` 填自然语言判据（如「上游内容是否在询问天气」），模型在出边中选一条；出边顺序即候选编号，第 0 条为「命中」默认分支。用便宜快的小模型即可（`data.model` / `cfg.router_model` 覆盖）。详见 [`langgraph-bridge/README.md`](./langgraph-bridge/README.md)。

### Release 构建的权限说明

本工程 `capabilities/default.json` 仅含 `core:default`。**Debug（`tauri:dev`）下自定义命令自动放开**，可直接运行。若要做 **Release（`tauri:build`）**，需在 `src-tauri/permissions/` 下定义显式权限集（授予 `allow-run-flow` 等），再在 capability 中引用，否则自定义命令会被 ACL 拦截。

---

## 已知占位 / 后续路线

**🔴 高优先级（安全性）**

- ~~**环路保护的路径不对称**~~ → **已在 S2 修复**。bridge 构图前做 DFS 环检测（服务端日志 + `RUN_STARTED.hasCycle`），并**一律显式**设置 `recursion_limit`（优先级 `wf.recursion_limit` > `AGENT_FLOW_RECURSION_LIMIT` 环境变量 > 按图规模推算，有环时收紧到 `max(12, n*2)`），超限转成可读的 `RUN_ERROR`。实测含环流程 **1.68 秒**返回错误，而非一路跑到 10007 步（旧行为外推：真实 LLM 数小时挂死 + 等额计费）。
  同时修正 `workflow.rs` 中「存在环或孤立节点」的错误文案（Kahn 下失败的只可能是环——孤立节点 in-degree 为 0，照样会被排出）。
  **遗留**：前端 `onConnect` 仍无环校验（画环本身是合法需求，真正该拒绝的是 Rust 原生执行器，它已拦截）。详见 [AGENTS.md § 7](./AGENTS.md)。
- **正式环境化**：三处 `127.0.0.1` 落差（bridge 地址、MCP 挂载、鉴权）——当前所有调用入口均无鉴权，仅适合本机。**HITL 让这条从「不方便」升级为「危险」**：`/flows/{id}/resume` 没有审批人身份校验，任何能访问端口的人都能替你「通过」一次审批。上生产前必须先补鉴权。

**🟡 功能缺口**

- `condition` 的**跨引擎语义对齐**：bridge 侧已是模型判定（S2），Rust 原生执行器侧仍是直通 + `warn`。若要让 Rust 侧也支持，需在同步执行器里引入模型调用，或把判定拆成独立节点；
- **Loop Engineering 四件套仍不齐**：有「模型选边」（`condition`）与「节点内 ReAct 循环」（`tool`），但缺**迭代计数器**、**可配的继续/退出判据**（无法表达「迭代到满意为止」）、**跨节点迭代状态**（累积器 / 上一轮结果）；
- **HITL 的边界（已实现，但仍是 1.0 版）**：
  - 审批**只能在节点上做**（`human` 节点），还不能在「任意节点执行前/后」插钩子；
  - 挂起态**无 TTL、无超时自动决策、无待办列表** —— 挂起的 run 只能靠人回来手动续，服务重启后**需要旧 `threadId`** 才能找回（前端节点上会存着，但换个客户端就断了）；
  - **无审批人身份与鉴权**：谁都能调 `/resume`，谁都能点「通过」。生产必须补 `Authorization` + 审批人白名单（这是当前最大的安全缺口）；
  - 一个 thread **同时只能有一个挂起点**（串联多审批可以，并行分支同时等人不行）；
  - `human` 节点在 **Rust 原生执行器下不生效**（会被整图委托给 bridge，见上）；
- `tool` 节点 Rust 原生接 MCP 的便捷封装（当前 MCP 接入由 `langgraph-bridge` 子项目承担）；
- ReAct 循环的**跨节点迭代状态**（累积器 / 上一轮结果）尚不可配；循环内部的中间态随 checkpointer 落盘，但**尚无清理策略**（`checkpoints.db` 会随运行次数增长）；
- bridge 侧 `tool(langgraph)` 会把整图（含前驱节点）再跑一遍，属演示性「接管」；生产应改为只委托该节点之后的子图；
- 流程**导入**（从导出的 JSON 反向导入）、节点参数校验；
- 流程重命名 UI、流程分组 / 搜索；
- 多 LLM 供应商切换与失败重试；
- bridge 进程管理：原型手动起，生产可改 Tauri sidecar 内嵌 Python（代价是包体膨胀 + 跨平台打包）。

## Docs

| File | What it is |
|---|---|
| [README.md](./README.md) | 你在这里 —— 功能总览 + 快速开始 + 运行语义 |
| [AGENTS.md](./AGENTS.md) | **AI coding agent 的权威工作指引**（硬约束、文件职责表、改造 recipe、验证清单） |
| [CHANGELOG.md](./CHANGELOG.md) | 版本日志（Keep a Changelog 格式） |
| [SECURITY.md](./SECURITY.md) | 安全模型现状（无鉴权边界）+ 漏洞私报流程 |
| [langgraph-bridge/README.md](./langgraph-bridge/README.md) | bridge 子项目专题：HITL / MCP / AG-UI 协议细节 |

## License

[MIT](./LICENSE) © agent-flow contributors
