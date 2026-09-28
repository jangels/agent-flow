import type { AgentNodeData, NodeType } from "../types";

export interface SelectedNode {
  id: string;
  type: NodeType;
  data: AgentNodeData;
}

interface Props {
  node: SelectedNode | null;
  onChange: (patch: Partial<AgentNodeData>) => void;
  onDelete: () => void;
}

export function Inspector({ node, onChange, onDelete }: Props) {
  if (!node) {
    return (
      <div className="af-inspector empty">
        <p>选中一个节点以编辑其属性。</p>
      </div>
    );
  }

  return (
    <div className="af-inspector">
      <div className="af-inspector-head">
        <span>节点属性 · {node.type}</span>
        <button className="af-btn danger sm" onClick={onDelete}>
          删除
        </button>
      </div>

      <label className="af-field">
        <span>名称</span>
        <input
          value={node.data.label ?? ""}
          onChange={(e) => onChange({ label: e.target.value })}
        />
      </label>

      {node.type === "start" && (
        <label className="af-field">
          <span>初始输入</span>
          <textarea
            rows={5}
            value={node.data.input ?? ""}
            onChange={(e) => onChange({ input: e.target.value })}
          />
        </label>
      )}

      {node.type === "llm" && (
        <>
          <label className="af-field">
            <span>System 指令</span>
            <textarea
              rows={3}
              value={node.data.system ?? ""}
              onChange={(e) => onChange({ system: e.target.value })}
            />
          </label>
          <label className="af-field">
            <span>Prompt（用户消息）</span>
            <textarea
              rows={5}
              value={node.data.prompt ?? ""}
              onChange={(e) => onChange({ prompt: e.target.value })}
            />
          </label>
          <label className="af-field">
            <span>模型（覆盖全局）</span>
            <input
              value={node.data.model ?? ""}
              placeholder="留空则使用全局模型"
              onChange={(e) => onChange({ model: e.target.value })}
            />
          </label>
        </>
      )}

      {node.type === "condition" && (
        <>
          <label className="af-field">
            <span>分支判据（自然语言）</span>
            <textarea
              rows={3}
              value={node.data.expression ?? ""}
              placeholder={"例：上游内容是否在询问天气\n候选分支 = 该节点的每一条出边"}
              onChange={(e) => onChange({ expression: e.target.value })}
            />
          </label>
          <label className="af-field">
            <span>判定模型（覆盖全局）</span>
            <input
              value={node.data.model ?? ""}
              placeholder="留空用全局模型；路由建议填便宜快的小模型"
              onChange={(e) => onChange({ model: e.target.value })}
            />
          </label>
          <p className="af-hint">
            由模型在候选分支中选一条（结构化输出 choose_branch，temperature=0）。判定在
            <strong>节点内部</strong>完成，选中结果会回显在节点上。
            <br />
            ⚠️ 仅<strong>委托 langgraph bridge 执行</strong>时生效：Rust 原生执行器下的 condition
            只做直通，且会执行<strong>全部</strong>出边，并不按判据选边。
          </p>
        </>
      )}

      {node.type === "tool" && (
        <>
          <label className="af-field">
            <span>工具白名单（ReAct 模式）</span>
            <textarea
              rows={3}
              value={(node.data.tools ?? []).join("\n")}
              placeholder={"每行一个，留空则走下面的单工具直调\ndemo__add\ndemo__get_time\n* 表示全部已加载工具"}
              onChange={(e) =>
                onChange({
                  tools: e.target.value
                    .split(/[\n,]/)
                    .map((s) => s.trim())
                    .filter(Boolean),
                })
              }
            />
          </label>
          <label className="af-field">
            <span>最大迭代轮数</span>
            <input
              type="number"
              min={1}
              value={node.data.max_iterations ?? 6}
              onChange={(e) =>
                onChange({ max_iterations: Math.max(1, Number(e.target.value) || 1) })
              }
            />
          </label>
          <label className="af-field">
            <span>MCP 工具全名（direct 模式）</span>
            <input
              value={node.data.mcp_tool ?? ""}
              placeholder="如 demo__add，单工具直调"
              onChange={(e) => onChange({ mcp_tool: e.target.value })}
            />
          </label>
          <label className="af-field">
            <span>工具名称（本地 mock 回退）</span>
            <input
              value={node.data.tool ?? ""}
              onChange={(e) => onChange({ tool: e.target.value })}
            />
          </label>
          <p className="af-hint">
            白名单非空 → 模型在<strong>节点内部</strong>自主循环：决定调哪个工具、调几次、何时收工。
            留空 → 走单工具直调。可用工具见 <code>GET /mcp/tools</code>。
          </p>
        </>
      )}

      {node.type === "human" && (
        <>
          <label className="af-field">
            <span>审批提示语</span>
            <textarea
              rows={3}
              value={node.data.hitlPrompt ?? ""}
              placeholder="例：是否批准本次 500 元退款？"
              onChange={(e) => onChange({ hitlPrompt: e.target.value })}
            />
          </label>
          <label className="af-field">
            <span>候选动作</span>
            <textarea
              rows={3}
              value={(node.data.actions ?? ["approve", "reject"]).join("\n")}
              placeholder={"每行一个，留空则用 approve / reject\napprove\nreject\nescalate"}
              onChange={(e) =>
                onChange({
                  actions: e.target.value
                    .split(/[\n,]/)
                    .map((s) => s.trim())
                    .filter(Boolean),
                })
              }
            />
          </label>
          <label className="af-field af-field-inline">
            <input
              type="checkbox"
              checked={node.data.allow_edit !== false}
              onChange={(e) => onChange({ allow_edit: e.target.checked })}
            />
            <span>允许审批时直接修改内容</span>
          </label>
          <p className="af-hint">
            流程执行到本节点会<strong>挂起</strong>，卡片上长出「批准 / 驳回」按钮；
            人给决策后从断点续跑，<strong>已执行过的节点不会重跑</strong>。
            <br />
            <strong>批准</strong>：下游拿到（可被人工修改过的）内容。
            <br />
            <strong>驳回</strong>：下游拿到 <code>[已驳回] 理由</code>，
            可接一个条件分支节点据此改道。
            <br />
            ⚠️ 依赖 bridge 侧的 checkpointer 与 thread_id，因此<strong>整图必须走
            langgraph bridge</strong>（含审批节点时会自动委托，无需手动配）。
          </p>
        </>
      )}

      {node.type === "end" && (
        <p className="af-hint">结束节点无需配置，输出即为工作流最终结果。</p>
      )}

      {node.data.output && (
        <div className="af-field">
          <span>运行输出</span>
          <pre className="af-output-box">{node.data.output}</pre>
        </div>
      )}
    </div>
  );
}
