"""ReAct 工具节点（S1）自测：直接驱动 _react_tool_node，校验事件流与循环语义。

运行：
    cd langgraph-bridge && uv run python tests/test_react.py

覆盖：
  1. 白名单解析（数组 / 字符串 / 裸名 / '*' / 'all'）
  2. Mock ReAct 循环的事件序列与 toolCallId 唯一性
  3. 真实 MCP 工具确实被调用（stdio demo server）
  4. max_iterations 硬上限生效
  5. 旧 data.mcp_tool 单工具直调路径未被破坏
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as S  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail else ""))


def drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        item = q.get_nowait()
        if item is not None:
            out.append(item)
    return out


def types_of(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def ids_preview(events: list[dict]) -> list[str]:
    return [f"{e['type']}:{e.get('toolCallId')}" for e in events if "toolCallId" in e]


async def main() -> int:
    tools_map = await S._load_mcp_tools(None)
    print(f"\n已加载 MCP 工具: {sorted(tools_map.keys())}")
    if not tools_map:
        print("!! 未加载到任何 MCP 工具，后续用例无法进行")
        return 1

    # ---------------------------------------------------------------- 1. 白名单
    print("\n[1] 白名单解析")
    check("数组形式", S._as_list(["a", "b"]) == ["a", "b"])
    check("字符串逗号分隔", S._as_list("a, b ,c") == ["a", "b", "c"])
    check("字符串换行分隔", S._as_list("a\nb") == ["a", "b"])
    check("None -> 空", S._as_list(None) == [])
    check("空字符串 -> 空", S._as_list("   ") == [])

    all_tools = S._select_tools(["*"], tools_map)
    check("'*' 选中全部", len(all_tools) == len(tools_map), f"{len(all_tools)} 个")
    check("'all' 选中全部", len(S._select_tools(["all"], tools_map)) == len(tools_map))

    full_name = sorted(tools_map.keys())[0]
    bare = full_name.split("__")[-1]
    check("全名匹配", [t.name for t in S._select_tools([full_name], tools_map)] == [bare])
    check("裸名匹配", [t.name for t in S._select_tools([bare], tools_map)] == [bare])
    check("不存在的名字 -> 空", S._select_tools(["nope__nope"], tools_map) == [])

    # ---------------------------------------------------- 2. Mock 循环事件流
    print("\n[2] Mock ReAct 循环（计算 3 加 5 -> demo__add）")
    tools = S._select_tools(["*"], tools_map)
    q: asyncio.Queue = asyncio.Queue()
    out = await S._react_tool_node({}, "计算 3 加 5", {}, q, "t_add", tools)
    evs = drain(q)
    ts = types_of(evs)

    check("首个事件是 TEXT_MESSAGE_START", ts[0] == "TEXT_MESSAGE_START", ts[0])
    check("含 TOOL_CALL_START", "TOOL_CALL_START" in ts)
    check("含 TOOL_CALL_ARGS", "TOOL_CALL_ARGS" in ts)
    check("含 TOOL_CALL_END", "TOOL_CALL_END" in ts)
    check("含 TOOL_CALL_RESULT", "TOOL_CALL_RESULT" in ts)
    check("含 TEXT_MESSAGE_END", "TEXT_MESSAGE_END" in ts)

    starts = [e for e in evs if e["type"] == "TOOL_CALL_START"]
    results = [e for e in evs if e["type"] == "TOOL_CALL_RESULT"]
    check("工具调用与结果成对", len(starts) == len(results) == 1 and starts and results,
          f"{len(starts)} start / {len(results)} result")
    if starts and results:
        check("toolCallId 格式含轮次", starts[0]["toolCallId"] == "tc_t_add_r0_0",
              starts[0]["toolCallId"])
        check("START/RESULT 的 toolCallId 一致",
              starts[0]["toolCallId"] == results[0]["toolCallId"])
        check("调用的工具名是 add", starts[0]["toolCallName"] == "add",
              starts[0]["toolCallName"])
        args_ev = next((e for e in evs if e["type"] == "TOOL_CALL_ARGS"), None)
        parsed = None
        if args_ev:
            try:
                parsed = json.loads(args_ev["delta"])
            except Exception:  # noqa: BLE001
                parsed = None
        check("TOOL_CALL_ARGS.delta 是合法 JSON 对象",
              isinstance(parsed, dict), str(args_ev["delta"]) if args_ev else "缺失")
        check("工具参数含任务文本", parsed and "3" in str(parsed.values()), str(parsed))
        check("add 工具真实返回 3+5=8", "8" in results[0]["content"], results[0]["content"])
    check("模型最终收工（未撞上限）", "达到最大迭代轮数" not in out, out[:60])
    check("最终输出含工具结果", "8" in out or "工具返回" in out, out[:60])

    # 多轮 toolCallId 唯一性（同节点内不会撞 Rust 侧 tool_map 的 key）
    print("\n[3] 每次工具调用的 toolCallId 唯一")
    start_ids = [e["toolCallId"] for e in evs if e["type"] == "TOOL_CALL_START"]
    check("TOOL_CALL_START 的 id 不重复", len(start_ids) == len(set(start_ids)), f"{start_ids}")
    check("同一调用的 START/RESULT 共享 id 且 ARGS/END 同 id",
          all(e["toolCallId"] == start_ids[0] for e in evs if "toolCallId" in e),
          str(ids_preview(evs)))

    # ----------------------------------------- 4. 未知工具 / 无匹配工具的鲁棒性
    print("\n[4] 鲁棒性")
    q2: asyncio.Queue = asyncio.Queue()
    out2 = await S._react_tool_node({}, "随便说点什么无关紧要的话", {}, q2, "t_echo", tools)
    evs2 = drain(q2)
    check("无关输入也能收工", "达到最大迭代轮数" not in out2, out2[:60])
    check("无关输入仍有工具调用", "TOOL_CALL_START" in types_of(evs2))

    q3: asyncio.Queue = asyncio.Queue()
    out3 = await S._react_tool_node({}, "计算 1+1", {}, q3, "t_none", [])
    evs3 = drain(q3)
    check("空工具列表不崩溃", isinstance(out3, str), out3[:50])
    check("空工具列表无工具调用事件", "TOOL_CALL_START" not in types_of(evs3))
    check("空工具列表提示无工具", "没有可用工具" in out3 or "未产生输出" in out3, out3[:50])

    # ------------------------------------------------------ 5. 迭代上限
    print("\n[5] max_iterations 硬上限")
    q4: asyncio.Queue = asyncio.Queue()
    out4 = await S._react_tool_node({"max_iterations": 1}, "计算 9 减 4", {}, q4, "t_cap", tools)
    evs4 = drain(q4)
    n4 = len([e for e in evs4 if e["type"] == "TOOL_CALL_START"])
    check("max_iterations=1 时只跑一轮", n4 == 1, f"{n4} 次工具调用")
    check("撞上限时输出带明确标记", "达到最大迭代轮数 1" in out4, out4[:70])

    q5: asyncio.Queue = asyncio.Queue()
    out5 = await S._react_tool_node({"max_iterations": 0}, "计算 2+2", {}, q5, "t_zero", tools)
    check("max_iterations=0 被兜底为 1", "达到最大迭代轮数 1" in out5, out5[:70])

    # ------------------------------------------- 6. 旧路径（direct）未被破坏
    print("\n[6] 兼容旧行为：data.mcp_tool 单工具直调")
    q6: asyncio.Queue = asyncio.Queue()
    add_tool = tools_map.get("demo__add")
    out6 = await S._tool_node({"tool": "add"}, "计算 20 加 22", q6, "t_direct", add_tool)
    evs6 = drain(q6)
    check("direct 模式仍产生完整 TOOL_CALL_*",
          all(t in types_of(evs6) for t in
              ["TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT"]),
          str(types_of(evs6)))
    check("direct 模式 toolCallId 保持旧格式 tc_<nid>",
          evs6[0]["toolCallId"] == "tc_t_direct", evs6[0]["toolCallId"])
    check("direct 模式结果正确", "42" in out6, out6)

    # ------------------------------ 7. 整图执行（覆盖 make_node 的模式派发分支）
    print("\n[7] 整图执行 start -> tool(ReAct) -> end")
    wf = {
        "nodes": [
            {"id": "n_start", "type": "start", "data": {"input": "计算 123 加 456"}},
            {"id": "n_react", "type": "tool",
             "data": {"tools": ["*"], "max_iterations": 3}},
            {"id": "n_end", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n_start", "target": "n_react"},
            {"id": "e2", "source": "n_react", "target": "n_end"},
        ],
    }
    q7: asyncio.Queue = asyncio.Queue()
    await S._run_graph(wf, "", {}, q7, "th_x", "run_x", None)
    evs7 = drain(q7)
    ts7 = types_of(evs7)
    steps7 = [e["stepName"] for e in evs7 if e["type"] == "STEP_STARTED"]

    check("首个事件是 RUN_STARTED", ts7[0] == "RUN_STARTED", ts7[0])
    check("有 RUN_FINISHED", "RUN_FINISHED" in ts7)
    check("图执行无 RUN_ERROR", "RUN_ERROR" not in ts7,
          next((e.get("message", "")[:80] for e in evs7 if e["type"] == "RUN_ERROR"), ""))
    check("三个节点各发一次 STEP_STARTED", steps7 == ["n_start", "n_react", "n_end"], str(steps7))
    check("ReAct 循环不下沉到图层面（tool 节点只出现一次）",
          steps7.count("n_react") == 1)
    check("整图流中工具调用事件齐全",
          "TOOL_CALL_START" in ts7 and "TOOL_CALL_RESULT" in ts7)
    check("toolCallId 带节点名与轮次",
          all(i.startswith("tc_n_react_r") for i in
              [e["toolCallId"] for e in evs7 if e["type"] == "TOOL_CALL_START"]))
    fin = next((e for e in evs7 if e["type"] == "RUN_FINISHED"), {})
    check("最终结果含 579", "579" in (fin.get("result") or ""), (fin.get("result") or "")[:60])

    # ------------------------------------------- 8. 整图兼容旧路径 + 白名单兜底
    print("\n[8] 整图兼容：mcp_tool 直调 / 白名单不匹配兜底")
    wf_direct = {
        "nodes": [
            {"id": "n_start", "type": "start", "data": {"input": "计算 7 加 8"}},
            {"id": "n_tool", "type": "tool", "data": {"mcp_tool": "demo__add"}},
            {"id": "n_end", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n_start", "target": "n_tool"},
            {"id": "e2", "source": "n_tool", "target": "n_end"},
        ],
    }
    q8: asyncio.Queue = asyncio.Queue()
    await S._run_graph(wf_direct, "", {}, q8, "th_d", "run_d", None)
    evs8 = drain(q8)
    check("旧 mcp_tool 图无错误", "RUN_ERROR" not in types_of(evs8))
    check("旧 mcp_tool 图工具事件完整",
          "TOOL_CALL_START" in types_of(evs8) and "TOOL_CALL_RESULT" in types_of(evs8))
    fin8 = next((e for e in evs8 if e["type"] == "RUN_FINISHED"), {})
    check("旧 mcp_tool 图结果含 15", "15" in (fin8.get("result") or ""),
          (fin8.get("result") or "")[:60])

    wf_bad = {
        "nodes": [
            {"id": "n_start", "type": "start", "data": {"input": "计算 1 加 2"}},
            {"id": "n_tool", "type": "tool", "data": {"tools": ["nope__nope"]}},
            {"id": "n_end", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n_start", "target": "n_tool"},
            {"id": "e2", "source": "n_tool", "target": "n_end"},
        ],
    }
    q9: asyncio.Queue = asyncio.Queue()
    await S._run_graph(wf_bad, "", {}, q9, "th_b", "run_b", None)
    evs9 = drain(q9)
    check("白名单全不匹配时不崩溃", "RUN_ERROR" not in types_of(evs9),
          next((e.get("message", "")[:60] for e in evs9 if e["type"] == "RUN_ERROR"), ""))
    check("白名单全不匹配时仍走完图", "RUN_FINISHED" in types_of(evs9))

    # ------------------------------------------------ 9. 多意图任务 -> 多轮循环
    print("\n[9] 多意图任务驱动多轮循环（这才是「循环」被用起来的场景）")
    q10: asyncio.Queue = asyncio.Queue()
    out10 = await S._react_tool_node(
        {"max_iterations": 5}, "现在几点？另外帮我算 88 加 12", {}, q10, "t_multi", tools
    )
    evs10 = drain(q10)
    starts10 = [e for e in evs10 if e["type"] == "TOOL_CALL_START"]
    names10 = [e["toolCallName"] for e in starts10]
    rounds10 = sorted({
        e["toolCallId"].split("_r")[-1].split("_")[0] for e in starts10
    })
    check("多意图触发两次工具调用", len(names10) == 2, str(names10))
    check("两次调用落在不同轮次", len(rounds10) == 2, str(rounds10))
    check("工具不重复调用", len(set(names10)) == len(names10), str(names10))
    check("命中 add 与 get_time", set(names10) == {"add", "get_time"}, str(names10))
    check("每次调用的 toolCallId 唯一",
          len({e["toolCallId"] for e in starts10}) == 2, str(rounds10))
    check("多轮后正常收工（未撞上限）", "达到最大迭代轮数" not in out10, out10[:70])
    check("最终输出汇总了两条工具结果",
          "88 + 12" in out10 and "T" in out10, out10[:80])

    # ---------------------------------------------------------------- 汇总
    print(f"\n{'='*60}")
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("失败用例：")
        for f in FAIL:
            print(f"  - {f}")
    print(f"{'='*60}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
