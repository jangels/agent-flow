"""真实标准 MCP server（demo）——stdio 传输，供 agent-flow langgraph-bridge 验证「接真实 MCP」。

用 mcp SDK 的 FastMCP 实现，走标准 stdio JSON-RPC 协议；可被任意 MCP client 调用，
与 server.py 里的 _mock_tool 本质不同（后者是 Python 内硬编码函数）。

工具统一接受 input:str，便于 agent-flow 上游文本直接灌入；内部自行解析。

运行（被 bridge 作为子进程拉起，无需手动启动）：
    python demo_server.py
"""
import re
from datetime import datetime

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agent-flow-demo-mcp")


@mcp.tool()
def add(input: str) -> str:
    """从文本中提取两个整数相加。输入形如 '计算 3 加 5'。"""
    nums = [int(x) for x in re.findall(r"-?\d+", input)]
    if len(nums) >= 2:
        return f"{nums[0]} + {nums[1]} = {nums[0] + nums[1]}"
    return "无法解析出两个整数"


@mcp.tool()
def get_time(input: str = "") -> str:
    """返回当前服务器时间（ISO8601）。"""
    return datetime.now().isoformat(timespec="seconds")


@mcp.tool()
def echo(input: str) -> str:
    """回显输入文本，验证 MCP 工具真实被调用。"""
    return f"echo: {input}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
