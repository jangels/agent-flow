# agent-flow ↔ LangGraph 桥接服务（路径 A 集成原型 · AG-UI 协议版 · 真实 MCP · ReAct 工具节点 · 模型分支路由 · 人工审批 HITL）

把上一轮讨论的「路径 A」落地成可跑原型：**agent-flow 的 Rust 引擎把整张工作流图委托给一个独立的 Python langgraph 服务执行，流式结果以 AG-UI 标准协议回传前端**。验证「自研 Rust 引擎 + 外挂 langgraph」能真正跑通，且后端是标准 AG-UI server。

> 当前版本 **0.7.0**。
> - **0.7.0（HITL）**：新增第 6 种节点 **`human`（人工审批）**——执行到该节点时图**挂起**，人给决策后从断点续跑。为此接入**持久化 checkpointer**（SQLite，跨进程重启存活）并引入 `interrupt()` / `Command(resume=...)`，新增 `POST /flows/{id}/resume` 与 `RUN_PAUSED` 事件。实测：挂起 → **杀掉服务进程** → 重启 → 用原 threadId 续跑成功。
> - **0.6.0（S2）**：`condition` 节点改为**模型决策的分支路由**（Routing pattern）——`data.expression` 是自然语言判据，候选菜单来自该节点的出边；判定在节点内完成、结果进 state，`router` 只查表。同时 `recursion_limit` **一律显式设置**，消灭内部默认值 10007 带来的「含环图挂死数小时」风险（实测含环流程 **1.7 秒**报错）。
> - **0.5.0（S1）**：`tool` 节点支持 **ReAct 模式**（`data.tools` 白名单 → 模型在节点内部自主循环决定调哪个工具、调几次、何时收工），循环封在节点内、图的形状不变，**Rust 引擎零改动**。

## 架构

```
agent-flow (Tauri + React + Rust)
  └─ workflow.rs 跑图，遇到 tool 节点 tool=="langgraph"
     │            （或图中含 human 节点 → 自动整图委托，见下）
       └─ bridge.rs: POST 整图 {nodes,edges,input,config} ──HTTP──▶ langgraph-bridge (FastAPI + LangGraph)
                                                                         └─ 翻译成 StateGraph 编译执行
                                                                         └─ SSE 推 AG-UI 标准事件
                                                                            (RUN_STARTED / STEP_STARTED / TEXT_MESSAGE_*
                                                                             / TOOL_CALL_* / STEP_FINISHED / RUN_PAUSED
                                                                             / RUN_FINISHED / RUN_ERROR)
       └─ bridge.rs: AG-UI → 内部 wf:event 薄适配层 ──emit("wf:event")──▶ React 前端（零改）
                                                                              └─ human 挂起 → 节点上长出审批按钮
                                                                              └─ 点按钮 → POST /flows/{id}/resume（同一链路）
```

协议边界：
- **后端（server.py）** 已是标准 **AG-UI server**：发出 `RUN_STARTED / STEP_STARTED / TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT / TEXT_MESSAGE_END / TOOL_CALL_START / TOOL_CALL_ARGS / TOOL_CALL_END / TOOL_CALL_RESULT / STEP_FINISHED / RUN_FINISHED / RUN_ERROR`，字段遵循 AG-UI 规范（`threadId` / `runId` / `stepName` / `messageId` / `delta` / `toolCallId` / `message`）。`STEP_FINISHED.output` 与 `RUN_FINISHED.result` 为应用扩展字段，供桥回填内部模型。**`RUN_PAUSED` 是扩展事件**——AG-UI 标准协议没有「暂停」语义（HITL 需要「本次 run 结束但流程未结束」这个第三态）。
- **Rust 侧（bridge.rs）** 做「AG-UI → 内部 wf:event」薄适配层：把标准事件翻译回 agent-flow 内部模型（`node:start` / `token` / `tool:start` / `tool:args` / `tool:result` / `node:done` / `node:awaiting` / `workflow:done`）再 `emit` 前端。维护 `messageId→nodeId` 映射把 `TEXT_MESSAGE_CONTENT.delta` 落到正确节点的 `token`，并维护 `toolCallId→(nodeId, 工具名)` 映射把工具调用细节透出到 Console（`TOOL_CALL_END` 无新增信息，被吸收）。
- **前端（React / Tauri 壳）零改动**：仍监听 `wf:event`，不感知 AG-UI。后端升级为 AG-UI 后，未来可直接对接 CopilotKit 等任意 AG-UI 前端，无需改服务。

## 运行

### 1. 起桥接服务（Python 侧，uv 管理）

```bash
cd agent-flow/langgraph-bridge
uv run uvicorn server:app --host 127.0.0.1 --port 8000
```

> ⚠️ **端口**：默认 8000。若本机已有服务占用（实测本机 `~/project/my/script/sg/dashboard/dashboard_server.py` 绑了 `*:8000`），换个端口起即可：`--port 8010`，同时把 `flows_server.py` 的 `LANGGRAPH_BRIDGE_URL` 指到同端口（`/run` 与裸 base 两种写法都支持）。

零 API key 即可跑：LLM 用 `MockChatModel`；**MCP 走本地真实 demo MCP server**（`mcp_servers/demo_server.py`，标准 stdio 协议，非 mock），默认已连，`tool` 节点声明 `data.mcp_tool` 即真实调用。
服务地址可用环境变量覆盖：`LANGGRAPH_BRIDGE_URL=http://127.0.0.1:8000/run`。

### 2. 在 agent-flow 里触发委托

在画布上加一个 `tool` 节点，属性 `tool` 设为 `langgraph`（或 `data.bridge=true`）。运行时 Rust 引擎跑完该节点的前驱后，把整张图 POST 给桥接服务，由 LangGraph 编译重跑并流式回流（AG-UI 事件 → 桥翻译 → 前端高亮）。

也可直接验证服务（不经过 Tauri GUI）：

```bash
curl -N -X POST http://127.0.0.1:8000/run -H 'Content-Type: application/json' -d '{
  "nodes":[
    {"id":"n1","type":"start","data":{"input":"北京天气怎么样？顺便算 12*(3+4)"}},
    {"id":"n2","type":"llm","data":{"prompt":"处理用户请求","system":"你是助手"}},
    {"id":"n3","type":"tool","data":{"tool":"langgraph"}},
    {"id":"n4","type":"end","data":{}}
  ],
  "edges":[{"source":"n1","target":"n2"},{"source":"n2","target":"n3"},{"source":"n3","target":"n4"}],
  "input":""
}'
```

### AG-UI 事件流示例（零 key 跑通实测）

```
data: {"type":"RUN_STARTED","threadId":"wf-0d1ef683","runId":"run-783fe7f7"}
data: {"type":"STEP_STARTED","stepName":"n_start","nodeType":"start"}
data: {"type":"STEP_FINISHED","stepName":"n_start","output":"北京天气怎么样？顺便算 12*(3+4)"}
data: {"type":"STEP_STARTED","stepName":"n_llm","nodeType":"llm"}
data: {"type":"TEXT_MESSAGE_START","messageId":"msg_n_llm","role":"assistant"}
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg_n_llm","delta":"我已查询天气服务..."}
data: {"type":"TEXT_MESSAGE_END","messageId":"msg_n_llm"}
data: {"type":"STEP_FINISHED","stepName":"n_llm","output":"我已查询天气服务..."}
data: {"type":"STEP_STARTED","stepName":"n_tool","nodeType":"tool"}
data: {"type":"TOOL_CALL_START","toolCallId":"tc_n_tool","toolCallName":"mock"}
data: {"type":"TOOL_CALL_ARGS","toolCallId":"tc_n_tool","delta":"..."}
data: {"type":"TOOL_CALL_RESULT","messageId":"msg_n_tool","toolCallId":"tc_n_tool","content":"get_weather(北京) = 晴 26°C","role":"tool"}
data: {"type":"STEP_FINISHED","stepName":"n_tool","output":"get_weather(北京) = 晴 26°C"}
data: {"type":"STEP_STARTED","stepName":"n_end","nodeType":"end"}
data: {"type":"STEP_FINISHED","stepName":"n_end","output":"get_weather(北京) = 晴 26°C"}
data: {"type":"RUN_FINISHED","runId":"run-783fe7f7","result":"get_weather(北京) = 晴 26°C"}
data: [DONE]
```

### 接真实 MCP（默认已连，tool 节点声明即可用）

桥接服务用 `langchain-mcp-adapters` 连接标准 MCP server，把工具加载为 LangGraph 可用工具。**默认连内置 `mcp_servers/demo_server.py`**（FastMCP 实现，stdio 传输，提供 `add` / `get_time` / `echo`，统一接受 `input:str`）。

在 agent-flow 的 `tool` 节点 `data` 里声明要调的 MCP 工具（命名规则 `{server}__{tool}`）：

```json
{"id":"t","type":"tool","data":{"mcp_tool":"demo__add"}}
```

运行时该节点会：
1. 用 `langchain-mcp-adapters` 调真实 MCP 工具的 `ainvoke({"input": <上游文本>})`；
2. 发完整 AG-UI `TOOL_CALL_START / TOOL_CALL_ARGS / TOOL_CALL_END / TOOL_CALL_RESULT` 事件（MCP 返回的 content-block 已规整为纯文本）；
3. 未命中 `mcp_tool` 时 fallback 到本地 mock 工具。

查看已加载工具：`curl http://127.0.0.1:8000/mcp/tools` → `{"tools":["demo__add","demo__echo","demo__get_time"]}`。

覆盖默认 MCP server（接你自己的，stdio / sse / streamable_http 均可），在 POST body 顶层传：

```json
"mcp_servers":[{"name":"x","transport":"stdio","command":"python","args":["/abs/path/your_server.py"]}]
```

以上是 **direct 模式**（单工具直调）。需要模型自己决定「调哪个、调几次」时，见下一节。

### tool 节点的两种模式：direct 直调 / ReAct 节点内循环

`data` 里**有没有 `tools` 白名单**决定走哪条路：

| 模式 | 触发条件 | 行为 | 图层面 |
|---|---|---|---|
| **direct** | 只填 `mcp_tool` | 拿到上游文本 → 调这一个工具 → 返回结果 | 一次动作 |
| **react** | `tools` 非空 | 模型在**节点内部**自主循环：决定调哪个工具、调几次、何时收工 | 仍是一次 `STEP_STARTED` / `STEP_FINISHED` |

```json
{"id":"n_react","type":"tool","data":{"tools":["demo__add","demo__get_time"],"max_iterations":5}}
```

- `tools` 支持全名（`demo__add`）与裸名（`add`）；`["*"]` 或 `["all"]` = 全部已加载工具
- `max_iterations` **显式**限定轮数（默认 6），替代隐式的递归上限
- 留空 `tools` 即回到 direct 模式，**旧配置完全兼容**
- 白名单没匹配到任何工具时退回 direct 并在服务端打一行日志，不静默失败

**关键设计：循环封在节点内部。** 对外只有一次 `STEP_STARTED` / `STEP_FINISHED`，图的形状不变 —— 所以 Rust 侧的 Kahn 拓扑排序与环检测对本循环完全无感，**零引擎改动**。

每轮工具调用发一对 `TOOL_CALL_START` / `TOOL_CALL_RESULT`，`toolCallId` 形如 `tc_<node>_r<轮次>_<序号>`（带轮次后缀，与 Rust 侧 `toolCallId → (节点, 工具名)` 映射一一对应，多轮不会串）。前端 Console 逐轮可见。

实测（零 key，多意图任务「现在几点？另外帮我算 88 加 12」）：

```
STEP_STARTED      stepName=n_react                    <- 图层面只出现一次
TOOL_CALL_START   tc_n_react_r0_0  toolCallName=add
TOOL_CALL_RESULT  tc_n_react_r0_0  content="88 + 12 = 100"
TOOL_CALL_START   tc_n_react_r1_0  toolCallName=get_time
TOOL_CALL_RESULT  tc_n_react_r1_0  content="2026-09-24T00:42:14"
STEP_FINISHED     stepName=n_react  output="已取得工具返回结果：88 + 12 = 100；2026-09-24T00:42:14"
```

即：**两个工具、两轮、图上一个节点。**

决策者是谁取决于有没有 key：零 key 时由 `MockReActModel` 按任务文本推断意图（逐个满足、已调过的不重复调、无意义即收工）；有 `api_key` 时换 `ChatOpenAI.bind_tools` + `astream`，由真实模型决定调用序列（`AIMessageChunk` 的 `+` 合并能把分散在多片的 `tool_call_chunks` 拼回完整 `tool_calls`，流式与工具调用兼得）。

自测：

```bash
cd langgraph-bridge && uv run python tests/test_react.py   # 58 项断言
```

### condition 节点：模型决策的分支路由（S2）

`condition` 节点按**判据**在它的**出边**中挑一条走。判据是自然语言，写在 `data.expression`；候选菜单就是该节点的出边。

```json
{"id":"n_cond","type":"condition","data":{"expression":"上游内容是否在询问天气"}}
```

出边顺序决定候选编号：第 0 条 = 「是/命中」分支，最后一条 = 「否/未命中」兜底。

| 情况 | 行为 |
|---|---|
| 出边 ≥ 2 条 | 模型判定选一条（结构化 `choose_branch` 工具，`temperature=0`） |
| 出边 = 1 条 | 直接连边，**不调模型**（不浪费一次调用） |
| 无有效出边 | 该分支即为终点，不连边 |
| `expression` 留空 | 退回旧的中文词表判据（`是/yes/true/可以/正确`），**旧配置完全兼容** |

**关键设计：判定在节点内部完成，`router` 只做纯查表。**

LangGraph 的 `add_conditional_edges` 需要**同步**的 path function，而模型调用是异步的。所以 server.py 让 `condition` 节点自己（异步地）完成判定，把选中的 target 写进 state 的 `outputs` 条目（`branch` 字段），`router` 只从 state 里读出来返回。附带好处：**判定进入执行轨迹**，可观测、可回放。

判定结果会随 `STEP_FINISHED` 的三个扩展字段透出（普通节点不带这些键）：

```
data: {"type":"STEP_FINISHED","stepName":"n_cond","output":"今天北京天气怎么样",
       "branch":"n_yes","branchLabel":"问天气","branchReason":"命中判据关键词"}
```

- `output` 仍是**上游文本原样直通** —— condition 只决定走哪条边，不改数据
- `branch` / `branchLabel` / `branchReason` 经 Rust 侧 `bridge.rs` 透传为 `node:done` 的扩展字段，前端在节点上显示「→ 分支名」，并在 Console 打一行日志

**用哪个模型**：优先级 `data.model` > `cfg.router_model` > 全局 `cfg.model`。**路由建议填便宜快的小模型**（Anthropic 对 Routing pattern 的明确建议），用大模型选边是杀鸡用牛刀。

零 key 时由 `MockRouterModel` 判定：把判据切成中文 **2-gram**（不引入分词依赖），任一组合命中上游内容即走第 0 个候选，否则走最后一个。例：判据「用户在询问天气」→ 2-gram 含「天气」→ 上游「今天北京天气怎么样」命中。

> ⚠️ **这条路径只在委托 langgraph bridge 时生效。** Rust 原生执行器是确定性 DAG：`condition` 只做直通，且会执行**全部**出边、不按判据选边。为避免「看起来配了其实没生效」的假接口，Rust 侧遇到出边 ≥ 2 的 `condition` 节点会主动发一条 `warn` 事件，前端 Console 打印提醒。

### human 节点：人工审批 / Human-in-the-Loop

第 6 种节点类型。执行到它时**整张图挂起**，人给决策后从断点续跑 —— 已执行过的节点不会重跑。

```
start → llm → [human 审批] → tool → end
                    ↑
          挂起（RUN_PAUSED），人看完再点「批准 / 驳回」
```

```json
{"id":"n_human","type":"human",
 "data":{"hitlPrompt":"是否批准本次 500 元报销？",
         "actions":["approve","reject"],
         "allow_edit":true}}
```

| 字段 | 说明 |
|---|---|
| `hitlPrompt` | 审批提示语（兼容旧字段名 `prompt`） |
| `actions` | 候选动作，默认 `["approve","reject"]`；可加 `escalate` 等自定义动作 |
| `allow_edit` | 是否允许审批时直接改内容，默认 `true` |

**挂起（第一次 run）**：`ainvoke` **不抛异常**，而是正常返回、state 里带 `__interrupt__`。服务据此发：

```
data: {"type":"STEP_STARTED","stepName":"n_human","nodeType":"human"}
data: {"type":"RUN_PAUSED","threadId":"flow-flow_hit-d6122e","runId":"run-45154413",
       "stepName":"n_human","prompt":"是否批准本次 500 元报销？",
       "input":"申请报销 500 元差旅费","actions":["approve","reject"],
       "allowEdit":true,"interruptId":"5c642b02..."}
data: {"type":"RUN_FINISHED","runId":"run-45154413","result":"申请报销 500 元差旅费","paused":true}
```

`RUN_FINISHED.paused = true` 是关键：**本次 run 结束了，但流程没结束**。前端据此提示「已暂停，等待人工审批」而不是「执行完成」。

**续跑（第二次请求）**：

```bash
curl -N -X POST http://127.0.0.1:8010/flows/flow_hitl_demo/resume \
     -H 'Content-Type: application/json' \
     -d '{"threadId":"flow-flow_hit-d6122e",
          "decision":{"decision":"approve","value":"已核对发票，批准 480 元"}}'
```

`decision` 的语义（`format_decision`）：

| decision | 往下游传什么 |
|---|---|
| `{"decision":"approve"}` | 上游内容**原样直通**（纯放行） |
| `{"decision":"approve","value":"改过的内容"}` | 人工改后的内容（**人不是只投票，还能改数据**） |
| `{"decision":"reject","note":"理由"}` | `[已驳回] 理由` |
| `{"decision":"escalate","value":"..."}` | `[escalate] ...`（自定义动作） |

> 驳回不阻断流程，而是把 `[已驳回] 理由` 交给下游 —— 后面接一个 `condition` 节点就能实现「批准走 A、驳回走 B」。这是 HITL 与分支路由的组合用法。

**持久化（checkpointer）** —— 这是 HITL 的地基：

| 项 | 值 |
|---|---|
| 默认位置 | `<data_dir>/checkpoints.db`（SQLite，WAL） |
| 环境变量覆盖 | `AGENT_FLOW_CHECKPOINT_DB` |
| 依赖 | `langgraph-checkpoint-sqlite`（未装则回退 `InMemorySaver`，启动日志会明确标注） |
| 为什么需要 | `interrupt()` 挂起后必须有个地方存「停在哪」，否则人无从续跑；且人可能**隔天才回来**，进程内存不成立 |

实测（`/flows/{id}/run` → kill 服务进程 → 重启 → 用旧 threadId resume）**续跑成功**，`steps: 2` —— 前面已完成的 `start` 没有重跑。

> ⚠️ **含 human 节点的流程必须走 bridge。** Rust 原生执行器是确定性 DAG，既不能中断也不能恢复；若在那里直通，等于**静默跳过审批**（安全事故而非功能缺失）。因此 `workflow.rs` 做图级预检：**图里只要有 human 节点就自动整图委托 bridge**，并先发一条 `warn` 说明。

自测：

```bash
cd langgraph-bridge && uv run python tests/test_hitl.py   # 50 项断言
```

### 执行上限：recursion_limit 一律显式设置

LangGraph 内部默认 `DEFAULT_RECURSION_LIMIT = int(env or 10007)`。对含环的图，这意味着「跑一万步才报错」——真实 LLM 下是**数小时挂死 + 按 token 计费**。

因此本服务**从不使用默认值**，优先级：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `wf.recursion_limit`（`/run` body 顶层字段 / `/flows/{id}/run` body） | 最高优先 |
| 2 | 环境变量 `AGENT_FLOW_RECURSION_LIMIT` | 服务级默认 |
| 3 | 按图规模推算 | 无环 `max(25, n*4)`；**有环收紧到 `max(12, n*2)`** |

构图前会做 DFS **环检测**：有环时在服务端打印环路径，并收紧上限。`RUN_STARTED` 事件带两个扩展字段，非流式返回体里也有：

```
data: {"type":"RUN_STARTED","threadId":"...","runId":"...","recursionLimit":25,"hasCycle":false}
```

撞上限时不再抛出裸的 LangGraph 异常，而是转成可读的 `RUN_ERROR`：

```
[超过执行步数上限 recursion_limit=12] 图很可能在循环且未收敛。
可给流程设置更大的 recursion_limit、或给循环补上明确的退出判据。原始错误：Recursion limit of 12 reached...
```

> 注意：**bridge 侧不拒绝有环图**——LangGraph 原生支持循环（`condition` 回边即合法循环，这是它相对静态 DAG 引擎的核心能力）。拒绝方应该是 Rust 原生执行器（它只做 Kahn 拓扑排序）。

自测：

```bash
cd langgraph-bridge && uv run python tests/test_condition.py   # 55 项断言
```

### 接真实 LLM

在 agent-flow 设置里填 `base_url` + `api_key` + `model`，Rust 通过 `config` 传给桥接服务；服务检测到 `api_key` 后，llm 节点改走 `ChatOpenAI(streaming=True)` 真实流式（仍发 `TEXT_MESSAGE_CONTENT`）。

### 已发布流程的 MCP 入口（把流程暴露给任意 MCP client）

`mcp_servers/flows_server.py` 是一个标准 **stdio MCP server**，把 agent-flow 中【已发布】的流程暴露为 MCP 工具：

| 工具 | 作用 |
|------|------|
| `list_published_flows` | 列出所有已发布流程（id / 名称 / 版本） |
| `run_flow(flow_id, input)` | 单独执行某个已发布流程，返回最终结果 |

- **流程来源**：agent-flow 的 SQLite 数据库 `agent_flow.db`（默认 `~/.agent-flow/agent_flow.db`），读取表 `flows` 中 `status='published'` 的行。数据目录可用 `AGENT_FLOW_HOME` 覆盖、`.db` 文件可用 `AGENT_FLOW_DB` 直接指定（两者解析规则与 Rust 侧 `lib.rs::resolve_data_dir` 完全一致）。本 server 使用**只读连接**（`mode=ro`），在 WAL 模式下与 app 的写入互不阻塞。
- **执行后端**：POST 到本桥接服务的 `POST /flows/{flow_id}/run`（`stream:false`，取回最终 result），默认 base `http://127.0.0.1:8000`，可用 `LANGGRAPH_BRIDGE_URL` 覆盖（给 `.../run` 或裸 base 都行，会被规整）。走这个端点而不是 `/run` 的好处：**bridge 会自动带上 app 里配好的 LLM 配置**（读 SQLite `kv['config']`），不会因为空 config 落到 Mock；同时 404/409 语义由 bridge 统一给出。

运行：

```bash
python mcp_servers/flows_server.py            # stdio 模式
```

在 MCP client 挂载（示例）：

```json
{
  "mcpServers": {
    "agent-flow": {
      "command": "python",
      "args": ["/abs/path/to/langgraph-bridge/mcp_servers/flows_server.py"],
      "env": {
        "AGENT_FLOW_HOME": "~/.agent-flow",
        "LANGGRAPH_BRIDGE_URL": "http://127.0.0.1:8000/run"
      }
    }
  }
}
```

即：**在 agent-flow 里点「发布」= 流程进入 MCP 可见集；外部要单独执行 = 调 `run_flow` 工具**。

### 按 flow_id 直接调用（HTTP，不经 MCP）

外部系统（CI / 业务服务 / curl）**不需要知道图长什么样**，传一个 `flow_id` 就能跑。bridge 自己去 SQLite 取图。

```bash
# 1. 看有哪些已发布流程
curl http://127.0.0.1:8000/flows

# 2. 传 id 直接执行 —— 流式（默认，与 /run 完全同格式的 AG-UI SSE）
curl -N -X POST http://127.0.0.1:8000/flows/<flow_id>/run \
     -H 'Content-Type: application/json' -d '{"input":"计算 3 加 5"}'

# 3. 传 id 直接执行 —— 非流式（脚本 / CI 友好，等跑完拿 JSON）
curl -X POST http://127.0.0.1:8000/flows/<flow_id>/run \
     -H 'Content-Type: application/json' \
     -d '{"input":"计算 3 加 5","stream":false}'

# 4. 流程若含 human 节点：run 会返回 paused:true + threadId，拿它去 resume
curl -X POST http://127.0.0.1:8000/flows/<flow_id>/resume \
     -H 'Content-Type: application/json' \
     -d '{"threadId":"<run 返回的 threadId>",
          "decision":{"decision":"approve","value":"人工改过的内容"}}'
```

| 端点 | 作用 |
|------|------|
| `GET /flows` | 列出已发布流程（`?all=true` 含草稿），返回 `id / name / version / status / nodes / edges` |
| `GET /flows/{flow_id}` | 单个流程元信息（先确认 id 是否拿对） |
| `POST /flows/{flow_id}/run` | 读 SQLite 取图直接执行；默认流式 SSE，`stream:false` 返回 JSON |
| `POST /flows/{flow_id}/resume` | **恢复一个等待人工审批的运行**（HITL 续跑）；`decision` 必填 |

`POST /flows/{flow_id}/run` 的 body（字段全部可选）：

| 字段 | 说明 |
|------|------|
| `input` | 传给 `start` 节点的输入文本 |
| `config` | **不传** → 自动读 SQLite `kv['config']`（app 里配好的 LLM）；传 `{}` → 显式强制零 key 模式（落 `MockChatModel`） |
| `mcp_servers` | 覆盖默认的 MCP server 列表（`[]` = 明确不要工具；`null`/不传 = 默认 demo server） |
| `recursion_limit` | 覆盖执行步数上限（不传则由 bridge 按图规模显式推算，见「执行上限」章） |
| `stream` | 默认 `true`（AG-UI SSE）；`false` → JSON，失败时 HTTP 500 + `error` |

非流式返回体：

```
{ok, flowId, flowName, version, threadId, runId, steps, result, paused,
 recursionLimit, hasCycle, usedStoredConfig}

# 流程含 human 节点且已挂起时，额外带：
  "paused": true,
  "awaiting": {nodeId, prompt, input, actions, allowEdit},
  "resumeUrl": "/flows/<flow_id>/resume"
```

`POST /flows/{flow_id}/resume` 的 body：

| 字段 | 说明 |
|------|------|
| `threadId` | **必填** —— 挂起时由 run 的返回 / `RUN_PAUSED` 事件带出 |
| `decision` | **必填** —— `{"decision":"approve","value":"...","note":"..."}` 或裸字符串 `"approve"` / `"reject"` |
| `config` / `mcp_servers` / `stream` | 同 `/run` |

两处刻意的设计：① `decision` 缺失时**报错而不是默认放行** —— 审批的默认答案永远不能是「通过」；② resume **不检查 `published`** —— 一个已经跑到一半的 run，不该因为中途取消了发布而无法收尾。

错误语义：流程不存在 → `404`；流程未发布 → `409`（仅 run）；图为空 → `422`；resume 缺 `threadId` / `decision` → `422`；thread 上没有挂起点 → HTTP 500 + 可读 `error`。

> **与 MCP 的关系**：`/flows/{id}/run` 是**执行入口**，MCP（`flows_server.py`）是**发现入口**——它内部就是调这个端点。所以「调用流程必须用 MCP 吗」→ **不必**，MCP 只是给 AI agent 用的可选一层。补充一点：MCP 那条路会把整条流读干、只留最终 result（**有损**：拿不到逐 token 与工具调用细节），要流式体验就直接消费 `/flows/{id}/run` 的 SSE。

## 验证状态

- ✅ **按 flow_id 的 HTTP 直调**（本机 8010 端口实测）：`GET /flows` 正确过滤 published（`?all=true` 含草稿）；`POST /flows/{id}/run` 流式返回完整 AG-UI 事件（`RUN_STARTED → STEP_STARTED → TOOL_CALL_* → TEXT_MESSAGE_* → STEP_FINISHED → RUN_FINISHED`），真实 MCP 工具结果 `3 + 5 = 8`；`stream:false` 返回 JSON；草稿 `409` / 不存在 `404` 均正确；`config` 覆盖语义（不传→读库、传 `{}`→Mock）正确。
- ✅ Python 服务：`uv run` 起 + curl 样例图，SSE 完整回流 AG-UI 标准事件（`RUN_STARTED → STEP_STARTED → TEXT_MESSAGE_* / TOOL_CALL_* → STEP_FINISHED → RUN_FINISHED`，零 key）。
- ✅ Rust 侧：`bridge.rs` 已加「AG-UI → wf:event」薄适配层（`translate_agui`），前端零改；`cargo check` 验证编译。
- ✅ 工具事件透出：`TOOL_CALL_*` 不再被吸收，翻译为 `tool:start` / `tool:args` / `tool:result` 并在 Console 展示；`cargo test bridge::` **4/4 通过**（覆盖工具全生命周期、token 节点归属、RUN_FINISHED/RUN_ERROR、未识别事件忽略）。
- ℹ️ 实测 `TOOL_CALL_START.toolCallName` 是 **LangChain 侧的裸工具名**（如 `add`），而非 `/mcp/tools` 列出的带前缀键名（`demo__add`）——两者由 `langchain-mcp-adapters` 的命名策略决定，前端展示用裸名即可。
- ⚠️ 完整 GUI 闭环（Rust→前端实时高亮）需在本机 `npm run tauri:dev` 跑桌面窗口验证（headless 环境无法跑 GUI）。
- ✅ 真实 MCP 接入：`uv run` 起服务，`/mcp/tools` 列出 `demo__add/demo__echo/demo__get_time`；POST 含 `data.mcp_tool:"demo__add"` 的图，SSE 中 `TOOL_CALL_RESULT` 实测为 `3 + 5 = 8`（真实调用 `demo_server.py`，非 mock）。
- ✅ SQLite 存储：流程/配置改存 `agent_flow.db`（rusqlite bundled + WAL）；Rust `cargo check` 通过；端到端实测「SQLite → flows_server → bridge → LangGraph」返回 `3 + 5 = 8`，草稿流程被正确拒绝，只读连接不阻塞写入。
- ✅ **ReAct 工具节点（S1）**：`data.tools` 白名单触发节点内循环。`uv run python tests/test_react.py` **58/58 通过**（白名单解析 / 事件序列 / 迭代上限 / 多轮唯一性 / 旧路径兼容 / 整图执行）；Rust 侧新增多轮隔离用例，`cargo test` **9/9 通过**；HTTP 实测「两个工具、两轮、图上一个节点」（`tc_n_react_r0_0` → `tc_n_react_r1_0`）。
- ✅ **模型分支路由（S2）**：`uv run python tests/test_condition.py` **55/55 通过**（候选菜单构造 / schema / Mock 判定规则 / 编号容错解析 / 环检测 / 上限解析 / 单候选不调模型 / 整图分支 / 含环撞限）。HTTP 实测：
  - `condition` 流式：`STEP_FINISHED stepName=n_cond output="今天北京天气怎么样" branch=n_yes branchLabel="问天气" branchReason="命中判据关键词"`，且 `n_no` **未被调度**；`RUN_STARTED` 带 `recursionLimit=25, hasCycle=false`
  - `/flows/{id}/run` 非流式（分支图）：`{ok:true, steps:4, recursionLimit:25, hasCycle:false}`
  - `/flows/{id}/run` 非流式（含环图）：**1.68 秒**返回 `{ok:false, steps:12, recursionLimit:12, hasCycle:true, error:"[超过执行步数上限 recursion_limit=12]..."}` —— 对比修复前会一路跑到 10007 步
- ✅ Rust 侧 `condition` 出边 ≥ 2 时主动发 `warn` 事件（前端 Console 提醒「Rust 原生执行器不按判据选边」），`workflow.rs` 的拓扑排序错误文案由「存在环或孤立节点」修正为「存在环」并给出改用 bridge 的指引。
- ✅ **人工审批 HITL（0.7.0）**：`uv run python tests/test_hitl.py` **50/50 通过**（决策语义 / 挂起字段完整 / 续跑不重跑已执行节点 / 人工编辑值 / 驳回标记 / 自定义动作 / 线程隔离 / 无挂起点续跑的可读报错 / 多审批节点串联 / checkpointer 落 SQLite）。HTTP 端到端实测（本机 8020，临时数据目录）：
  - `run` 非流式 → `{ok:true, paused:true, steps:1, threadId:"flow-flow_hit-d01823", awaiting:{nodeId:"n_human", prompt, input, actions, allowEdit}, resumeUrl}`
  - `resume`（approve + 人工改内容）→ `{ok:true, resumed:true, steps:2, result:"已核对发票，批准 480 元（核减 20 元）"}` —— **`steps:2` 证明 `n_start` 没有重跑**
  - `resume`（reject）→ `STEP_FINISHED n_human output="[已驳回] 发票缺失，请补充后重提"`，流程正常收尾
  - **跨进程恢复**：挂起后 `kill` 掉服务进程 → 重启 → 用原 threadId resume **成功**（SQLite checkpointer 的核心价值）
  - 参数校验：缺 `decision` / 缺 `threadId` 均 `422` + 可读文案；错误 threadId → 可读「没有等待中的挂起点」
  - Rust 侧 `cargo test` **13/13 通过**（新增 `RUN_PAUSED` → `node:awaiting` 字段完整 / 默认值补齐 / `paused` 标记透传 / `bridge_origin` 路径剥离）

## 已知限制 / 后续

- `condition` 节点：bridge 侧已是**模型决策**（S2）；**Rust 原生执行器侧仍是直通**（会执行全部出边）。两边语义差距已在 Inspector 提示 + Rust 侧 `warn` 事件里明确标出，不再静默。
  - 判据「假接口」问题已修：旧版 `data.expression` 写进去**没有任何引擎读它**（Rust 是 `input.clone()`，bridge 用硬编码中文词表），现在 bridge 真的把它当判据用。
  - 边界：`edge.label` 目前既当候选可读名、又当给模型看的语义标签，命名质量会影响路由准确率（用户不会意识到自己在写 prompt）；LangGraph 惯例是用节点名。
- **S2 未覆盖的强度**：只做了「选出边」这一档（业界对应 Routing）。池内动态派工（orchestrator-workers / supervisor，图不变但运行期选节点）与运行期造节点（真 autonomous agent，图会变）都未做——按 Anthropic 的分类，前两者仍是 **workflow** 而非 agent，且 90% 实际需求在「选出边 + ReAct」两档已够。判定结果会随图一起落 checkpoint（0.7.0 起接入 checkpointer），可观测。（**HITL 已作为正交能力单独落地**，见上。）
- **HITL 的边界（0.7.0 新增，属刻意不做而非遗漏）**：
  - 审批只在**单个 thread** 上挂起/恢复，**没有「待审批列表」端点**——跨会话找回挂起态需要调用方自己保存 `threadId`（前端目前靠运行期写进节点 data）。
  - **无审批历史 / 审计日志**：决策进入 `STEP_FINISHED.output` 与 Console，但不落专门的审计表。生产做审批合规时需要补。
  - **无超时 / 自动升级**：挂起可以无限期等待，没有「N 小时未处理则升级」这类策略。
  - checkpoint 是**本机 SQLite 文件**：不支持把挂起态导出/迁移到另一个服务实例。
  - 连续多级审批（human → human → human）已实测可用，但**每级挂起都是一次新的 HTTP 请求**，调用方要按序处理。
- **`human` 节点的跨引擎语义**：Rust 原生执行器不支持挂起/恢复，所以含 `human` 节点的图会被**自动整图委托** bridge（`workflow.rs` 图级预检 + `warn` 事件）。这是刻意为之——在那里直通等于**静默跳过审批**，是安全事故而非功能缺失。
- **候选顺序即优先级**：候选按连线顺序编号，模型判定失败时兜底到第 0 条，语义上相当于「默认命中」——设计图时把默认分支放第一条。
- `tool` 节点在桥接服务内已接真实 MCP（默认连 `demo` server）；未声明 `mcp_tool` 时 fallback 到本地 mock 工具。接更多 MCP server 在 POST body 顶层传 `mcp_servers` 即可。
- **ReAct 模式的边界**：① `MockReActModel` 是确定性规则（按关键词推断意图），不是真语义判断——零 key 只用于演示与自测，真实效果依赖 `api_key`；② 循环只发生在**单个节点内**，跨节点的迭代状态（累积器、上一轮结果）尚不可配；③ 无 checkpointer，循环中途中断无法恢复重跑（每轮状态仅在内存）；④ 中间轮次的 token 会先累加到节点 `output`，最终被 `STEP_FINISHED.output` 覆盖——这是预期行为，不是 bug。
- **Rust 侧遗留**：`bridge.rs` 的 `msg_to_node` 映射只增不减（节点结束后不清理），长循环会残留少量映射条目。当前 `max_iterations` 默认 6，影响可忽略，待后续统一处理。
- 当前 Rust 侧遇到 `tool(langgraph)` 会把整图再交给 LangGraph 跑一遍（含前驱节点），属演示性「接管」；生产应改为只把该节点之后的子图委托。
- 服务进程管理：原型手动起；生产可改 Tauri sidecar 内嵌 Python（代价是包体膨胀 + 跨平台打包）。
- **协议升级收益**：后端已是标准 AG-UI server，后续若要换前端框架（如直接接 CopilotKit React SDK），只需把 `bridge.rs` 的薄适配层替换为 AG-UI 客户端，Rust 引擎与画布无需改动。
