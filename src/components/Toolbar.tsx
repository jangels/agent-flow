import type { NodeType } from "../types";

const PALETTE: { type: NodeType; label: string; color: string }[] = [
  { type: "start", label: "开始", color: "#10b981" },
  { type: "llm", label: "LLM 调用", color: "#6366f1" },
  { type: "condition", label: "条件分支", color: "#f59e0b" },
  { type: "tool", label: "工具调用", color: "#0ea5e9" },
  { type: "human", label: "人工审批", color: "#8b5cf6" },
  { type: "end", label: "结束", color: "#ef4444" },
];

interface Props {
  onAddNode: (t: NodeType) => void;
  onRun: () => void;
  onSave: () => void;
  onPublish: () => void;
  onExport: () => void;
  onOpenSettings: () => void;
  running: boolean;
  published: boolean;
}

export function Toolbar({
  onAddNode,
  onRun,
  onSave,
  onPublish,
  onExport,
  onOpenSettings,
  running,
  published,
}: Props) {
  return (
    <div className="af-toolbar">
      <div className="af-brand">AgentFlow</div>
      <div className="af-palette">
        {PALETTE.map((p) => (
          <button
            key={p.type}
            className="af-chip"
            style={{ borderColor: p.color, color: p.color }}
            onClick={() => onAddNode(p.type)}
            disabled={running}
          >
            + {p.label}
          </button>
        ))}
      </div>
      <div className="af-actions">
        <button className="af-btn primary" onClick={onRun} disabled={running}>
          {running ? "运行中…" : "▶ 运行"}
        </button>
        <button className="af-btn" onClick={onSave} disabled={running}>
          保存
        </button>
        <button className="af-btn" onClick={onPublish} disabled={running}>
          {published ? "取消发布" : "发布"}
        </button>
        <button className="af-btn" onClick={onExport} disabled={running}>
          导出
        </button>
        <button className="af-btn ghost" onClick={onOpenSettings}>
          ⚙ 设置
        </button>
      </div>
    </div>
  );
}
