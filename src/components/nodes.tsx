import { useEffect, useState } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { AgentNodeData } from "../types";

const TYPE_META: Record<string, { title: string; color: string }> = {
  start: { title: "▶ 开始", color: "#10b981" },
  llm: { title: "🤖 LLM 调用", color: "#6366f1" },
  condition: { title: "◆ 条件分支", color: "#f59e0b" },
  tool: { title: "🔧 工具调用", color: "#0ea5e9" },
  human: { title: "🙋 人工审批", color: "#8b5cf6" },
  end: { title: "■ 结束", color: "#ef4444" },
};

/** 审批动作的中文标签；未收录的自定义动作直接显示原文。 */
const ACTION_LABEL: Record<string, string> = {
  approve: "批准",
  reject: "驳回",
  escalate: "升级",
};

/** 审批回调：由 App 注入（节点组件拿不到全局执行上下文）。 */
export type ResumeHandler = (nodeId: string, action: string, value: string) => void;

function statusColor(s?: string) {
  switch (s) {
    case "running":
      return "#f59e0b";
    case "awaiting":
      return "#8b5cf6";
    case "done":
      return "#10b981";
    case "error":
      return "#ef4444";
    default:
      return "#94a3b8";
  }
}

function Shell({
  type,
  selected,
  status,
  children,
  hasTarget = true,
  hasSource = true,
}: {
  type: string;
  selected?: boolean;
  status?: string;
  children: React.ReactNode;
  hasTarget?: boolean;
  hasSource?: boolean;
}) {
  const meta = TYPE_META[type] ?? { title: type, color: "#64748b" };
  const awaiting = status === "awaiting";
  return (
    <div
      className={`af-node ${selected ? "sel" : ""} ${awaiting ? "awaiting" : ""}`}
      style={{ borderTopColor: meta.color }}
    >
      <div className="af-node-head">
        <span style={{ color: meta.color, fontWeight: 600 }}>{meta.title}</span>
        <span className="af-node-head-right">
          {awaiting && <span className="af-awaiting-tag">等待审批</span>}
          <span className="af-status-dot" style={{ background: statusColor(status) }} />
        </span>
      </div>
      <div className="af-node-body">{children}</div>
      {hasTarget && <Handle type="target" position={Position.Top} />}
      {hasSource && <Handle type="source" position={Position.Bottom} />}
    </div>
  );
}

export function StartNode({ data, selected }: NodeProps) {
  const d = data as AgentNodeData;
  return (
    <Shell type="start" selected={selected} status={d.status} hasTarget={false}>
      <div className="af-preview">{d.input || "(空输入)"}</div>
    </Shell>
  );
}

export function LLMNode({ data, selected }: NodeProps) {
  const d = data as AgentNodeData;
  return (
    <Shell type="llm" selected={selected} status={d.status}>
      <div className="af-preview">{d.prompt || "(未配置提示词)"}</div>
      {d.output && <div className="af-output">{d.output}</div>}
    </Shell>
  );
}

export function ConditionNode({ data, selected }: NodeProps) {
  const d = data as AgentNodeData;
  return (
    <Shell type="condition" selected={selected} status={d.status}>
      <div className="af-preview">{d.expression || "(未配置判据)"}</div>
      {d.branch && (
        <div className="af-branch">
          → {d.branch}
          {d.branchReason ? <span className="af-branch-reason"> · {d.branchReason}</span> : null}
        </div>
      )}
    </Shell>
  );
}

export function ToolNode({ data, selected }: NodeProps) {
  const d = data as AgentNodeData;
  const tools = d.tools ?? [];
  const isReact = tools.length > 0;
  const summary = tools.includes("*") ? "全部工具" : `${tools.length} 个工具`;
  return (
    <Shell type="tool" selected={selected} status={d.status}>
      {isReact ? (
        <div className="af-preview">
          ⟳ ReAct · {summary} · ≤{d.max_iterations ?? 6} 轮
        </div>
      ) : (
        <div className="af-preview">工具: {d.mcp_tool || d.tool || "unknown"}</div>
      )}
      {d.output && <div className="af-output">{d.output}</div>}
    </Shell>
  );
}

/**
 * 人工审批节点（HITL）。
 *
 * 运行期有两个截然不同的形态：
 *   - 挂起中（status === "awaiting"）：内联渲染审批控件 —— 可编辑的待审内容 +
 *     每个候选动作一个按钮。点按钮即把决策回传，图从断点续跑。
 *   - 其他：只展示「会问什么」和已提交的决策，避免误触。
 *
 * 这里刻意把审批做在**节点卡片内**而不是弹窗：审批的对象是流程里的具体一环，
 * 贴着它呈现比浮层更不容易搞错上下文；也复用了既有的「运行期状态回显」通路。
 */
export function HumanNode({
  id,
  data,
  selected,
  onResume,
}: NodeProps & { onResume?: ResumeHandler }) {
  const d = data as AgentNodeData;
  const actions = d.actions && d.actions.length > 0 ? d.actions : ["approve", "reject"];
  const awaiting = d.status === "awaiting" || d.awaiting === true;
  const allowEdit = d.allow_edit !== false;
  // 草稿：人可以在待审内容基础上直接改，改完再批
  const [draft, setDraft] = useState("");

  useEffect(() => {
    if (awaiting) setDraft(d.hitlInput ?? "");
  }, [awaiting, d.hitlInput]);

  return (
    <Shell type="human" selected={selected} status={d.status}>
      <div className="af-preview">{d.hitlPrompt || "请确认是否继续"}</div>

      {awaiting ? (
        <div className="af-hitl">
          {allowEdit ? (
            <textarea
              className="af-hitl-input nodrag nowheel"
              rows={3}
              value={draft}
              placeholder="待审内容（可直接修改后再批准）"
              onChange={(e) => setDraft(e.target.value)}
            />
          ) : (
            <div className="af-hitl-readonly">{d.hitlInput || "(无待审内容)"}</div>
          )}
          <div className="af-hitl-actions">
            {actions.map((a) => (
              <button
                key={a}
                className={`af-hitl-btn nodrag ${a}`}
                onClick={(e) => {
                  e.stopPropagation(); // 别让点击冒泡成「选中节点」
                  onResume?.(id, a, allowEdit ? draft : "");
                }}
              >
                {ACTION_LABEL[a] ?? a}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <>
          <div className="af-hitl-actions">
            {actions.map((a) => (
              <span key={a} className="af-hitl-chip">
                {ACTION_LABEL[a] ?? a}
              </span>
            ))}
          </div>
          {d.hitlDecision && (
            <div className="af-branch">
              已{ACTION_LABEL[d.hitlDecision] ?? d.hitlDecision}
              {d.hitlNote ? <span className="af-branch-reason"> · {d.hitlNote}</span> : null}
            </div>
          )}
          {d.output && <div className="af-output">{d.output}</div>}
        </>
      )}
    </Shell>
  );
}

export function EndNode({ data, selected }: NodeProps) {
  const d = data as AgentNodeData;
  return (
    <Shell type="end" selected={selected} status={d.status} hasSource={false}>
      <div className="af-preview">{d.output ? d.output : "(等待输入)"}</div>
    </Shell>
  );
}

export const nodeTypes = {
  start: StartNode,
  llm: LLMNode,
  condition: ConditionNode,
  tool: ToolNode,
  human: HumanNode,
  end: EndNode,
};
