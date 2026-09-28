import { useEffect, useRef } from "react";
import { useStore } from "../store";

export function Console() {
  const logs = useStore((s) => s.logs);
  const clearLogs = useStore((s) => s.clearLogs);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [logs]);

  return (
    <div className="af-console">
      <div className="af-console-head">
        <span>运行日志</span>
        <button className="af-btn ghost sm" onClick={clearLogs}>
          清空
        </button>
      </div>
      <div className="af-console-body" ref={ref}>
        {logs.length === 0 ? (
          <div className="af-console-empty">点击「运行」查看执行过程…</div>
        ) : (
          logs.map((l, i) => (
            <div key={i} className="af-log-line">
              {l}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
