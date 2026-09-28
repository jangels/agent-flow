import type { Flow } from "../types";

interface Props {
  flows: Flow[];
  activeId: string | null;
  running: boolean;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDuplicate: (id: string) => void;
  onDelete: (id: string) => void;
  onPublish: (id: string, published: boolean) => void;
}

export function FlowList({
  flows,
  activeId,
  running,
  onSelect,
  onCreate,
  onDuplicate,
  onDelete,
  onPublish,
}: Props) {
  return (
    <aside className="af-flows">
      <div className="af-flows-head">
        <span>流程</span>
        <button className="af-btn sm" onClick={onCreate} disabled={running}>
          + 新建
        </button>
      </div>
      <div className="af-flows-body">
        {flows.length === 0 && <div className="af-flows-empty">还没有流程</div>}
        {flows.map((f) => {
          const published = f.status === "published";
          return (
            <div
              key={f.id}
              className={"af-flow-item" + (f.id === activeId ? " active" : "")}
              onClick={() => onSelect(f.id)}
            >
              <div className="af-flow-main">
                <span className="af-flow-name" title={f.name}>
                  {f.name}
                </span>
                <span className={"af-flow-badge " + f.status}>
                  {published ? `已发布 v${f.version}` : "草稿"}
                </span>
              </div>
              <div className="af-flow-ops">
                <button
                  className="af-btn sm"
                  disabled={running}
                  onClick={(e) => {
                    e.stopPropagation();
                    onPublish(f.id, !published);
                  }}
                >
                  {published ? "取消发布" : "发布"}
                </button>
                <button
                  className="af-btn sm"
                  disabled={running}
                  onClick={(e) => {
                    e.stopPropagation();
                    onDuplicate(f.id);
                  }}
                >
                  复制
                </button>
                <button
                  className="af-btn sm danger"
                  disabled={running}
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete(f.id);
                  }}
                >
                  删除
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </aside>
  );
}
