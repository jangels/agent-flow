"""HITL（人工审批节点）自测：挂起 / 续跑 / 决策语义 / 线程隔离。

运行：
    cd langgraph-bridge && uv run python tests/test_hitl.py

覆盖：
  1. format_decision 的决策语义（approve / reject / 自定义动作 / 边界）
  2. _interrupt_of 的边界（无挂起、空列表、非 dict）
  3. 挂起：RUN_PAUSED 字段完整 + RUN_FINISHED(paused) + 后续节点未执行
  4. 续跑 approve：跑完全程，且**不重跑已执行过的 start**（checkpoint 生效的证据）
  5. 续跑 approve 带人工编辑值
  6. 续跑 reject：输出驳回标记，下游可据此分流
  7. actions / allow_edit 可配
  8. thread 隔离：两条挂起线程互不串台
  9. 对没有挂起点的 thread 续跑 -> 可读的 RUN_ERROR（而非 LangGraph 内部错）
 10. 多个审批节点串联（连续两次挂起 / 两次续跑）
 11. checkpointer 落 SQLite 且写在临时目录（不污染用户数据）

注意：checkpointer 是【进程级单例】，所以临时数据目录必须在 import server 之前设好。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 隔离测试数据：绝不写用户的 ~/.agent-flow
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="af-hitl-test-"))
os.environ["AGENT_FLOW_CHECKPOINT_DB"] = str(_TMP / "checkpoints.db")
os.environ["AGENT_FLOW_HOME"] = str(_TMP)
os.environ["AGENT_FLOW_DB"] = str(_TMP / "agent_flow.db")

import server as S  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(
        f"  [{'PASS' if cond else 'FAIL'}] {name}"
        + (f"  -> {detail}" if detail else "")
    )


def drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        item = q.get_nowait()
        if item is not None:
            out.append(item)
    return out


def ev(events: list[dict], etype: str) -> list[dict]:
    return [e for e in events if e.get("type") == etype]


def started(events: list[dict]) -> list[str]:
    return [e.get("stepName") for e in ev(events, "STEP_STARTED")]


def finished(events: list[dict], node: str) -> dict | None:
    for e in ev(events, "STEP_FINISHED"):
        if e.get("stepName") == node:
            return e
    return None


def make_wf(
    prompt: str = "批准吗？",
    input_text: str = "原始内容",
    actions=None,
    allow_edit=None,
) -> dict:
    data: dict = {"prompt": prompt}
    if actions is not None:
        data["actions"] = actions
    if allow_edit is not None:
        data["allow_edit"] = allow_edit
    return {
        "nodes": [
            {"id": "n_start", "type": "start", "data": {"input": input_text}},
            {"id": "n_human", "type": "human", "data": data},
            {"id": "n_end", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n_start", "target": "n_human"},
            {"id": "e2", "source": "n_human", "target": "n_end"},
        ],
    }


async def drive(
    wf: dict, thread_id: str | None = None, decision=None
) -> tuple[list[dict], str]:
    """驱动一次 run（decision=None）或 resume，返回 (事件列表, thread_id)。

    mcp_servers 传 [] —— 显式不要工具（审批流程不需要），同时避免每次 spawn demo server。
    """
    q: asyncio.Queue = asyncio.Queue()
    tid = thread_id or f"t-{uuid.uuid4().hex[:8]}"
    rid = f"run-{uuid.uuid4().hex[:8]}"
    if decision is None:
        await S._run_graph(wf, "", {}, q, tid, rid, [])
    else:
        await S._resume_graph(wf, {}, q, tid, rid, decision, [])
    return drain(q), tid


async def main() -> int:
    print(f"\n临时数据目录: {_TMP}")
    await S.get_checkpointer()
    print(f"checkpointer : {S.checkpointer_kind()}")

    # ------------------------------------------------------------ 1. 决策语义
    print("\n[1] format_decision 决策语义")
    check(
        "approve 带值 -> 用人工值",
        S.format_decision({"decision": "approve", "value": "人工改过"}, "原始")
        == "人工改过",
    )
    check(
        "approve 空值 -> 沿用上游（纯放行）",
        S.format_decision({"decision": "approve"}, "原始") == "原始",
    )
    check(
        "approve 值为空白 -> 沿用上游",
        S.format_decision({"decision": "approve", "value": "   "}, "原始") == "原始",
    )
    check(
        "reject -> 加驳回标记",
        S.format_decision({"decision": "reject"}, "原始") == "[已驳回]",
    )
    check(
        "reject 带 note -> 标记含理由",
        "预算超支"
        in S.format_decision({"decision": "reject", "note": "预算超支"}, "原始"),
    )
    check(
        "裸字符串 'reject' -> 同样处理",
        S.format_decision("reject", "原始") == "[已驳回]",
    )
    check(
        "大写 APPROVE 归一化",
        S.format_decision({"decision": "APPROVE", "value": "x"}, "原始") == "x",
    )
    check(
        "自定义 action -> 带动作名",
        S.format_decision({"action": "escalate", "value": "给上级"}, "原始")
        == "[escalate] 给上级",
    )
    check("None -> 视作放行（上游直通）", S.format_decision(None, "原始") == "原始")

    # ------------------------------------------------------------ 2. 边界
    print("\n[2] _interrupt_of 边界")
    check("无 __interrupt__ 键 -> None", S._interrupt_of({"outputs": []}) is None)
    check("非 dict -> None", S._interrupt_of(None) is None)
    check("空列表 -> None", S._interrupt_of({"__interrupt__": []}) is None)

    # ------------------------------------------------------------ 3. 挂起
    print("\n[3] human 节点挂起")
    wf3 = make_wf()
    e3, tid3 = await drive(wf3)
    p3 = ev(e3, "RUN_PAUSED")
    f3 = ev(e3, "RUN_FINISHED")
    check("发出 RUN_PAUSED", len(p3) == 1, str(len(p3)))
    p = p3[0] if p3 else {}
    check("RUN_PAUSED 带 threadId", p.get("threadId") == tid3, str(p.get("threadId")))
    check(
        "RUN_PAUSED.stepName = 审批节点",
        p.get("stepName") == "n_human",
        str(p.get("stepName")),
    )
    check("RUN_PAUSED 带 prompt", p.get("prompt") == "批准吗？", str(p.get("prompt")))
    check("RUN_PAUSED 带待审内容", p.get("input") == "原始内容", str(p.get("input")))
    check(
        "RUN_PAUSED 带默认 actions",
        p.get("actions") == ["approve", "reject"],
        str(p.get("actions")),
    )
    check("RUN_PAUSED 带 allowEdit 默认 true", p.get("allowEdit") is True)
    check(
        "RUN_FINISHED 标记 paused",
        len(f3) == 1 and f3[0].get("paused") is True,
        str(f3),
    )
    check(
        "挂起后的节点未执行（n_end 不出现）",
        "n_end" not in started(e3),
        str(started(e3)),
    )

    # ------------------------------------------------------------ 4. resume approve
    print("\n[4] resume · approve（纯放行）")
    e4, _ = await drive(wf3, thread_id=tid3, decision={"decision": "approve"})
    check("续跑不再挂起", not ev(e4, "RUN_PAUSED"))
    check("续跑发出 RUN_FINISHED", len(ev(e4, "RUN_FINISHED")) == 1)
    check(
        "续跑结束无 paused 标记",
        ev(e4, "RUN_FINISHED")[0].get("paused") in (None, False),
        str(ev(e4, "RUN_FINISHED")[0].get("paused")),
    )
    check(
        "★ 不重跑 start（checkpoint 真的生效）",
        "n_start" not in started(e4),
        str(started(e4)),
    )
    check(
        "续跑执行了 human 与 end", started(e4) == ["n_human", "n_end"], str(started(e4))
    )
    h4 = finished(e4, "n_human")
    check(
        "human 输出 = 上游（纯放行）",
        bool(h4) and h4.get("output") == "原始内容",
        str(h4.get("output") if h4 else None),
    )
    check(
        "最终结果透传到 end",
        ev(e4, "RUN_FINISHED")[0].get("result") == "原始内容",
        str(ev(e4, "RUN_FINISHED")[0].get("result")),
    )

    # ------------------------------------------------------------ 5. resume 带编辑值
    print("\n[5] resume · approve 并编辑内容")
    wf5 = make_wf()
    _e5a, tid5 = await drive(wf5)
    e5, _ = await drive(
        wf5,
        thread_id=tid5,
        decision={"decision": "approve", "value": "人工批准后的新内容"},
    )
    h5 = finished(e5, "n_human")
    check(
        "human 输出 = 人工编辑值",
        bool(h5) and h5.get("output") == "人工批准后的新内容",
        str(h5.get("output") if h5 else None),
    )
    check(
        "最终结果 = 人工编辑值",
        ev(e5, "RUN_FINISHED")[0].get("result") == "人工批准后的新内容",
    )

    # ------------------------------------------------------------ 6. resume reject
    print("\n[6] resume · reject")
    wf6 = make_wf()
    _e6a, tid6 = await drive(wf6)
    e6, _ = await drive(
        wf6, thread_id=tid6, decision={"decision": "reject", "note": "预算超支"}
    )
    h6 = finished(e6, "n_human")
    out6 = (h6.get("output") if h6 else "") or ""
    check("驳回输出带标记", out6.startswith("[已驳回]"), str(out6))
    check("驳回输出带理由", "预算超支" in out6, str(out6))
    check(
        "驳回也让流程收尾（下游 condition 可据此分流）",
        len(ev(e6, "RUN_FINISHED")) == 1,
    )

    # ------------------------------------------------------------ 7. actions / allow_edit
    print("\n[7] actions / allow_edit 可配")
    wf7 = make_wf(actions=["approve", "reject", "escalate"], allow_edit=False)
    e7, tid7 = await drive(wf7)
    p7 = (ev(e7, "RUN_PAUSED") or [{}])[0]
    check(
        "自定义 actions 透传",
        p7.get("actions") == ["approve", "reject", "escalate"],
        str(p7.get("actions")),
    )
    check(
        "allow_edit=False 透传", p7.get("allowEdit") is False, str(p7.get("allowEdit"))
    )
    e7b, _ = await drive(
        wf7, thread_id=tid7, decision={"decision": "escalate", "value": "升级处理"}
    )
    h7 = finished(e7b, "n_human")
    check(
        "自定义动作的输出格式",
        bool(h7) and (h7.get("output") or "").startswith("[escalate]"),
        str(h7.get("output") if h7 else None),
    )

    # ------------------------------------------------------------ 8. thread 隔离
    print("\n[8] thread 隔离")
    wfa, wfb = make_wf(), make_wf()
    _ea0, ta = await drive(wfa)
    _eb0, tb = await drive(wfb)
    check("两条线程 id 不同", ta != tb, f"{ta} / {tb}")
    ea, _ = await drive(
        wfa, thread_id=ta, decision={"decision": "approve", "value": "A 的批准"}
    )
    eb, _ = await drive(
        wfb, thread_id=tb, decision={"decision": "approve", "value": "B 的批准"}
    )
    check(
        "A 的结果是 A 的决策",
        ev(ea, "RUN_FINISHED")[0].get("result") == "A 的批准",
        str(ev(ea, "RUN_FINISHED")[0].get("result")),
    )
    check(
        "B 的结果是 B 的决策",
        ev(eb, "RUN_FINISHED")[0].get("result") == "B 的批准",
        str(ev(eb, "RUN_FINISHED")[0].get("result")),
    )

    # ------------------------------------------------------------ 9. 无挂起点续跑
    print("\n[9] 对没有挂起点的 thread 续跑")
    wf9 = make_wf()
    _e9a, tid9 = await drive(wf9)
    await drive(wf9, thread_id=tid9, decision={"decision": "approve"})  # 跑完
    e9, _ = await drive(
        wf9, thread_id=tid9, decision={"decision": "approve"}
    )  # 再续一次
    errs9 = ev(e9, "RUN_ERROR")
    check("已跑完的 thread 再续跑 -> RUN_ERROR", len(errs9) == 1, str(len(errs9)))
    check(
        "错误信息可读（含「没有等待中的挂起点」）",
        bool(errs9) and "没有等待中的挂起点" in errs9[0].get("message", ""),
        (errs9[0].get("message", "")[:90] if errs9 else ""),
    )

    # ------------------------------------------------------------ 10. 多审批串联
    print("\n[10] 多个审批节点串联（连续两次挂起）")
    wf10 = {
        "nodes": [
            {"id": "n_start", "type": "start", "data": {"input": "内容"}},
            {"id": "h1", "type": "human", "data": {"prompt": "一级审批"}},
            {"id": "h2", "type": "human", "data": {"prompt": "二级审批"}},
            {"id": "n_end", "type": "end", "data": {}},
        ],
        "edges": [
            {"id": "e1", "source": "n_start", "target": "h1"},
            {"id": "e2", "source": "h1", "target": "h2"},
            {"id": "e3", "source": "h2", "target": "n_end"},
        ],
    }
    eA, t10 = await drive(wf10)
    pA = (ev(eA, "RUN_PAUSED") or [{}])[0]
    check("第 1 次挂在 h1", pA.get("stepName") == "h1", str(pA.get("stepName")))
    eB, _ = await drive(
        wf10, thread_id=t10, decision={"decision": "approve", "value": "一级通过"}
    )
    pB = (ev(eB, "RUN_PAUSED") or [{}])[0]
    check(
        "第 2 次挂在 h2（连续挂起）",
        pB.get("stepName") == "h2",
        str(pB.get("stepName")),
    )
    check("二级审批看到一级的产物", pB.get("input") == "一级通过", str(pB.get("input")))
    check("二级挂起时 end 仍未执行", "n_end" not in started(eB), str(started(eB)))
    eC, _ = await drive(
        wf10, thread_id=t10, decision={"decision": "approve", "value": "二级通过"}
    )
    check(
        "两次通过后跑完全程",
        not ev(eC, "RUN_PAUSED")
        and ev(eC, "RUN_FINISHED")[0].get("result") == "二级通过",
        str(ev(eC, "RUN_FINISHED")[0].get("result")) if ev(eC, "RUN_FINISHED") else "",
    )
    check(
        "全程 start 只执行过一次", started(eA).count("n_start") == 1, str(started(eA))
    )

    # ------------------------------------------------------------ 11. checkpointer
    print("\n[11] checkpointer 持久化")
    check(
        "使用 SQLite checkpointer（跨进程存活）",
        (S.checkpointer_kind() or "").startswith("sqlite"),
        str(S.checkpointer_kind()),
    )
    check(
        "checkpoint 文件已落盘",
        S.checkpoint_db_path().exists(),
        str(S.checkpoint_db_path()),
    )
    check(
        "写在临时目录（不污染用户数据）",
        str(S.checkpoint_db_path()).startswith(str(_TMP)),
    )

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    # 必须显式归还 SQLite 连接：aiosqlite 的工作线程不是 daemon，
    # 连接不关的话测试全部 PASS 了进程也不会退出（卡在 asyncio.run 收尾）。
    await S.close_checkpointer()
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
