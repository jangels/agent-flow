"""agent-flow ↔ LangGraph 桥接服务（路径 A 集成原型 · AG-UI 协议版 · 已接真实 MCP）。

agent-flow 的 Rust 引擎把整张工作流图 {nodes, edges} + 用户输入 POST 给本服务，
本服务把它翻译成 LangGraph StateGraph 编译执行，并通过 SSE 把进度以
【AG-UI 标准协议】事件流推回：

    RUN_STARTED / STEP_STARTED / TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT
    TEXT_MESSAGE_END / TOOL_CALL_START / TOOL_CALL_ARGS / TOOL_CALL_END
    TOOL_CALL_RESULT / STEP_FINISHED / RUN_FINISHED / RUN_ERROR

Rust 侧（bridge.rs）把上述 AG-UI 事件翻译回 agent-flow 内部 wf:event 模型再 emit
前端（node:start / token / node:done / workflow:done），因此前端 React / Tauri 壳
零改动。后端已是标准 AG-UI server，未来可直接对接 CopilotKit 等任意 AG-UI 前端。

真实 MCP 接入：
    本服务用 langchain-mcp-adapters 连接标准 MCP server（默认连内置 demo server，
    走 stdio JSON-RPC 协议，非 mock）。当 agent-flow 的 tool 节点 data.mcp_tool
    命中已加载工具时，调用真实 MCP 工具（ainvoke）并回流 AG-UI TOOL_CALL_* 事件；
    未命中则 fallback 到本地 mock。可在 POST body 顶层用 mcp_servers 覆盖默认配置：

        "mcp_servers": [{"name": "x", "transport": "stdio",
                         "command": "python", "args": ["/abs/path/server.py"]}]

tool 节点两种模式（data.tools 非空即进入 ReAct 模式）：

  1) direct（兼容旧行为）   data.mcp_tool = "demo__add"
     单工具直调：拿到上游文本 -> 调这一个工具 -> 返回结果。图上的一次「动作」。

  2) react（S1 · 节点内循环）data.tools = ["demo__add", "demo__get_time"] 或 ["*"]
     给出工具白名单后，模型在【节点内部】自主循环：决定调哪个工具、调几次、何时收工。
     循环封在一个节点内（对外仍是一次 STEP_STARTED / STEP_FINISHED），因此 Rust 侧
     Kahn 拓扑排序看不到环，无需任何引擎改动。
     - data.max_iterations 显式限定轮数（默认 6），替代隐式的 10007 递归上限
     - 每轮工具调用发一对 AG-UI TOOL_CALL_START / TOOL_CALL_RESULT（toolCallId 带
       轮次后缀，与 Rust 侧 tool_map 一一对应），前端 Console 逐轮可见
     - 零 key 时由 MockReActModel 做确定性决策（首轮选工具、拿到结果后收工），
       循环结构 / 事件流 / 真实 MCP 调用全部真实

condition 节点：模型决策的分支路由（Routing pattern）

    判据来自 data.expression（自然语言），**候选菜单来自该节点的出边**（每条出边一个候选，
    可读名优先取 edge.label，其次 sourceHandle，再其次目标节点名 / id）。判定在【节点内部】
    完成并把选中结果写进 state，conditional edge 的 router 只做纯查表 —— 因为 LangGraph 的
    path function 需同步执行，把模型调用放节点内才能既异步又可观测（判定可进执行轨迹）。

    - 模型通过 choose_branch 工具返回候选编号（结构化输出，不是自由文本），temperature=0
    - 用便宜快的小模型即可：data.model 覆盖 > cfg.router_model > cfg.model
    - data.expression 留空 -> 退回旧的中文词表判据（是/yes/true/可以/正确），保持向后兼容
    - 只有一个出边 -> 直连，不调模型；零出边 -> 该分支即为终点
    - 零 key 时由 MockRouterModel 判定（中文 2-gram 命中判据 -> 第 0 个候选，否则最后一个候选）

human 节点：人工审批 / Human-in-the-Loop（HITL）

    第 6 种节点类型。执行体调用 LangGraph 原生 `interrupt(payload)` **把图挂起**：

        start → llm → [human 审批] → tool → end
                            ↑
                 挂起，等人给决策，再从断点续跑（不重跑前面的节点）

    挂起时 `ainvoke` **不抛异常**，而是正常返回、state 里带 `__interrupt__`；
    checkpoint 落盘后线程可跨进程存活，人隔天回来也能续。
    本服务据此发 AG-UI 扩展事件 **RUN_PAUSED**（含 threadId / prompt / input /
    actions / allowEdit），随后 RUN_FINISHED 带 `paused: true` 收尾本次 run。

    续跑：POST /flows/{flow_id}/resume  body: {threadId, decision, ...}
        用 `Command(resume=<decision>)` + **同一 thread_id** 重入，图从断点继续。
        decision 形如 {"decision":"approve","value":"人工改过的内容","note":"理由"}：
          - approve → 输出 = value（留空则沿用上游输入），人工可直接改内容
          - reject  → 输出 = "[已驳回] <note>"，下游 condition 可据此分流

    持久化（checkpointer）：默认 SQLite `<data_dir>/checkpoints.db`
        （`AGENT_FLOW_CHECKPOINT_DB` 可覆盖）。装不上 langgraph-checkpoint-sqlite 时
        回退 InMemorySaver —— 功能一致，但**进程重启丢挂起态**，启动日志会明确标注。

    ⚠️ human 节点依赖 checkpointer，checkpointer 依赖 thread_id：二者缺一不可。
    这也是「控制流由运行期决定 → 状态持久化从可选变必需」的直接推论。

执行上限（recursion_limit）

    本服务**一律显式**设置 LangGraph 的 recursion_limit，不再吃内部默认值 10007
    （`langgraph/_internal/_config.py`：`DEFAULT_RECURSION_LIMIT = int(env or 10007)`）。
    10007 步对含环的图意味着「跑一万步才报错」——真实 LLM 下是数小时挂死 + 按 token 计费。
    优先级：wf.recursion_limit > 环境变量 AGENT_FLOW_RECURSION_LIMIT > 按图规模推算
    （无环 max(25, n*4)；有环收紧到 max(12, n*2)）。构图前会做环检测并打印日志，
    RUN_STARTED 事件带 recursionLimit / hasCycle 两个扩展字段，便于脚本消费。

零 API key 也能跑：LLM 用 MockChatModel，MCP 走本地 demo server。
config 提供 base_url + api_key + model 时，llm 节点改走真实 OpenAI 兼容流式。

【两种调用方式】

1) 整图调用（agent-flow app 内部走这条，bridge.rs 就是它的客户端）
    POST /run   body: {nodes, edges, input, config, mcp_servers}

2) 按 flow_id 调用（外部 HTTP 直接调，不必知道图长什么样）
    GET  /flows                      列出已发布流程
    GET  /flows/{flow_id}            单个流程元信息
    POST /flows/{flow_id}/run        读 SQLite 里的图直接执行（默认流式 AG-UI SSE）
                                     body: {input, config?, mcp_servers?, stream?}
                                     - 不传 config  -> 自动读 SQLite kv['config']（app 里配的 LLM）
                                     - config: {}   -> 显式空配置，走零 key 的 MockChatModel
                                     - stream:false -> 等跑完返回 JSON {ok, result} 便于脚本消费

运行：
    uv run uvicorn server:app --host 127.0.0.1 --port 8000 --reload

列出已加载的真实 MCP 工具：
    curl http://127.0.0.1:8000/mcp/tools

按 flow_id 直接跑一条已发布流程（流式）：
    curl -N -X POST http://127.0.0.1:8000/flows/<flow_id>/run \
         -H 'Content-Type: application/json' -d '{"input":"计算 3 加 5"}'
"""

from __future__ import annotations

import asyncio
import json
import operator
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, TypedDict

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

import flow_store


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """应用关闭时归还 checkpointer 的 SQLite 连接（避免 WAL 锁残留）。"""
    yield
    await close_checkpointer()


app = FastAPI(
    title="agent-flow langgraph bridge (AG-UI + MCP + flow_id HTTP + ReAct + Routing + HITL)",
    version="0.7.0",
    lifespan=_lifespan,
)

# CORS：聊天页（http://localhost:1420 / chat.html）在浏览器里直接 fetch 本服务，
# 跨源请求会先发 OPTIONS preflight —— 没有这个中间件就会 「blocked by CORS policy」。
# 来源收敛到本机前端 dev server，不放开通配（bridge 无鉴权，见已知限制）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

# 「本次不是续跑」的哨兵：用来区分「审核人明确提交 null」和「根本不是 resume 调用」。
# 用 None 做判据会把前者误判成后者。
_NO_RESUME: Any = object()


# --------------------------------------------------------------------------
# 零 key 的「假 LLM」与 mock 工具（MCP 未命中时的 fallback）
# --------------------------------------------------------------------------


class MockChatModel:
    """确定性假 LLM：根据用户输入规则生成文本，不调任何外部 API。"""

    def invoke(self, messages) -> AIMessage:
        user_text = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                user_text = str(m.content)
                break
        if re.search(r"\d", user_text) and any(op in user_text for op in "+-*/"):
            return AIMessage(content="我已计算该表达式，并给出算式的求值结果。")
        if any(c in user_text for c in ["北京", "上海", "深圳", "杭州", "天气"]):
            return AIMessage(content="我已查询天气服务，下面给出该城市的实时天气。")
        return AIMessage(content="我已综合上游信息，给出本次任务的最终答复。")


def _mock_tool(inp: str) -> str:
    """根据输入文本选择 mock 工具（calculator / weather / search）。"""
    if re.search(r"\d", inp) and any(op in inp for op in "+-*/"):
        expr = re.sub(r"[^0-9+\-*/(). ]", "", inp)
        try:
            # eval 环境显式清空 __builtins__；引号用单引号避免 f-string 复用双引号（py3.11 不允许）
            safe_globals = {"__builtins__": {}}
            return f"calculator({expr}) = {eval(expr, safe_globals, {})}"
        except Exception as e:  # noqa: BLE001
            return f"calculator error: {e}"
    for city in ["北京", "上海", "深圳", "杭州"]:
        if city in inp:
            data = {
                "北京": "晴 26°C",
                "上海": "多云 30°C",
                "深圳": "雷阵雨 28°C",
                "杭州": "阴 24°C",
            }
            return f"get_weather({city}) = {data[city]}"
    return f"search('{inp[:20]}') = [mock] 找到 3 条相关条目: A/B/C"


def _tokenize(text: str) -> list[str]:
    """把整段文本切成流式分片：中文逐字、英文数字按词（零 key 演示用）。"""
    return [
        t for t in re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|\s+|[^\s]", text) if t
    ]


class MockReActModel:
    """零 key 的确定性 ReAct 决策器：首轮选工具，拿到结果后收工。

    真实场景下这两步都由 LLM 完成（bind_tools -> tool_calls -> ToolMessage 回灌）。
    零 key 时用规则模拟「语义判断」，使循环结构、AG-UI 事件流、真实 MCP 调用
    全部保持真实，只有「决策」这一步被替换成确定性规则。
    """

    def __init__(self, tools: list):
        self.tools = list(tools)

    def bind_tools(self, tools, **kwargs):
        """与 ChatOpenAI.bind_tools 保持一致的链式用法。"""
        return MockReActModel(list(tools))

    def _tool_names(self) -> list[str]:
        return [t.name for t in self.tools]

    # 计算类意图：必须有数字，且出现运算符或中文运算词（"加"没有 ASCII 符号）
    CALC_WORDS = [
        "+",
        "-",
        "*",
        "/",
        "×",
        "÷",
        "加",
        "减",
        "乘",
        "除",
        "等于",
        "求和",
        "相加",
    ]
    CITIES = ["北京", "上海", "深圳", "杭州"]

    def _wants(self, task: str) -> list[str]:
        """推断任务需要哪几类工具（可能多个 -> 用来演示多轮循环）。"""
        wants: list[str] = []
        if any(c in task for c in ["天气", "气温"]) or any(
            c in task for c in self.CITIES
        ):
            wants.append("weather")
        if re.search(r"\d", task) and any(w in task for w in self.CALC_WORDS):
            wants.append("add")
        if any(c in task for c in ["时间", "几点", "日期"]) or re.search(
            r"\btime\b", task, re.I
        ):
            wants.append("time")
        return wants

    def _args_for(self, tool, task: str) -> dict:
        """按工具的 args_schema 猜参数名（demo server 统一是 input）。"""
        schema = getattr(tool, "args_schema", None)
        fields = list(getattr(schema, "model_fields", {}) or {})
        if fields:
            return {fields[0]: task}
        return {"input": task}

    def invoke(self, messages) -> AIMessage:
        results = [m for m in messages if isinstance(m, ToolMessage)]
        done = {getattr(m, "name", None) for m in results}
        done.discard(None)

        task = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                task = str(m.content)
                break

        names = self._tool_names()
        if not names:
            return AIMessage(content="当前没有可用工具，无法执行。")

        def prefer(*keys: str) -> str | None:
            for k in keys:
                for n in names:
                    if k in n:
                        return n
            return None

        def call(tool_name: str) -> AIMessage:
            tool = next((t for t in self.tools if t.name == tool_name), None)
            args = self._args_for(tool, task) if tool is not None else {"input": task}
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": args,
                        "id": f"call_mock_{len(done) + 1}",
                        "type": "tool_call",
                    }
                ],
            )

        # 逐个满足识别出的意图；已调过的工具不重复调 -> 多意图任务天然跑成多轮
        wants = self._wants(task)
        for want in wants:
            hit = prefer(want)
            if hit and hit not in done:
                return call(hit)

        # 没识别出明确意图时，用 echo/search 兜底一次（不做无意义的重复调用）
        if not wants:
            hit = prefer("echo", "search", "fetch") or names[0]
            if hit and hit not in done:
                return call(hit)

        summary = "；".join(str(m.content) for m in results)
        return AIMessage(
            content=f"已取得工具返回结果：{summary}" if summary else "任务已完成。"
        )

    async def ainvoke(self, messages) -> AIMessage:
        return self.invoke(messages)


class MockRouterModel:
    """零 key 的确定性分支判定器（对应真实 LLM 的 choose_branch 工具调用）。

    判定规则（确定性、可解释，仅用于零 key 演示与自测）：
      - 判据 expression 里的词命中上游内容 -> 选第 0 个候选（连线顺序里的「是」分支）
      - 未命中 -> 选最后一个候选（「否」分支）
      - expression 留空 -> 退回旧词表（是/yes/true/可以/正确 出现在内容里）

    中文不引入分词依赖：把判据切成 **2-gram**（任意相邻两字组合），任一组合出现在
    上游内容里即算命中。例：判据「用户是否在问天气」-> 2-gram 含「天气」，
    上游「今天北京天气怎么样」命中 -> 走第 0 个候选。
    """

    POSITIVE_WORDS = ["是", "yes", "true", "可以", "正确"]
    # 高频虚词组合，2-gram 命中它们不算语义命中（否则「是否」「用户」会误判）
    WEAK_GRAMS = {
        "是否",
        "用户",
        "判断",
        "内容",
        "如果",
        "那么",
        "否则",
        "这个",
        "文本",
        "输入",
        "上游",
        "问题",
        "询问",
        "属于",
        "以及",
        "并且",
        "或者",
        "用于",
    }

    def __init__(self, n: int, expression: str = ""):
        self.n = max(1, int(n))
        self.expression = (expression or "").strip()

    def bind_tools(self, tools, **kwargs):
        """与 ChatOpenAI.bind_tools 保持一致的链式用法（本判定器只有一个工具）。"""
        return self

    def _hit(self, task: str) -> bool:
        exp = self.expression
        if not exp:
            return any(w in task for w in self.POSITIVE_WORDS)
        # 英文/数字词：整词包含
        for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", exp):
            if w.lower() in task.lower():
                return True
        # 中文：连续汉字段切 2-gram，跳过虚词组合
        for seg in re.split(r"[^\u4e00-\u9fff]+", exp):
            for i in range(len(seg) - 1):
                gram = seg[i : i + 2]
                if gram in self.WEAK_GRAMS:
                    continue
                if gram in task:
                    return True
        return False

    def invoke(self, messages) -> AIMessage:
        task = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                task = str(m.content)
                break
        hit = self._hit(task)
        idx = 0 if hit else self.n - 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "choose_branch",
                    "args": {
                        "index": idx,
                        "reason": "命中判据关键词" if hit else "未命中判据关键词",
                    },
                    "id": "call_route_1",
                    "type": "tool_call",
                }
            ],
        )

    async def ainvoke(self, messages) -> AIMessage:
        return self.invoke(messages)


# --------------------------------------------------------------------------
# 真实 MCP：用 langchain-mcp-adapters 连接标准 MCP server，加载工具
# --------------------------------------------------------------------------


def _demo_mcp_servers() -> list[dict]:
    """默认连内置 demo MCP server（stdio，符合 MCP 协议，非 mock）。"""
    demo_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mcp_servers", "demo_server.py"
    )
    return [
        {
            "name": "demo",
            "transport": "stdio",
            "command": sys.executable,
            "args": [demo_path],
            "encoding": "utf-8",
        }
    ]


async def _load_mcp_tools(mcp_servers: list[dict] | None) -> dict[str, Any]:
    """连接所有声明的 MCP server，返回 {f'{server}__{tool.name}': BaseTool} 映射。

    `None`（未声明）-> 自动连内置 demo server，使桥默认就接了真实 MCP；
    `[]`（显式空）  -> 明确「本次不要任何工具」，不连任何 server。
    两者必须区分：早先用 `if mcp_servers` 判断，`[]` 会被 falsy 吞成「用默认」，
    于是调用方想禁掉工具反而启了 demo server。
    任一 server 加载失败不影响其他 server（错误打印日志，不阻断整图）。
    """
    servers = _demo_mcp_servers() if mcp_servers is None else mcp_servers
    tools_map: dict[str, Any] = {}
    for spec in servers:
        name = spec.get("name", "mcp")
        cfg: dict[str, Any] = {
            "transport": spec.get("transport", "stdio"),
            "command": spec.get("command", sys.executable),
            "args": spec.get("args", []),
            "encoding": spec.get("encoding", "utf-8"),
        }
        if spec.get("transport") in ("sse", "streamable_http", "http"):
            cfg["url"] = spec["url"]
        if spec.get("env"):
            cfg["env"] = spec["env"]
        try:
            client = MultiServerMCPClient({name: cfg})
            tools = await client.get_tools()
            for t in tools:
                tools_map[f"{name}__{t.name}"] = t
        except Exception as e:  # noqa: BLE001
            print(f"[MCP] 加载 server '{name}' 失败: {e}", flush=True)
    return tools_map


def _normalize_mcp_output(out) -> str:
    """把 MCP 工具返回的 content blocks 规整为纯文本。

    langchain-mcp-adapters 的 ainvoke 默认返回 list[ContentBlock]
    （形如 [{'type':'text','text':'...','id':'...'}]），这里提取 text 字段，
    使 AG-UI TOOL_CALL_RESULT.content 与前端展示干净可读。
    """
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts: list[str] = []
        for item in out:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(out)


# --------------------------------------------------------------------------
# LangGraph State（outputs 用 list + operator.add，LangGraph 对 list 最友好）
# --------------------------------------------------------------------------


class WFState(TypedDict):
    outputs: Annotated[list, operator.add]  # [{"id": node_id, "text": str}, ...]
    messages: Annotated[list, add_messages]  # LLM 节点的对话历史


def _state_outputs(state) -> list:
    """兼容 dict 与 Pydantic State 两种形态，取出 outputs 列表。"""
    if isinstance(state, dict):
        return state.get("outputs", []) or []
    return getattr(state, "outputs", []) or []


# --------------------------------------------------------------------------
# Checkpointer：HITL 的持久化地基
#
# interrupt() 挂起后必须有个地方存「停在哪」，否则人就无从续跑。LangGraph 把这个
# 职责交给 checkpointer，并且要求 config.configurable.thread_id 作为索引键 ——
# 二者缺一，interrupt() 直接报错。
#
# 默认落 SQLite（<data_dir>/checkpoints.db），跨进程重启存活：人可能隔天才回来。
# 装不上 langgraph-checkpoint-sqlite 时回退 InMemorySaver（功能一致、重启即丢），
# 启动日志会明确标注，不静默降级。
# --------------------------------------------------------------------------

CHECKPOINT_DB_NAME = "checkpoints.db"
_CHECKPOINTER: Any = None
_CHECKPOINTER_KIND: str | None = None


def checkpoint_db_path() -> Path:
    """checkpoint 库路径：AGENT_FLOW_CHECKPOINT_DB 优先，否则 <data_dir>/checkpoints.db。"""
    raw = os.environ.get("AGENT_FLOW_CHECKPOINT_DB")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return flow_store.default_data_dir() / CHECKPOINT_DB_NAME


async def get_checkpointer():
    """进程级单例 checkpointer（首次调用时建立连接）。"""
    global _CHECKPOINTER, _CHECKPOINTER_KIND
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER

    path = checkpoint_db_path()
    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(path))
        _CHECKPOINTER = AsyncSqliteSaver(conn)
        _CHECKPOINTER_KIND = f"sqlite:{path}"
    except Exception as e:  # noqa: BLE001
        from langgraph.checkpoint.memory import InMemorySaver

        _CHECKPOINTER = InMemorySaver()
        _CHECKPOINTER_KIND = "memory"
        print(
            f"[HITL] SQLite checkpointer 不可用（{type(e).__name__}: {e}），"
            f"已回退 InMemorySaver —— 挂起态在进程重启后丢失",
            flush=True,
        )

    if _CHECKPOINTER_KIND and _CHECKPOINTER_KIND.startswith("sqlite"):
        print(f"[HITL] checkpointer = SQLite ({path})", flush=True)
    return _CHECKPOINTER


def checkpointer_kind() -> str | None:
    return _CHECKPOINTER_KIND


async def close_checkpointer() -> None:
    """关闭底层连接（应用退出时调用）。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        return
    conn = getattr(_CHECKPOINTER, "conn", None)
    if conn is not None:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001
            pass
    _CHECKPOINTER = None


# --------------------------------------------------------------------------
# 工具节点：发 AG-UI TOOL_CALL_* 事件，返回输出文本
# --------------------------------------------------------------------------


async def _tool_node(
    data: dict, inp: str, q: asyncio.Queue, nid: str, mcp_tool=None
) -> str:
    tc_id = f"tc_{nid}"
    # 真实 MCP 工具优先：调用标准 MCP 协议工具，发完整 TOOL_CALL_* 事件
    if mcp_tool is not None:
        await q.put(
            {
                "type": "TOOL_CALL_START",
                "toolCallId": tc_id,
                "toolCallName": mcp_tool.name,
            }
        )
        await q.put({"type": "TOOL_CALL_ARGS", "toolCallId": tc_id, "delta": inp})
        await q.put({"type": "TOOL_CALL_END", "toolCallId": tc_id})
        try:
            out = await mcp_tool.ainvoke({"input": inp})
            out = _normalize_mcp_output(out)
        except Exception as e:  # noqa: BLE001
            out = f"[MCP 工具错误] {e}"
        await q.put(
            {
                "type": "TOOL_CALL_RESULT",
                "messageId": f"msg_{nid}",
                "toolCallId": tc_id,
                "content": out,
                "role": "tool",
            }
        )
        return out
    # fallback：本地 mock
    tool = data.get("tool") or "mock"
    await q.put({"type": "TOOL_CALL_START", "toolCallId": tc_id, "toolCallName": tool})
    await q.put({"type": "TOOL_CALL_ARGS", "toolCallId": tc_id, "delta": inp})
    await q.put({"type": "TOOL_CALL_END", "toolCallId": tc_id})
    out = _mock_tool(inp)
    await q.put(
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": f"msg_{nid}",
            "toolCallId": tc_id,
            "content": out,
            "role": "tool",
        }
    )
    return out


# --------------------------------------------------------------------------
# ReAct 工具节点（S1）：模型在节点内部自主决定「调哪个工具 / 调几次 / 何时收工」
#
# 与上面的 _tool_node 的区别：
#   _tool_node      —— 一次动作：拿到上游文本，调指定的那一个工具，返回结果
#   _react_tool_node —— 一个循环：给出工具白名单，模型自己决定调用序列
#
# 循环封在一个 LangGraph 节点内部，对外只有一次 STEP_STARTED / STEP_FINISHED，
# 因此图的形状不变 —— Rust 侧拓扑排序与环检测对本循环完全无感（零引擎改动）。
# --------------------------------------------------------------------------


def _as_list(v) -> list[str]:
    """把 data.tools 规范化为字符串列表（支持数组、逗号/换行分隔的字符串）。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,\n]", v) if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


def _select_tools(whitelist: list[str], tools_map: dict) -> list:
    """按白名单挑选工具。支持全名（server__tool）与裸名（tool）；'*' / 'all' = 全部。"""
    if not whitelist or "*" in whitelist or any(w.lower() == "all" for w in whitelist):
        return list(tools_map.values())
    wanted = set(whitelist)
    return [t for k, t in tools_map.items() if k in wanted or t.name in wanted]


async def _model_step(
    model, messages, q: asyncio.Queue, msg_id: str, is_mock: bool, emit: bool = True
):
    """跑一步模型调用：边流式外发边累积，返回 (文本, tool_calls)。

    真实模型走 astream（AIMessageChunk 支持 `+` 合并，能把分散在多片里的
    tool_call_chunks 拼回完整 tool_calls），所以流式与工具调用可以兼得。

    `emit=False`：只取结果不外发 TEXT_MESSAGE_* 事件（分支判定用 —— condition
    节点的输出必须直通上游文本，判定过程不应污染该节点的 token 流）。
    """
    if is_mock:
        resp = model.invoke(messages)
        text = resp.content or ""
        if emit:
            for tok in _tokenize(text):
                await q.put(
                    {"type": "TEXT_MESSAGE_CONTENT", "messageId": msg_id, "delta": tok}
                )
        return text, (resp.tool_calls or [])

    merged = None
    async for chunk in model.astream(messages):
        merged = chunk if merged is None else merged + chunk
        if chunk.content and emit:
            await q.put(
                {
                    "type": "TEXT_MESSAGE_CONTENT",
                    "messageId": msg_id,
                    "delta": chunk.content,
                }
            )
    if merged is None:
        return "", []
    return (merged.content or ""), (getattr(merged, "tool_calls", None) or [])


async def _react_tool_node(
    data: dict, inp: str, cfg: dict, q: asyncio.Queue, nid: str, tools: list
) -> str:
    """模型在节点内部循环：决定 -> 调工具 -> 回灌结果 -> 再决定，直到不再要工具。"""
    # 注意不能用 `data.get("max_iterations") or 6`：0 是 falsy，会被误吞成 6
    raw_max = data.get("max_iterations")
    try:
        max_iter = int(raw_max) if raw_max is not None else 6
    except (TypeError, ValueError):
        max_iter = 6
    if max_iter < 1:
        max_iter = 1
    system = data.get("system") or (
        "你可以调用工具来完成任务。先判断需要哪个工具，再给出参数；"
        "信息足够后直接输出最终答复，不要继续调用工具。"
    )
    task = (inp or data.get("prompt") or "").strip()
    messages: list = [SystemMessage(content=str(system)), HumanMessage(content=task)]

    has_key = bool(cfg.get("api_key"))
    if has_key:
        from langchain_openai import ChatOpenAI

        base = ChatOpenAI(
            model=data.get("model") or cfg.get("model", "gpt-4o-mini"),
            base_url=cfg.get("base_url"),
            api_key=cfg["api_key"],
            temperature=cfg.get("temperature", 0.7),
        )
        model = base.bind_tools(tools)
    else:
        model = MockReActModel(tools)

    by_name = {t.name: t for t in tools}
    used: list[dict] = []
    final = ""
    hit_limit = True

    for i in range(max_iter):
        msg_id = f"msg_{nid}_r{i}"
        await q.put(
            {"type": "TEXT_MESSAGE_START", "messageId": msg_id, "role": "assistant"}
        )
        text, calls = await _model_step(model, messages, q, msg_id, not has_key)
        await q.put({"type": "TEXT_MESSAGE_END", "messageId": msg_id})

        if not calls:
            final = text or "（模型未产生输出）"
            hit_limit = False
            break

        # 回灌 assistant 轮（含 tool_calls），否则下一轮模型看不到自己刚要求过什么
        call_records = []
        for k, c in enumerate(calls):
            call_records.append(
                {
                    "name": c.get("name", ""),
                    "args": c.get("args") or {},
                    "id": c.get("id") or f"call_{i}_{k}",
                    "type": "tool_call",
                }
            )
        messages.append(AIMessage(content=text, tool_calls=call_records))

        for k, rec in enumerate(call_records):
            tool = by_name.get(rec["name"])
            # 轮次+序号后缀：与 Rust 侧 toolCallId -> (node, tool) 映射一一对应
            tc_id = f"tc_{nid}_r{i}_{k}"
            await q.put(
                {
                    "type": "TOOL_CALL_START",
                    "toolCallId": tc_id,
                    "toolCallName": rec["name"],
                }
            )
            await q.put(
                {
                    "type": "TOOL_CALL_ARGS",
                    "toolCallId": tc_id,
                    "delta": json.dumps(rec["args"], ensure_ascii=False),
                }
            )
            await q.put({"type": "TOOL_CALL_END", "toolCallId": tc_id})
            try:
                if tool is None:
                    out = f"[未知工具] {rec['name']} 不在本节点白名单内"
                else:
                    out = _normalize_mcp_output(await tool.ainvoke(rec["args"]))
            except Exception as e:  # noqa: BLE001
                out = f"[工具错误] {rec['name']}: {e}"
            used.append(
                {"round": i, "tool": rec["name"], "args": rec["args"], "result": out}
            )
            await q.put(
                {
                    "type": "TOOL_CALL_RESULT",
                    "messageId": msg_id,
                    "toolCallId": tc_id,
                    "content": out,
                    "role": "tool",
                }
            )
            messages.append(
                ToolMessage(content=str(out), tool_call_id=rec["id"], name=rec["name"])
            )

    if hit_limit:
        last = used[-1]["result"] if used else "无"
        final = (
            f"[达到最大迭代轮数 {max_iter}] 已执行 {len(used)} 次工具调用仍未收敛。"
            f"最后一次结果：{last}"
        )
    return final


# --------------------------------------------------------------------------
# 条件分支节点（S2）：模型决策的分支路由（Routing pattern）
#
# 与 LangGraph 惯用法的差异（有意为之）：
#   add_conditional_edges 的 path function 需同步执行，而模型调用是异步的。
#   因此判定放在【节点内部】完成，结果写进 state（outputs 条目里的 branch 字段），
#   router 只做纯查表。附带好处：判定进入执行轨迹，可观测 / 可回放。
# --------------------------------------------------------------------------


def _route_tool(candidates: list[dict]) -> dict:
    """把候选分支编码成 choose_branch 工具的 JSON Schema（封闭菜单）。

    候选写进 description 而非 enum：目标是让模型看到「编号 = 什么语义」的映射，
    而不是编造节点 id（n_123 之类的串模型很容易吐错）。
    """
    menu = "；".join(f"{i}={c['label']}" for i, c in enumerate(candidates))
    return {
        "name": "choose_branch",
        "description": (
            "在给定的候选分支中选出唯一一个符合判据的分支。"
            f"候选编号与含义：{menu}。只能返回其中一个编号。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": f"选中分支的编号（0 起，合法范围 0~{len(candidates) - 1}）",
                },
                "reason": {"type": "string", "description": "选择该分支的理由，一句话"},
            },
            "required": ["index"],
        },
    }


def _route_index(calls, n: int, text: str = "") -> tuple[int, str]:
    """从模型输出解析候选编号，返回 (index, reason)；无法解析时 index = -1。

    容错：index 可能被模型返回成字符串 / 带多余文字 / 越界；都没有时从文本里捞第一个数字。
    """
    for c in calls or []:
        if not isinstance(c, dict) or c.get("name") != "choose_branch":
            continue
        args = c.get("args") or {}
        raw = args.get("index")
        try:
            i = int(raw)
        except (TypeError, ValueError):
            m = re.search(r"\d+", str(raw if raw is not None else ""))
            if not m:
                continue
            i = int(m.group())
        return max(0, min(n - 1, i)), str(args.get("reason") or "")
    m = re.search(r"\d+", text or "")
    if m:
        return max(0, min(n - 1, int(m.group()))), "从模型文本中解析"
    return -1, ""


async def _condition_node(
    data: dict, inp: str, cfg: dict, q: asyncio.Queue, nid: str, candidates: list[dict]
) -> tuple[str, str, str, str]:
    """分支判定，返回（直通下游的文本, 选中的 target id, 候选可读名, 理由）。

    直通语义：condition 不改变数据，只决定走哪条边 —— 因此返回的文本 = 上游文本。
    """
    if not candidates:
        return inp, "", "", ""
    if len(candidates) == 1:
        c = candidates[0]
        return inp, c["target"], c["label"], "唯一候选，无需判定"

    expression = str(data.get("expression") or "").strip()
    menu = "\n".join(f"{i}. {c['label']}" for i, c in enumerate(candidates))
    system = (
        "你是工作流里的分支判定器。依据判据，在候选分支中选出唯一一个最符合的分支。\n"
        "必须调用 choose_branch 工具返回候选编号，不要输出其他内容。"
    )
    human = (
        f"[判据]\n{expression or '（未配置判据）'}\n\n"
        f"[待判定的上游内容]\n{inp or '（空）'}\n\n"
        f"[候选分支]\n{menu}"
    )
    messages = [SystemMessage(content=system), HumanMessage(content=human)]
    msg_id = f"msg_{nid}_route"
    has_key = bool(cfg.get("api_key"))

    if has_key:
        from langchain_openai import ChatOpenAI

        base = ChatOpenAI(
            # 路由用便宜快的小模型（Anthropic 对 Routing pattern 的明确建议）
            model=data.get("model")
            or cfg.get("router_model")
            or cfg.get("model", "gpt-4o-mini"),
            base_url=cfg.get("base_url"),
            api_key=cfg["api_key"],
            temperature=0,  # 路由要稳定可复现，不要发挥
        )
        model = base.bind_tools([_route_tool(candidates)])
        text, calls = await _model_step(model, messages, q, msg_id, False, emit=False)
    else:
        # 零 key 的 Mock 只喂「上游内容」，不喂完整 prompt ——
        # prompt 里含判据本身，关键词规则会对判据自我命中（真实模型靠语义理解不受影响）。
        text, calls = await _model_step(
            MockRouterModel(len(candidates), expression),
            [HumanMessage(content=inp)],
            q,
            msg_id,
            True,
            emit=False,
        )

    idx, reason = _route_index(calls, len(candidates), text)
    if idx < 0:
        idx, reason = 0, (reason or "模型未给出有效编号，回退到第一个候选")
    c = candidates[idx]
    return inp, c["target"], c["label"], reason


def _candidates_of(nid: str, edges: list, nodes: dict) -> list[dict]:
    """把某节点的出边翻译成候选菜单（去重、过滤悬空边、保持连线顺序）。

    候选可读名优先级：edge.label > edge.sourceHandle > 目标节点名 > 目标 id。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for e in edges:
        if e.get("source") != nid:
            continue
        t = e.get("target") or ""
        if t not in nodes or t in seen:
            continue
        seen.add(t)
        tdata = nodes[t].get("data") or {}
        label = e.get("label") or e.get("sourceHandle") or tdata.get("label") or t
        out.append({"target": t, "label": str(label)})
    return out


# --------------------------------------------------------------------------
# 环检测与执行上限
# --------------------------------------------------------------------------


def _find_cycle(nodes: dict, edges: list) -> list[str] | None:
    """DFS 找出图中的一条环（返回节点 id 路径，首尾同点）；无环返回 None。

    用途只有一个：把「有环」这件事变得**可见**并据此收紧 recursion_limit。
    bridge 侧不拒绝有环图 —— LangGraph 原生支持循环（condition 回边即合法循环），
    拒绝的应该是 Rust 原生执行器（它只做 Kahn 拓扑排序）。
    """
    adj: dict[str, list[str]] = {nid: [] for nid in nodes}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s in adj and t in nodes:
            adj[s].append(t)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}
    path: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        path.append(u)
        for v in adj[u]:
            if color[v] == GRAY:  # 命中回边：截出环
                return path[path.index(v) :] + [v]
            if color[v] == WHITE:
                r = dfs(v)
                if r:
                    return r
        path.pop()
        color[u] = BLACK
        return None

    for nid in nodes:
        if color[nid] == WHITE:
            r = dfs(nid)
            if r:
                return r
    return None


def _resolve_recursion_limit(wf: dict, n_nodes: int, has_cycle: bool) -> int:
    """显式决定 LangGraph 的 recursion_limit（superstep 上限）。

    优先级：wf.recursion_limit > AGENT_FLOW_RECURSION_LIMIT 环境变量 > 按图规模推算。
    推算规则：无环 DAG -> max(25, n*4)（拓扑序一遍，冗余充足）；
              有环     -> max(12, n*2)（循环必须可控，宁可早失败也不要挂死）。
    """
    raw = wf.get("recursion_limit")
    if raw is None:
        raw = os.getenv("AGENT_FLOW_RECURSION_LIMIT")
    if raw is not None and raw != "":
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            print(
                f"[bridge] 忽略非法的 recursion_limit={raw!r}，改用按图推算", flush=True
            )
    return max(12, n_nodes * 2) if has_cycle else max(25, n_nodes * 4)


# --------------------------------------------------------------------------
# LLM 节点：发 AG-UI TEXT_MESSAGE_* 事件，返回输出文本 + 对话历史
# --------------------------------------------------------------------------


async def _llm_node(data: dict, inp: str, cfg: dict, q: asyncio.Queue, nid: str):
    system = data.get("system") or "你是一个高效、可靠的 AI 助手。"
    prompt = data.get("prompt") or ""
    user_msg = f"{prompt}\n\n[上游上下文]\n{inp}" if inp else prompt
    has_key = cfg.get("api_key")
    msg_id = f"msg_{nid}"
    await q.put(
        {"type": "TEXT_MESSAGE_START", "messageId": msg_id, "role": "assistant"}
    )
    if has_key:
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=cfg.get("model", "gpt-4o-mini"),
            base_url=cfg.get("base_url"),
            api_key=cfg["api_key"],
            temperature=cfg.get("temperature", 0.7),
            streaming=True,
        )
        collected: list[str] = []
        async for chunk in model.astream([("system", system), ("human", user_msg)]):
            tok = chunk.content or ""
            if tok:
                collected.append(tok)
                await q.put(
                    {"type": "TEXT_MESSAGE_CONTENT", "messageId": msg_id, "delta": tok}
                )
        text = "".join(collected)
    else:
        model = MockChatModel()
        text = model.invoke([HumanMessage(user_msg)]).content
        # 切片模拟流式回流（零 key 演示用）：中文逐字、英文按词，避免整句一次性蹦出
        for tok in _tokenize(text):
            await q.put(
                {"type": "TEXT_MESSAGE_CONTENT", "messageId": msg_id, "delta": tok}
            )
    await q.put({"type": "TEXT_MESSAGE_END", "messageId": msg_id})
    return text, [HumanMessage(user_msg), AIMessage(text)]


# --------------------------------------------------------------------------
# human 节点：人工审批 / Human-in-the-Loop（HITL）
# --------------------------------------------------------------------------

# 审批节点的默认候选动作（data.actions 可覆盖）
DEFAULT_HITL_ACTIONS = ("approve", "reject")


def format_decision(decision, inp: str) -> str:
    """把人工决策规整为「往下游传的文本」。

    这是审批节点的价值所在：人不是只投一票，还能**直接改内容**。
      - approve → 人工值；留空则沿用上游输入（纯放行）
      - reject  → "[已驳回] <note>"，下游 condition 可据此分流
      - 其它动作 → "[<action>] <value|note>"（审批类型的自定义扩展）

    单独的 format_decision 便于单测直接覆盖三种分支，不必起图。
    """
    if isinstance(decision, dict):
        action = (
            str(decision.get("decision") or decision.get("action") or "approve")
            .strip()
            .lower()
        )
        value = decision.get("value")
        note = decision.get("note") or decision.get("reason") or ""
    else:
        action = str(decision or "").strip().lower() or "approve"
        value, note = None, ""

    value = "" if value is None else str(value)
    note = str(note)

    if action == "approve":
        # 人工编辑过就用人工的；没编辑就是纯放行，沿用上游
        return value if value.strip() else inp
    if action == "reject":
        return f"[已驳回]{(' ' + note) if note.strip() else ''}"
    tail = value or note
    return f"[{action}]{(' ' + tail) if tail.strip() else ''}"


async def _human_node(data: dict, inp: str, q: asyncio.Queue, nid: str) -> str:
    """人工审批节点：interrupt() 把图挂起，等人给决策后从断点续跑。

    挂起那次调用**不会走到函数末尾** —— interrupt() 抛出的 GraphInterrupt 被
    LangGraph 捕获后整张图冻结、checkpoint 落盘、ainvoke 正常返回（state 里带
    __interrupt__）。人工给决策后续跑时，本函数从 interrupt() 这一行继续执行。

    所以下面那个 return 【只在 resume 之后】才会跑到 —— 这是它与其它节点最大的区别，
    也是「审批节点为什么必须配 checkpointer + thread_id」的答案。
    """
    prompt = (
        data.get("hitlPrompt") or data.get("prompt") or ""
    ).strip() or "请确认是否继续"
    actions = _as_list(data.get("actions")) or list(DEFAULT_HITL_ACTIONS)
    allow_edit = data.get("allow_edit")
    if allow_edit is None:
        allow_edit = True

    decision = interrupt(
        {
            "nodeId": nid,
            "prompt": prompt,
            "input": inp,
            "actions": actions,
            "allowEdit": bool(allow_edit),
        }
    )
    return format_decision(decision, inp)


def _interrupt_of(result) -> dict | None:
    """从 ainvoke 的返回值里取出「第一个挂起点的 payload」。

    挂起时 ainvoke 不抛异常，而是正常返回、state 带 __interrupt__（Interrupt 列表），
    每个 Interrupt 的 .value 即 interrupt(payload) 传入的东西。没有挂起则返回 None。
    """
    if not isinstance(result, dict):
        return None
    items = result.get("__interrupt__")
    if not items:
        return None
    first = items[0]
    value = getattr(first, "value", None)
    if value is None and isinstance(first, dict):
        value = first.get("value")
    out: dict[str, Any] = value if isinstance(value, dict) else {"input": value}
    iid = getattr(first, "id", None) or (
        first.get("id") if isinstance(first, dict) else None
    )
    if iid:
        out.setdefault("interruptId", iid)
    return out


# --------------------------------------------------------------------------
# 图翻译 + 执行
# --------------------------------------------------------------------------


def _run_config(thread_id: str, limit: int) -> dict:
    """执行 config：checkpointer 要求 configurable.thread_id；上限一律显式传。

    thread_id 缺失时 interrupt() 会直接报错，而 recursion_limit 缺失会吃 LangGraph
    内部的 10007 —— 两个都必须显式给。
    """
    return {
        "recursion_limit": limit,
        "configurable": {"thread_id": thread_id},
    }


async def _finalize(
    result, q: asyncio.Queue, run_id: str, thread_id: str, nodes: dict
) -> None:
    """统一收尾：挂起（RUN_PAUSED）或正常结束（RUN_FINISHED）。

    挂起时 ainvoke **不抛异常**、正常返回，只有检查 __interrupt__ 才知道图停住了。
    漏掉这个检查的症状很隐蔽：流程看起来「跑完了」，其实后半截从未执行。
    """
    end_ids = [nid for nid, n in nodes.items() if n.get("type") == "end"]
    final = ""
    for o in _state_outputs(result):
        if isinstance(o, dict) and o.get("id") in end_ids:
            final = o.get("text", "")
    if not final:
        for o in _state_outputs(result):
            if isinstance(o, dict):
                final = o.get("text", "")

    paused = _interrupt_of(result)
    if paused:
        # AG-UI 标准协议没有「暂停」语义 —— 用自定义扩展事件 + RUN_FINISHED(paused) 表达
        await q.put(
            {
                "type": "RUN_PAUSED",
                "threadId": thread_id,
                "runId": run_id,
                "stepName": paused.get("nodeId") or "",
                "prompt": paused.get("prompt") or "请确认是否继续",
                "input": paused.get("input") or "",
                "actions": paused.get("actions") or [],
                "allowEdit": bool(paused.get("allowEdit", True)),
                "interruptId": paused.get("interruptId") or "",
            }
        )
        await q.put(
            {"type": "RUN_FINISHED", "runId": run_id, "result": final, "paused": True}
        )
        return
    # AG-UI: 运行结束（result 为应用扩展字段，供桥回填 workflow:done）
    await q.put({"type": "RUN_FINISHED", "runId": run_id, "result": final})


async def _compile_graph(
    wf: dict, cfg: dict, q: asyncio.Queue, tools_map: dict, input_text: str
):
    """把 {nodes, edges} 翻译成 LangGraph StateGraph 并 compile（挂 checkpointer）。

    run 与 resume **各调用一次** —— 两次构图必须完全一致（同节点名、同边），
    否则 checkpoint 恢复出的执行位置会对不上。所以这段逻辑只此一份。
    返回 (graph, nodes, edges)。
    """
    nodes = {n["id"]: n for n in wf.get("nodes", [])}
    edges = wf.get("edges", [])

    try:
        preds: dict[str, list[str]] = {
            nid: [e["source"] for e in edges if e["target"] == nid] for nid in nodes
        }
        # condition 节点的候选菜单（出边 -> 编号候选），构图与判定共用同一份
        cond_candidates: dict[str, list[dict]] = {
            nid: _candidates_of(nid, edges, nodes)
            for nid, n in nodes.items()
            if n.get("type") == "condition"
        }

        builder = StateGraph(WFState)

        def make_node(nid: str, ntype: str, data: dict, tools_map: dict):
            async def node(state: dict) -> dict:
                outs = _state_outputs(state)
                inp = "\n---\n".join(
                    o.get("text", "")
                    for o in outs
                    if isinstance(o, dict) and o.get("id") in preds[nid]
                )
                # AG-UI: 步骤开始
                await q.put(
                    {"type": "STEP_STARTED", "stepName": nid, "nodeType": ntype}
                )
                out = inp
                msgs: list = []
                branch = branch_label = branch_reason = ""
                if ntype == "start":
                    # 外部传入的非空 input（聊天页 / HTTP 调用方）优先；
                    # 节点配置的固定 input 只是「编辑器直跑时的默认值」兜底。
                    # 反过来的优先级会让聊天页永远吃到节点里写死的旧输入。
                    out = (input_text or "").strip() or (
                        data.get("input") or ""
                    ).strip()
                elif ntype == "llm":
                    out, msgs = await _llm_node(data, inp, cfg, q, nid)
                elif ntype == "condition":
                    # 判定在节点内完成（模型调用是异步的），结果经 state 交给 router 查表
                    out, branch, branch_label, branch_reason = await _condition_node(
                        data, inp, cfg, q, nid, cond_candidates.get(nid, [])
                    )
                elif ntype == "tool":
                    whitelist = _as_list(data.get("tools"))
                    if whitelist:
                        # ReAct 模式：模型在节点内部自主决定调哪个工具、调几次
                        react_tools = _select_tools(whitelist, tools_map)
                        if react_tools:
                            out = await _react_tool_node(
                                data, inp, cfg, q, nid, react_tools
                            )
                        else:
                            # 白名单没匹配上任何已加载工具 -> 退回单工具直调，不静默失败
                            print(
                                f"[ReAct] 节点 {nid} 的白名单 {whitelist} 未匹配到任何工具，"
                                f"已退回 direct 模式",
                                flush=True,
                            )
                            out = await _tool_node(data, inp, q, nid, None)
                    else:
                        # direct 模式（兼容旧行为）：单工具直调
                        mcp_name = data.get("mcp_tool")
                        mcp_tool_ins = tools_map.get(mcp_name) if mcp_name else None
                        out = await _tool_node(data, inp, q, nid, mcp_tool_ins)
                elif ntype == "human":
                    # 人工审批：interrupt() 挂起整张图，等人给决策后从断点续跑。
                    # 挂起那次调用【不会执行到这里之后的任何代码】—— 见 _human_node。
                    out = await _human_node(data, inp, q, nid)
                elif ntype == "end":
                    out = inp
                # AG-UI: 步骤结束（output 为应用扩展字段，供桥回填内部模型）
                step_ev: dict[str, Any] = {
                    "type": "STEP_FINISHED",
                    "stepName": nid,
                    "output": out,
                }
                entry: dict[str, Any] = {"id": nid, "text": out}
                if branch:
                    # condition 专属：判定结果一份给桥/前端（事件），一份给 router（state）
                    step_ev.update(
                        {
                            "branch": branch,
                            "branchLabel": branch_label,
                            "branchReason": branch_reason,
                        }
                    )
                    entry["branch"] = branch
                await q.put(step_ev)
                update: dict[str, Any] = {"outputs": [entry]}
                if msgs:
                    update["messages"] = msgs
                return update

            return node

        for nid, n in nodes.items():
            builder.add_node(
                nid,
                make_node(nid, n.get("type", ""), n.get("data", {}) or {}, tools_map),
            )

        # edges：普通边 + condition 节点的条件边（router 只查表，不做 IO）
        for nid, n in nodes.items():
            if n.get("type") == "condition":
                cands = cond_candidates.get(nid) or []
                if len(cands) >= 2:

                    def router(state, _nid=nid, _cands=cands):
                        """纯查表：读 condition 节点写进 state 的判定结果。

                        判定缺失（异常路径）时兜底到第一个候选，保证路由永远有值。
                        """
                        for o in reversed(_state_outputs(state)):
                            if (
                                isinstance(o, dict)
                                and o.get("id") == _nid
                                and o.get("branch")
                            ):
                                return o["branch"]
                        return _cands[0]["target"]

                    builder.add_conditional_edges(
                        nid, router, {c["target"]: c["target"] for c in cands}
                    )
                    continue
                if len(cands) == 1:
                    # 只有一个出口：无需判定，直接连边（也不该白花一次模型调用）
                    builder.add_edge(nid, cands[0]["target"])
                    continue
                # 没有有效出边：该分支即为终点，不连边
                continue
            for e in edges:
                if e["source"] == nid:
                    builder.add_edge(nid, e["target"])

        for nid, n in nodes.items():
            if n.get("type") == "start":
                builder.add_edge(START, nid)
            if n.get("type") == "end":
                builder.add_edge(nid, END)

        cp = await get_checkpointer()
        return builder.compile(checkpointer=cp), nodes, edges
    except Exception as e:  # noqa: BLE001
        import traceback as _tb

        await q.put(
            {
                "type": "RUN_ERROR",
                "message": f"[bridge 构图错误] {e}\n{_tb.format_exc()}",
            }
        )
        raise


async def _run_graph(
    wf: dict,
    input_text: str,
    cfg: dict,
    q: asyncio.Queue,
    thread_id: str,
    run_id: str,
    mcp_servers: list | None = None,
) -> None:
    """整图执行（第一次调用；遇到 human 节点则在 interrupt 处挂起）。"""
    try:
        nodes = {n["id"]: n for n in wf.get("nodes", [])}
        edges = wf.get("edges", [])

        # 环检测 + 显式执行上限：必须在 RUN_STARTED 之前算好，作为扩展字段随事件下发
        cycle = _find_cycle(nodes, edges)
        limit = _resolve_recursion_limit(wf, len(nodes), bool(cycle))
        if cycle:
            print(
                f"[bridge] 检测到环：{' → '.join(cycle)}"
                f"（recursion_limit={limit}，超过即报错，不再挂死）",
                flush=True,
            )

        # AG-UI: 运行开始（recursionLimit / hasCycle 为应用扩展字段，桥会忽略未知字段）
        await q.put(
            {
                "type": "RUN_STARTED",
                "threadId": thread_id,
                "runId": run_id,
                "recursionLimit": limit,
                "hasCycle": bool(cycle),
            }
        )

        if not nodes:
            await q.put({"type": "RUN_ERROR", "message": "[空图] 没有任何节点"})
            return

        # 加载真实 MCP 工具（默认连内置 demo server；可被 body.mcp_servers 覆盖）
        tools_map = await _load_mcp_tools(mcp_servers)
        graph, nodes, _ = await _compile_graph(wf, cfg, q, tools_map, input_text)

        try:
            # 显式传 recursion_limit + thread_id（不再吃 LangGraph 内部默认的 10007）
            result = await graph.ainvoke(
                {"outputs": [], "messages": []}, _run_config(thread_id, limit)
            )
        except GraphRecursionError as e:
            await q.put(
                {
                    "type": "RUN_ERROR",
                    "message": (
                        f"[超过执行步数上限 recursion_limit={limit}] 图很可能在循环且未收敛。"
                        f"可给流程设置更大的 recursion_limit、或给循环补上明确的退出判据。"
                        f"原始错误：{e}"
                    ),
                }
            )
            return

        await _finalize(result, q, run_id, thread_id, nodes)
    except Exception as e:  # noqa: BLE001
        import traceback as _tb

        await q.put(
            {"type": "RUN_ERROR", "message": f"[bridge 错误] {e}\n{_tb.format_exc()}"}
        )
    finally:
        await q.put(None)


async def _resume_graph(
    wf: dict,
    cfg: dict,
    q: asyncio.Queue,
    thread_id: str,
    run_id: str,
    decision: Any,
    mcp_servers: list | None = None,
) -> None:
    """从挂起点续跑（HITL 的第二半）：Command(resume=...) + 同一 thread_id。

    已执行过的节点**不会重跑** —— LangGraph 从 checkpoint 恢复，只从断点之后继续。
    因此 input_text 传空串：start 节点这一轮根本不会被执行。
    """
    try:
        nodes = {n["id"]: n for n in wf.get("nodes", [])}
        edges = wf.get("edges", [])
        cycle = _find_cycle(nodes, edges)
        limit = _resolve_recursion_limit(wf, len(nodes), bool(cycle))

        await q.put(
            {
                "type": "RUN_STARTED",
                "threadId": thread_id,
                "runId": run_id,
                "recursionLimit": limit,
                "hasCycle": bool(cycle),
                "resumed": True,
            }
        )

        if not nodes:
            await q.put({"type": "RUN_ERROR", "message": "[空图] 没有任何节点"})
            return

        tools_map = await _load_mcp_tools(mcp_servers)
        graph, nodes, _ = await _compile_graph(wf, cfg, q, tools_map, "")

        # 先确认这个 thread 上确实有等待中的挂起点。少了这一步，「压根没挂起」时
        # LangGraph 会抛内部错误，报错信息对使用者毫无意义。
        try:
            snap = await graph.aget_state(_run_config(thread_id, limit))
        except Exception:  # noqa: BLE001
            snap = None
        if not snap or not getattr(snap, "next", None):
            await q.put(
                {
                    "type": "RUN_ERROR",
                    "message": (
                        f"[无法续跑] thread {thread_id} 上没有等待中的挂起点。可能原因："
                        f"threadId 不对、该流程并未挂起、或挂起态已失效"
                        f"（当前 checkpointer={checkpointer_kind()}）。"
                    ),
                }
            )
            return

        try:
            result = await graph.ainvoke(
                Command(resume=decision), _run_config(thread_id, limit)
            )
        except GraphRecursionError as e:
            await q.put(
                {
                    "type": "RUN_ERROR",
                    "message": (
                        f"[续跑时超过执行步数上限 recursion_limit={limit}] 循环未收敛。"
                        f"原始错误：{e}"
                    ),
                }
            )
            return

        await _finalize(result, q, run_id, thread_id, nodes)
    except Exception as e:  # noqa: BLE001
        import traceback as _tb

        await q.put(
            {
                "type": "RUN_ERROR",
                "message": f"[bridge 续跑错误] {e}\n{_tb.format_exc()}",
            }
        )
    finally:
        await q.put(None)


# --------------------------------------------------------------------------
# HTTP 入口
# --------------------------------------------------------------------------


def _sse_response(
    wf: dict,
    input_text: str,
    cfg: dict,
    mcp_servers: list | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    resume_decision: Any = _NO_RESUME,
) -> StreamingResponse:
    """把一次图执行包装成 AG-UI SSE 流（/run、/flows/{id}/run、/flows/{id}/resume 共用）。

    `resume_decision` 非默认值时为【续跑】：走 Command(resume=...) 从断点继续。
    用哨兵而非 None 判据，是因为「审核人明确提交 null」与「不是续跑」必须区分开。
    """
    q: asyncio.Queue = asyncio.Queue()
    thread_id = thread_id or f"wf-{uuid.uuid4().hex[:8]}"
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"

    async def event_gen():
        if resume_decision is _NO_RESUME:
            task = asyncio.create_task(
                _run_graph(wf, input_text, cfg, q, thread_id, run_id, mcp_servers)
            )
        else:
            task = asyncio.create_task(
                _resume_graph(
                    wf, cfg, q, thread_id, run_id, resume_decision, mcp_servers
                )
            )
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        await task

    return StreamingResponse(event_gen(), media_type="text/event-stream")


async def _run_collect(
    wf: dict,
    input_text: str,
    cfg: dict,
    mcp_servers: list | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    resume_decision: Any = _NO_RESUME,
) -> dict:
    """执行图并收集最终结果（非流式调用：脚本 / CI 场景）。"""
    q: asyncio.Queue = asyncio.Queue()
    thread_id = thread_id or f"wf-{uuid.uuid4().hex[:8]}"
    run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
    if resume_decision is _NO_RESUME:
        task = asyncio.create_task(
            _run_graph(wf, input_text, cfg, q, thread_id, run_id, mcp_servers)
        )
    else:
        task = asyncio.create_task(
            _resume_graph(wf, cfg, q, thread_id, run_id, resume_decision, mcp_servers)
        )
    result, error, steps = "", None, 0
    recursion_limit, has_cycle = None, None
    paused, pending = False, None
    while True:
        ev = await q.get()
        if ev is None:
            break
        etype = ev.get("type")
        if etype == "RUN_STARTED":
            recursion_limit = ev.get("recursionLimit")
            has_cycle = ev.get("hasCycle")
        elif etype == "STEP_FINISHED":
            steps += 1
        elif etype == "RUN_PAUSED":
            paused, pending = True, ev
        elif etype == "RUN_FINISHED":
            result = ev.get("result") or ""
            if ev.get("paused"):
                paused = True
        elif etype == "RUN_ERROR":
            error = ev.get("message") or "unknown error"
    await task
    out = {
        "threadId": thread_id,
        "runId": run_id,
        "result": result,
        "steps": steps,
        "error": error,
        "recursionLimit": recursion_limit,
        "hasCycle": has_cycle,
        "paused": paused,
    }
    if pending:
        # 挂起态单独成块：调用方据此决定「拿 threadId 去调 resume」
        out["awaiting"] = {
            "nodeId": pending.get("stepName") or "",
            "prompt": pending.get("prompt") or "",
            "input": pending.get("input") or "",
            "actions": pending.get("actions") or [],
            "allowEdit": bool(pending.get("allowEdit", True)),
        }
    return out


@app.post("/run")
async def run(req: Request):
    """整图调用：body 直接携带 {nodes, edges, input, config}。"""
    body = await req.json()
    # AG-UI 标准字段：每次运行一个 thread/run（可由调用方覆盖）
    return _sse_response(
        body,
        body.get("input", ""),
        body.get("config", {}),
        body.get("mcp_servers"),
        body.get("threadId"),
        body.get("runId"),
    )


# --------------------------------------------------------------------------
# 按 flow_id 调用：从 SQLite 读图后直接执行（外部 HTTP 无需知道图结构）
# --------------------------------------------------------------------------


def _flow_or_404(flow_id: str) -> dict:
    flow = flow_store.get_flow(flow_id)
    if flow is None:
        raise HTTPException(
            status_code=404,
            detail=f"未找到流程 {flow_id}（库：{flow_store.db_path()}）",
        )
    return flow


def _flow_meta(flow: dict) -> dict:
    """流程元信息（不含 graph）。"""
    graph = flow_store.graph_of(flow)
    return {
        "id": flow.get("id"),
        "name": flow.get("name", "未命名"),
        "version": flow.get("version", 1),
        "status": flow.get("status"),
        "updated_at": flow.get("updated_at"),
        "nodes": len(graph.get("nodes", []) or []),
        "edges": len(graph.get("edges", []) or []),
    }


@app.get("/flows")
async def list_flows(all: bool = False):
    """列出流程。默认只列已发布的；`?all=true` 列出全部（含草稿）。"""
    flows = flow_store.list_flows(published_only=not all)
    return {
        "db": str(flow_store.db_path()),
        "dbExists": flow_store.db_exists(),
        "publishedOnly": not all,
        "count": len(flows),
        "flows": [_flow_meta(f) for f in flows],
    }


@app.get("/flows/{flow_id}")
async def get_flow(flow_id: str):
    """单个流程的元信息（含节点/边数量，便于确认 id 是否拿对）。"""
    return _flow_meta(_flow_or_404(flow_id))


# ---------------------------------------------------------------------------
# 请求模型（仅用于 OpenAPI/Swagger 文档渲染 + 基础类型校验）
# ---------------------------------------------------------------------------
# 之前端点直接 await req.json() 收裸 dict，Swagger 里看不到 Request body。
# 字段全部可选 + extra="allow"（未知字段照收不拒），语义与旧的裸 dict 完全一致，
# 不会破坏任何现有调用方（聊天页 / Rust bridge / curl 脚本）。
class RunByIdBody(BaseModel):
    """POST /flows/{flow_id}/run 的 body（全部可选）。"""

    model_config = ConfigDict(extra="allow")

    input: str | None = Field(
        "",
        description="传给 start 节点的输入文本（用户实时输入优先于 start 节点配置的默认值）",
    )
    config: dict | None = Field(
        None,
        description="LLM 配置；不传 = 自动读 SQLite kv['config']；传 {} = 强制零 key Mock",
    )
    mcp_servers: list | None = Field(None, description="覆盖默认的 MCP server 列表")
    recursion_limit: int | None = Field(
        None, description="覆盖执行步数上限（不传则按图规模推算）"
    )
    threadId: str | None = Field(
        None, description="自定义 thread_id（不传自动生成；同 thread 续跑用于 HITL）"
    )
    runId: str | None = Field(None, description="自定义 run_id（不传自动生成）")
    stream: bool | None = Field(
        True, description="true 返回 AG-UI SSE；false 等跑完返回 JSON"
    )


class ResumeByIdBody(BaseModel):
    """POST /flows/{flow_id}/resume 的 body。"""

    model_config = ConfigDict(extra="allow")

    threadId: str | None = Field(
        None, description="挂起时由 run 返回体 / RUN_PAUSED 事件带出的 thread_id"
    )
    thread_id: str | None = Field(
        None, description="threadId 的 snake_case 别名（二选一）"
    )
    decision: str | dict | None = Field(
        None,
        description='人的决策：{"decision":"approve","value":"人工改过的内容","note":"理由"} 或裸字符串 "approve" / "reject"',
    )
    config: dict | None = Field(
        None, description="同 /run（不传 = 自动读 SQLite kv['config']）"
    )
    mcp_servers: list | None = Field(None, description="同 /run")
    recursion_limit: int | None = Field(None, description="同 /run")
    runId: str | None = Field(None, description="同 /run")
    stream: bool | None = Field(True, description="同 /run（默认 true SSE）")


@app.post("/flows/{flow_id}/run")
async def run_flow_by_id(flow_id: str, body: RunByIdBody):
    """按 flow_id 直接执行流程。

    body（全部可选）：
        input        传给 start 节点的输入文本
        config       不传 -> 自动读 SQLite kv['config']；传 {} -> 强制零 key Mock
        mcp_servers  覆盖默认的 MCP server 列表
        stream       true（默认）返回 AG-UI SSE；false 等跑完返回 JSON
    """
    body = body.model_dump(exclude_unset=True)

    flow = _flow_or_404(flow_id)
    if flow.get("status") != "published":
        raise HTTPException(
            status_code=409,
            detail=f"流程「{flow.get('name')}」尚未发布（当前 status={flow.get('status')}），"
            f"请先在 agent-flow 里发布",
        )

    graph = flow_store.graph_of(flow)
    if not graph.get("nodes"):
        raise HTTPException(
            status_code=422, detail=f"流程 {flow_id} 的图为空或无法解析"
        )
    # 允许调用方覆盖执行步数上限（不传则由 bridge 按图规模显式推算）
    if "recursion_limit" in body:
        graph = {**graph, "recursion_limit": body["recursion_limit"]}

    # 关键：不传 config 时自动带上 app 里配好的 LLM 配置，避免落到 MockChatModel
    cfg = body["config"] if "config" in body else flow_store.stored_config()
    input_text = body.get("input", "") or ""
    mcp_servers = body.get("mcp_servers")
    thread_id = body.get("threadId") or f"flow-{flow_id[:8]}-{uuid.uuid4().hex[:6]}"
    run_id = body.get("runId") or f"run-{uuid.uuid4().hex[:8]}"

    if body.get("stream", True):
        return _sse_response(graph, input_text, cfg, mcp_servers, thread_id, run_id)

    out = await _run_collect(graph, input_text, cfg, mcp_servers, thread_id, run_id)
    payload = {
        "ok": out["error"] is None,
        "flowId": flow_id,
        "flowName": flow.get("name"),
        "version": flow.get("version", 1),
        "threadId": out["threadId"],
        "runId": out["runId"],
        "steps": out["steps"],
        "result": out["result"],
        "paused": out["paused"],
        "recursionLimit": out["recursionLimit"],
        "hasCycle": out["hasCycle"],
        "usedStoredConfig": "config" not in body and bool(cfg),
    }
    if out.get("awaiting"):
        # 挂起态：告诉调用方「拿这个 threadId 去哪续跑」，不必去翻文档
        payload["awaiting"] = out["awaiting"]
        payload["resumeUrl"] = f"/flows/{flow_id}/resume"
    if out["error"]:
        payload["error"] = out["error"]
        return JSONResponse(payload, status_code=500)
    return payload


# ---------------------------------------------------------------------------
# 发布即分享：聊天页
# ---------------------------------------------------------------------------
# 聊天页已迁到前端 public/chat.html，由 Vite 静态资源服务直接提供。
# bridge 只保留 API（/flows/{id}/run），不再服务端生成页面。
# 发布链接格式：http://localhost:1420/chat.html?flowId=<id>&bridgeUrl=<bridge>&name=<name>


@app.post("/flows/{flow_id}/resume")
async def resume_flow_by_id(flow_id: str, body: ResumeByIdBody):
    """恢复一个「等待人工审批」而挂起的运行（HITL 的第二半）。

    body：
        threadId    必填 —— 挂起时由 run 的返回 / RUN_PAUSED 事件带出
        decision    必填 —— 人的决策：
                    {"decision":"approve","value":"人工改过的内容","note":"理由"}
                    也可给裸字符串 "approve" / "reject"
        config      不传 -> 自动读 SQLite kv['config']（与 run 一致）
        mcp_servers / stream   同 /run（stream 默认 true）

    两处刻意的设计：
    1) decision 缺失时**报错而不是默认放行** —— 审批的默认答案永远不能是「通过」。
    2) 不检查 published —— 一个已经跑到一半的 run，不该因为中途取消了发布而无法收尾。
    """
    body = body.model_dump(exclude_unset=True)

    thread_id = body.get("threadId") or body.get("thread_id")
    if not thread_id:
        raise HTTPException(
            status_code=422,
            detail="缺少 threadId（挂起时由 RUN_PAUSED 事件 / run 的返回带出）",
        )
    if body.get("decision") is None:
        raise HTTPException(
            status_code=422,
            detail='缺少 decision —— 审批不能默认放行，请显式给 {"decision":"approve"} '
            '或 "reject"（可带 value / note）',
        )

    flow = _flow_or_404(flow_id)
    graph = flow_store.graph_of(flow)
    if not graph.get("nodes"):
        raise HTTPException(
            status_code=422, detail=f"流程 {flow_id} 的图为空或无法解析"
        )
    if "recursion_limit" in body:
        graph = {**graph, "recursion_limit": body["recursion_limit"]}

    cfg = body["config"] if "config" in body else flow_store.stored_config()
    mcp_servers = body.get("mcp_servers")
    run_id = body.get("runId") or f"run-{uuid.uuid4().hex[:8]}"
    decision = body["decision"]

    if body.get("stream", True):
        return _sse_response(
            graph, "", cfg, mcp_servers, thread_id, run_id, resume_decision=decision
        )

    out = await _run_collect(
        graph, "", cfg, mcp_servers, thread_id, run_id, resume_decision=decision
    )
    payload = {
        "ok": out["error"] is None,
        "resumed": True,
        "flowId": flow_id,
        "flowName": flow.get("name"),
        "version": flow.get("version", 1),
        "threadId": out["threadId"],
        "runId": out["runId"],
        "steps": out["steps"],
        "result": out["result"],
        "paused": out["paused"],
        "recursionLimit": out["recursionLimit"],
        "hasCycle": out["hasCycle"],
    }
    if out.get("awaiting"):
        # 续跑后可能又挂在【下一个】审批节点上 —— 同样把续跑入口带出来
        payload["awaiting"] = out["awaiting"]
        payload["resumeUrl"] = f"/flows/{flow_id}/resume"
    if out["error"]:
        payload["error"] = out["error"]
        return JSONResponse(payload, status_code=500)
    return payload


@app.get("/mcp/tools")
async def mcp_tools():
    """列出当前已加载的真实 MCP 工具（默认连内置 demo server）。"""
    tools_map = await _load_mcp_tools(None)
    return {"tools": sorted(tools_map.keys())}


@app.get("/health")
async def health():
    # 主动初始化 checkpointer，让 health 能直接暴露「是否回退到了内存」
    await get_checkpointer()
    return {
        "status": "ok",
        "version": app.version,
        "db": str(flow_store.db_path()),
        "checkpointer": checkpointer_kind(),
    }
