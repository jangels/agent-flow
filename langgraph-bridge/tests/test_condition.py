"""条件分支（S2）自测：候选菜单、模型判定、环检测与显式执行上限。

运行：
    cd langgraph-bridge && uv run python tests/test_condition.py

覆盖：
  1. 候选菜单构造（出边 -> 编号候选）：顺序、去重、悬空边过滤、可读名优先级
  2. choose_branch 工具的 JSON Schema（封闭菜单）
  3. MockRouterModel 判定规则（命中 / 未命中 / 无判据退回旧词表）
  4. _route_index 容错解析（字符串编号 / 越界 / 无工具调用 / 完全无法解析）
  5. _find_cycle（无环 / 直接环 / 自环 / 大环）
  6. _resolve_recursion_limit（优先级、非法值回退、有环收紧）
  7. _condition_node（无候选 / 单候选不调模型 / 双候选真的判定）
  8. 整图端到端：分支只走一条、事件带 branch*、RUN_STARTED 带 recursionLimit/hasCycle
  9. 整图含环：撞显式上限即报错（不再挂到 10007 步）
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as S  # noqa: E402

PASS, FAIL = [], []


def _tid() -> str:
    """每个整图用例独占一个 thread_id —— 加了 checkpointer 后同 thread 的 state
    会跨 run 累积（checkpoint 持久化），固定 thread_id 会让后面的用例吃到
    前面用例的旧输出（症状：condition 输入拼进了上一用例的文本，误判分支）。"""
    return f"th-cond-{uuid.uuid4().hex[:8]}"


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


def steps(events: list[dict]) -> list[str]:
    return [e["stepName"] for e in events if e["type"] == "STEP_STARTED"]


# --------------------------------------------------------------------------
# 图构造小工具
# --------------------------------------------------------------------------

def node(nid: str, ntype: str, **data) -> dict:
    return {"id": nid, "type": ntype, "data": data}


def edge(src: str, dst: str, **kw) -> dict:
    return {"id": f"e_{src}_{dst}", "source": src, "target": dst, **kw}


def branch_graph(expression: str, input_text: str, labels=("yes", "no")) -> dict:
    """start -> condition ⇉ (t_yes | t_false) -> end 的经典二分支图。"""
    return {
        "nodes": [
            node("n_start", "start", input=input_text),
            node("n_cond", "condition", expression=expression),
            node("t_yes", "llm", prompt="命中分支"),
            node("t_false", "llm", prompt="未命中分支"),
            node("n_end", "end"),
        ],
        "edges": [
            edge("n_start", "n_cond"),
            edge("n_cond", "t_yes", label=labels[0]),
            edge("n_cond", "t_false", label=labels[1]),
            edge("t_yes", "n_end"),
            edge("t_false", "n_end"),
        ],
    }


async def main() -> int:  # noqa: C901
    # ---------------------------------------------------------- 1. 候选菜单
    print("\n[1] 候选菜单构造 _candidates_of")
    nodes = {
        "n_cond": node("n_cond", "condition", expression="x"),
        "a": node("a", "llm", label="天气分支"),
        "b": node("b", "llm"),
        "c": node("c", "end"),
    }
    edges = [
        edge("n_cond", "a", label="查天气"),
        edge("n_cond", "b"),                     # 无边 label -> 用目标节点 label / id
        edge("n_cond", "c", sourceHandle="false"),
        edge("n_cond", "a", label="重复边"),      # 同 target 去重
        edge("n_cond", "ghost"),                 # 悬空边 -> 过滤
        edge("other", "a"),                      # 非本节点出边 -> 忽略
    ]
    cands = S._candidates_of("n_cond", edges, nodes)
    check("候选数与连线顺序一致（去重 + 过滤悬空）", [c["target"] for c in cands] == ["a", "b", "c"],
          str([c["target"] for c in cands]))
    check("edge.label 优先作为可读名", cands[0]["label"] == "查天气", cands[0]["label"])
    check("无 label 时退回目标节点 label", cands[1]["label"] == "b", cands[1]["label"])
    check("再退回 sourceHandle", cands[2]["label"] == "false", cands[2]["label"])
    check("无出边 -> 空候选", S._candidates_of("n_start", edges, nodes) == [])

    # -------------------------------------------------- 2. choose_branch schema
    print("\n[2] choose_branch 工具 schema（封闭菜单）")
    tool = S._route_tool(cands)
    props = tool["parameters"]["properties"]
    check("工具名固定", tool["name"] == "choose_branch")
    check("index 必填", tool["parameters"]["required"] == ["index"])
    check("菜单写进 description（编号 -> 语义）",
          "0=查天气" in tool["description"] and "1=b" in tool["description"],
          tool["description"])
    check("index 描述带合法范围", "0~2" in props["index"]["description"], props["index"]["description"])

    # ------------------------------------------------------- 3. Mock 判定规则
    print("\n[3] MockRouterModel 判定规则")
    from langchain_core.messages import HumanMessage

    def mock_route(expression: str, task: str, n: int = 2) -> int:
        m = S.MockRouterModel(n, expression)
        msg = m.invoke([HumanMessage(content=task)])
        return msg.tool_calls[0]["args"]["index"]

    check("判据 2-gram 命中 -> 第 0 个候选",
          mock_route("用户在询问天气", "今天北京天气怎么样") == 0)
    check("判据未命中 -> 最后一个候选",
          mock_route("用户在询问天气", "帮我重构这段代码") == 1)
    check("虚词组合不算命中（是否/用户/内容）",
          mock_route("是否包含用户内容", "随便写点什么") == 1)
    check("英文判据整词命中", mock_route("asking about weather", "what is the weather") == 0)
    check("无判据 -> 退回旧词表（命中 是）", mock_route("", "是的，请继续") == 0)
    check("无判据 -> 退回旧词表（未命中）", mock_route("", "帮我重构这段代码") == 1)
    check("三候选时未命中落在最后一个", mock_route("天气", "帮我重构代码", n=3) == 2)

    # ------------------------------------------------------- 4. _route_index
    print("\n[4] _route_index 容错解析")
    calls = [{"name": "choose_branch", "args": {"index": 1, "reason": "因为"}}]
    check("正常解析", S._route_index(calls, 3) == (1, "因为"))
    check("字符串编号", S._route_index([{"name": "choose_branch", "args": {"index": "2"}}], 3)[0] == 2)
    check("越界夹紧到上界", S._route_index([{"name": "choose_branch", "args": {"index": 99}}], 3)[0] == 2)
    check("负数夹紧到下界", S._route_index([{"name": "choose_branch", "args": {"index": -5}}], 3)[0] == 0)
    check("混入文字的编号", S._route_index([{"name": "choose_branch", "args": {"index": "分支1"}}], 3)[0] == 1)
    check("无工具调用时从文本捞数字", S._route_index([], 3, "我选 2")[0] == 2)
    check("彻底无法解析 -> -1", S._route_index([], 3, "无法判断") == (-1, ""))
    check("忽略同名以外的工具", S._route_index([{"name": "other", "args": {"index": 1}}], 3, "")[0] == -1)

    # --------------------------------------------------------- 5. 环检测
    print("\n[5] 环检测 _find_cycle")
    dag = {
        "a": node("a", "start"), "b": node("b", "llm"), "c": node("c", "end"),
    }
    check("DAG -> 无环", S._find_cycle(dag, [edge("a", "b"), edge("b", "c")]) is None)
    cyc = S._find_cycle(dag, [edge("a", "b"), edge("b", "c"), edge("c", "a")])
    check("三节点环被检出（首尾同点）", cyc is not None and cyc[0] == cyc[-1] and len(cyc) == 4, str(cyc))
    self_loop = S._find_cycle(dag, [edge("a", "b"), edge("b", "b")])
    check("自环被检出", self_loop == ["b", "b"], str(self_loop))
    big = {f"n{i}": node(f"n{i}", "llm") for i in range(6)}
    big_edges = [edge(f"n{i}", f"n{i+1}") for i in range(5)] + [edge("n5", "n2")]
    got = S._find_cycle(big, big_edges)
    check("长环被检出", got is not None and set(got) == {"n2", "n3", "n4", "n5"}, str(got))

    # -------------------------------------------------- 6. recursion_limit
    print("\n[6] _resolve_recursion_limit")
    old_env = os.environ.pop("AGENT_FLOW_RECURSION_LIMIT", None)
    try:
        check("无环按规模推算（n=3 -> 25 下限）", S._resolve_recursion_limit({}, 3, False) == 25)
        check("无环大图按 n*4", S._resolve_recursion_limit({}, 20, False) == 80)
        check("有环收紧到 n*2（n=3 -> 12 下限）", S._resolve_recursion_limit({}, 3, True) == 12)
        check("wf.recursion_limit 优先", S._resolve_recursion_limit({"recursion_limit": 7}, 3, True) == 7)
        check("字符串数字可用", S._resolve_recursion_limit({"recursion_limit": "9"}, 3, False) == 9)
        check("非法值回退推算", S._resolve_recursion_limit({"recursion_limit": "abc"}, 3, False) == 25)
        check("0/负数回退推算", S._resolve_recursion_limit({"recursion_limit": 0}, 3, False) == 25)
        os.environ["AGENT_FLOW_RECURSION_LIMIT"] = "15"
        check("环境变量次优先", S._resolve_recursion_limit({}, 3, False) == 15)
        check("wf 覆盖环境变量", S._resolve_recursion_limit({"recursion_limit": 6}, 3, False) == 6)
    finally:
        os.environ.pop("AGENT_FLOW_RECURSION_LIMIT", None)
        if old_env is not None:
            os.environ["AGENT_FLOW_RECURSION_LIMIT"] = old_env

    # ---------------------------------------------------- 7. _condition_node
    print("\n[7] _condition_node 直调")
    q: asyncio.Queue = asyncio.Queue()
    out, br, lbl, why = await S._condition_node({}, "任意内容", {}, q, "c0", [])
    check("无候选 -> 直通且 branch 为空", out == "任意内容" and br == "" and drain(q) == [])

    q = asyncio.Queue()
    one = [{"target": "only", "label": "唯一出口"}]
    out, br, lbl, why = await S._condition_node({}, "任意内容", {}, q, "c1", one)
    check("单候选 -> 直选且不发任何事件", br == "only" and drain(q) == [])

    q = asyncio.Queue()
    out, br, lbl, why = await S._condition_node(
        {"expression": "询问天气"}, "今天北京天气", {}, q, "c2", cands[:2]
    )
    check("双候选命中 -> target=a", br == "a" and lbl == "查天气", f"{br}/{lbl}")
    check("判定不污染 token 流（无边判定的 TEXT_MESSAGE）",
          "TEXT_MESSAGE_CONTENT" not in types_of(drain(q)))

    q = asyncio.Queue()
    out, br, lbl, why = await S._condition_node(
        {"expression": "询问天气"}, "帮我重构代码", {}, q, "c3", cands[:2]
    )
    check("双候选未命中 -> target=b", br == "b", f"{br}/{lbl}")

    # ------------------------------------------------------ 8. 整图端到端
    print("\n[8] 整图端到端（DAG 分支）")
    wf = branch_graph("用户在询问天气", "今天北京天气怎么样")
    q = asyncio.Queue()
    await S._run_graph(wf, "", {}, q, _tid(), "run", None)
    evs = drain(q)
    started = next(e for e in evs if e["type"] == "RUN_STARTED")
    check("RUN_STARTED 带显式 recursionLimit", started.get("recursionLimit") == 25, str(started))
    check("RUN_STARTED 标记无环", started.get("hasCycle") is False, str(started.get("hasCycle")))
    check("命中走 t_yes，且 t_false 未被调度",
          "t_yes" in steps(evs) and "t_false" not in steps(evs), str(steps(evs)))
    cond_ev = next(e for e in evs if e["type"] == "STEP_FINISHED" and e["stepName"] == "n_cond")
    check("STEP_FINISHED 带 branch/branchLabel/branchReason",
          cond_ev.get("branch") == "t_yes" and cond_ev.get("branchLabel") == "yes"
          and bool(cond_ev.get("branchReason")), str(cond_ev))
    check("condition 输出直通上游内容（不改数据）", cond_ev["output"] == "今天北京天气怎么样",
          cond_ev["output"])
    check("流程正常结束", "RUN_FINISHED" in types_of(evs))

    wf2 = branch_graph("用户在询问天气", "帮我重构这段代码")
    q = asyncio.Queue()
    await S._run_graph(wf2, "", {}, q, _tid(), "run", None)
    evs2 = drain(q)
    check("未命中走 t_false", "t_false" in steps(evs2) and "t_yes" not in steps(evs2),
          str(steps(evs2)))

    # 无 expression -> 旧词表行为必须保持
    wf3 = branch_graph("", "是的，请继续")
    q = asyncio.Queue()
    await S._run_graph(wf3, "", {}, q, _tid(), "run", None)
    evs3 = drain(q)
    check("无判据时退回旧词表（命中是 -> 第一分支）", "t_yes" in steps(evs3), str(steps(evs3)))

    # ------------------------------------------------------ 9. 含环图
    print("\n[9] 含环图：撞显式上限即报错（不再挂到 10007 步）")
    loop_wf = {
        "nodes": [
            node("n_start", "start", input="天气"),
            node("n_cond", "condition", expression="询问天气"),
            node("n_end", "end"),
        ],
        "edges": [
            edge("n_start", "n_cond"),
            # 命中判据 -> 回到 start，形成真环（Label 顺序决定 0 号候选就是回边）
            edge("n_cond", "n_start", label="再查一次"),
            edge("n_cond", "n_end", label="结束"),
        ],
    }
    q = asyncio.Queue()
    await S._run_graph(loop_wf, "", {}, q, _tid(), "run", None)
    evs4 = drain(q)
    started4 = next(e for e in evs4 if e["type"] == "RUN_STARTED")
    check("含环图被检出", started4.get("hasCycle") is True, str(started4.get("hasCycle")))
    check("含环图上限收紧到 max(12, n*2)", started4.get("recursionLimit") == 12,
          str(started4.get("recursionLimit")))
    err = [e for e in evs4 if e["type"] == "RUN_ERROR"]
    check("撞上限报 RUN_ERROR（而非无限跑）", len(err) == 1, str(len(err)))
    check("错误信息指出上限与建议", bool(err) and "recursion_limit=12" in err[0]["message"],
          err[0]["message"][:90] if err else "")
    check("实际步数受上限约束（<= 20）", len(steps(evs4)) <= 20, f"{len(steps(evs4))} 步")

    # ---------------------------------------------------------- 汇总
    print(f"\n{'=' * 62}")
    print(f"  PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("  失败用例：")
        for f in FAIL:
            print(f"    - {f}")
    print(f"{'=' * 62}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
