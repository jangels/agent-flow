"""agent-flow 已发布流程的 MCP 入口（标准 stdio MCP server）。

把 agent-flow 桌面应用里【已发布】的流程暴露为 MCP 工具，使任意 MCP client
（Claude Desktop / 任意 MCP 网关 / 自研 agent）都能：

  - 列出已发布流程        -> list_published_flows
  - 按 flow_id 单独执行   -> run_flow

这样「发布」这个动作 = 让流程进入 MCP 可见集；「单独执行」= 调 run_flow 工具。

来源与后端：
  - 流程存储：agent-flow 的 SQLite 数据库
      默认: ~/.agent-flow/agent_flow.db      （刻意不放在系统 app 数据目录）
      可用 AGENT_FLOW_HOME 覆盖数据目录、或 AGENT_FLOW_DB 直接指定 .db 文件
      表：flows(id, name, version, graph, status, updated_at)
    读取逻辑统一由上层 flow_store.py 提供（与 server.py 共用，避免重复实现）。
  - 执行后端：langgraph-bridge 服务，走 POST /flows/{flow_id}/run（非流式）
      默认 http://127.0.0.1:8000，可用环境变量 LANGGRAPH_BRIDGE_URL 覆盖
      （给 .../run 或裸 base 都可以，两者都会被规整）

并发说明：agent-flow(Rust) 与本 server(Python) 是两个进程访问同一 SQLite。
数据库以 WAL 模式打开，本侧使用【只读连接】（mode=ro），读写互不阻塞。

运行（stdio）：
    AGENT_FLOW_DB=/path/to/agent_flow.db python flows_server.py

在 MCP client 中挂载示例（Claude Desktop / 通用 mcpServers 配置）：
    {
      "mcpServers": {
        "agent-flow": {
          "command": "python",
          "args": ["/abs/path/to/langgraph-bridge/mcp_servers/flows_server.py"],
          "env": {
            "AGENT_FLOW_HOME": "~/.agent-flow",
            "LANGGRAPH_BRIDGE_URL": "http://127.0.0.1:8000"
          }
        }
      }
    }
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# 共享存储层在上一级目录（langgraph-bridge/flow_store.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import flow_store  # noqa: E402


def _bridge_base() -> str:
    """执行后端 base url；兼容「给 .../run」与「给裸 base」两种写法。"""
    raw = os.environ.get("LANGGRAPH_BRIDGE_URL", "http://127.0.0.1:8000")
    raw = raw.rstrip("/")
    if raw.endswith("/run"):
        raw = raw[: -len("/run")]
    return raw


mcp = FastMCP("agent-flow-flows")


@mcp.tool()
def list_published_flows() -> str:
    """列出 agent-flow 中所有【已发布】的流程（id / 名称 / 版本）。"""
    flows = flow_store.list_flows(published_only=True)
    if not flows:
        return ("当前没有已发布的流程"
                f"（库：{flow_store.db_path()}；在 agent-flow 里点「发布」后即可在此处看到）。")
    lines = [
        f"- {f.get('id')} · {f.get('name', '未命名')} · v{f.get('version', 1)}"
        for f in flows
    ]
    return "已发布流程：\n" + "\n".join(lines)


@mcp.tool()
async def run_flow(flow_id: str, input: str = "") -> str:
    """单独执行某个【已发布】流程，返回最终结果。

    Args:
        flow_id: 流程 id（先用 list_published_flows 获取）
        input: 传给流程起始节点的输入文本（可选）
    """
    # 先本地校验，能在执行后端未运行时也给出准确的「不存在 / 未发布」提示
    flow = flow_store.get_flow(flow_id)
    if flow is None:
        return f"[错误] 未找到流程 {flow_id}（库：{flow_store.db_path()}）"
    if flow.get("status") != "published":
        return f"[错误] 流程「{flow.get('name')}」尚未发布，请先在 agent-flow 中发布"

    url = f"{_bridge_base()}/flows/{flow_id}/run"
    payload = {"input": input, "stream": False}
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                return (
                    f"[错误] 执行后端 {resp.status_code}: "
                    f"{resp.text[:400]}"
                )
            data = resp.json()
            return data.get("result") or "[错误] 未收到执行结果"
    except Exception as e:  # noqa: BLE001
        return (f"[错误] 调用执行后端失败"
                f"（请确认 langgraph-bridge 已在 {_bridge_base()} 运行）: {e}")


if __name__ == "__main__":
    mcp.run()
