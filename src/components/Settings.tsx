import { useEffect, useState } from "react";
import { useStore } from "../store";
import { setConfig } from "../lib/tauri";
import type { AppConfig } from "../types";

interface Props {
  open: boolean;
  onClose: () => void;
}

export function Settings({ open, onClose }: Props) {
  const config = useStore((s) => s.config);
  const setStoreConfig = useStore((s) => s.setConfig);
  const [draft, setDraft] = useState<AppConfig>(config);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    if (open) setDraft(config);
  }, [open, config]);

  if (!open) return null;

  const save = async () => {
    try {
      await setConfig(draft);
      setStoreConfig(draft);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      alert("保存失败: " + String(e));
    }
  };

  return (
    <div className="af-modal-mask" onClick={onClose}>
      <div className="af-modal" onClick={(e) => e.stopPropagation()}>
        <div className="af-modal-head">⚙ API 设置（OpenAI 兼容）</div>
        <label className="af-field">
          <span>Base URL</span>
          <input
            value={draft.base_url}
            onChange={(e) => setDraft({ ...draft, base_url: e.target.value })}
            placeholder="https://api.openai.com/v1"
          />
        </label>
        <label className="af-field">
          <span>API Key</span>
          <input
            type="password"
            value={draft.api_key}
            onChange={(e) => setDraft({ ...draft, api_key: e.target.value })}
            placeholder="sk-..."
          />
        </label>
        <label className="af-field">
          <span>模型</span>
          <input
            value={draft.model}
            onChange={(e) => setDraft({ ...draft, model: e.target.value })}
            placeholder="gpt-4o-mini"
          />
        </label>
        <label className="af-field">
          <span>温度 (0–2)</span>
          <input
            type="number"
            step="0.1"
            min="0"
            max="2"
            value={draft.temperature}
            onChange={(e) =>
              setDraft({ ...draft, temperature: parseFloat(e.target.value) || 0 })
            }
          />
        </label>
        <div className="af-modal-actions">
          <span className="af-saved">{saved ? "✓ 已保存" : ""}</span>
          <button className="af-btn" onClick={onClose}>
            关闭
          </button>
          <button className="af-btn primary" onClick={save}>
            保存配置
          </button>
        </div>
      </div>
    </div>
  );
}
