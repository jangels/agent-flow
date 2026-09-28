import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  MarkerType,
  type Node,
  type Edge,
  type Connection,
  type NodeProps,
} from "@xyflow/react";

import { Toolbar } from "./components/Toolbar";
import { Inspector } from "./components/Inspector";
import { Console } from "./components/Console";
import { Settings } from "./components/Settings";
import { FlowList } from "./components/FlowList";
import { nodeTypes as baseNodeTypes, HumanNode } from "./components/nodes";
import { useStore } from "./store";
import {
  getConfig,
  onWfEvent,
  runFlow,
  resumeFlow,
  listFlows,
  getActiveFlow,
  createFlow,
  saveFlow,
  deleteFlow,
  setActiveFlow,
  publishFlow,
  exportFlow,
  getBridgeUrl,
} from "./lib/tauri";
import type { AgentNodeData, Flow, FlowGraph, NodeType, WfEvent } from "./types";

const SEED_NODES: Node<AgentNodeData>[] = [
  {
    id: "n_start",
    type: "start",
    position: { x: 280, y: 40 },
    data: { label: "开始", input: "请介绍 Agent 工作流编排。" },
  },
  {
    id: "n_llm",
    type: "llm",
    position: { x: 280, y: 220 },
    data: {
      label: "LLM 调用",
      system: "你是一个高效、可靠的 AI 助手。",
      prompt: "用 3 句话概括上面的输入。",
    },
  },
  {
    id: "n_end",
    type: "end",
    position: { x: 280, y: 440 },
    data: { label: "结束" },
  },
];

const SEED_EDGES: Edge[] = [
  { id: "e1", source: "n_start", target: "n_llm" },
  { id: "e2", source: "n_llm", target: "n_end" },
];

const DEFAULT_DATA: Record<NodeType, AgentNodeData> = {
  start: { label: "开始", input: "" },
  llm: { label: "LLM 调用", system: "", prompt: "" },
  condition: { label: "条件分支", expression: "" },
  tool: { label: "工具调用", tool: "", tools: [] },
  human: {
    label: "人工审批",
    hitlPrompt: "请审批是否继续",
    actions: ["approve", "reject"],
    allow_edit: true,
  },
  end: { label: "结束" },
};

const emptyGraph = (): FlowGraph => ({ nodes: [], edges: [] });

/**
 * 清掉上一轮运行留下的痕迹（含 HITL 挂起现场）。
 * 不清理的话，上一轮挂起时写进节点的 threadId 会残留到下一次执行 —— 那时人去点
 * 审批按钮就会带着过期的 threadId 去续跑，报错信息还很难懂。
 */
const resetNodeRuntime = (d: AgentNodeData): AgentNodeData => ({
  ...d,
  status: "idle",
  output: "",
  branch: "",
  branchReason: "",
  awaiting: false,
  hitlInput: "",
  hitlThreadId: "",
  hitlDecision: "",
  hitlNote: "",
});

let idSeq = 100;

export default function App() {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<AgentNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [flows, setFlows] = useState<Flow[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [shareUrl, setShareUrl] = useState<string | null>(null);

  // 最新值 refs：跨 async 回调读取，避免闭包过期
  const flowsRef = useRef<Flow[]>([]);
  flowsRef.current = flows;
  const nodesRef = useRef<Node<AgentNodeData>[]>([]);
  nodesRef.current = nodes;
  const edgesRef = useRef<Edge[]>([]);
  edgesRef.current = edges;
  const activeRef = useRef<string | null>(null);
  activeRef.current = activeId;
  // 工具调用暂存：tool:start 记名 → tool:args 补参数 → tool:result 汇总展示
  const toolCalls = useRef<Map<string, { name: string; args: string }>>(new Map());

  const running = useStore((s) => s.running);
  const setRunning = useStore((s) => s.setRunning);
  const pushLog = useStore((s) => s.pushLog);
  const clearLogs = useStore((s) => s.clearLogs);
  const setStoreConfig = useStore((s) => s.setConfig);

  // running 的最新值 ref：resumeApproval 用它做重入判断。若把 running 写进依赖数组，
  // resumeApproval 每次运行状态变化都会变 → nodeTypes 重建 → 画布节点被整体重挂载。
  const runningRef = useRef(false);
  runningRef.current = running;

  useEffect(() => {
    getConfig()
      .then((c) => setStoreConfig(c))
      .catch(() => {});
  }, [setStoreConfig]);

  const applyFlow = useCallback(
    (f: Flow) => {
      setActiveId(f.id);
      activeRef.current = f.id;
      setNodes((f.graph?.nodes ?? []) as Node<AgentNodeData>[]);
      setEdges((f.graph?.edges ?? []) as Edge[]);
      setSelectedId(null);
    },
    [setNodes, setEdges]
  );

  // 首次挂载：拉流程列表；无流程则建一个默认流程；载入 active flow
  useEffect(() => {
    (async () => {
      try {
        let list = await listFlows();
        if (list.length === 0) {
          const f = await createFlow("默认流程", { nodes: SEED_NODES, edges: SEED_EDGES });
          list = [f];
        }
        setFlows(list);
        const af = await getActiveFlow();
        applyFlow(af);
      } catch (e) {
        pushLog("✗ 加载流程失败: " + String(e));
        setNodes(SEED_NODES);
        setEdges(SEED_EDGES);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 把当前画布写回 active flow 并持久化 */
  const persistActive = useCallback(async () => {
    const id = activeRef.current;
    if (!id) return;
    const cur = flowsRef.current.find((f) => f.id === id);
    if (!cur) return;
    const updated: Flow = {
      ...cur,
      graph: { nodes: nodesRef.current, edges: edgesRef.current },
    };
    await saveFlow(updated);
    setFlows((l) => l.map((f) => (f.id === id ? updated : f)));
  }, []);

  const onConnect = useCallback(
    (c: Connection) =>
      setEdges((eds) => addEdge({ ...c, markerEnd: { type: MarkerType.ArrowClosed } }, eds)),
    [setEdges]
  );

  const addNode = useCallback(
    (t: NodeType) => {
      const id = `n_${idSeq++}`;
      const position = { x: 120 + Math.random() * 360, y: 80 + Math.random() * 420 };
      const node: Node<AgentNodeData> = { id, type: t, position, data: { ...DEFAULT_DATA[t] } };
      setNodes((nds) => [...nds, node]);
    },
    [setNodes]
  );

  const updateSelected = useCallback(
    (patch: Partial<AgentNodeData>) => {
      if (!selectedId) return;
      setNodes((nds) =>
        nds.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, ...patch } } : n))
      );
    },
    [selectedId, setNodes]
  );

  const deleteSelected = useCallback(() => {
    if (!selectedId) return;
    setNodes((nds) => nds.filter((n) => n.id !== selectedId));
    setEdges((eds) => eds.filter((e) => e.source !== selectedId && e.target !== selectedId));
    setSelectedId(null);
  }, [selectedId, setNodes, setEdges]);

  const selectFlow = useCallback(
    async (id: string) => {
      if (id === activeRef.current) return;
      try {
        await persistActive();
        await setActiveFlow(id);
        const f = flowsRef.current.find((x) => x.id === id);
        if (f) applyFlow(f);
      } catch (e) {
        pushLog("✗ 切换流程失败: " + String(e));
      }
    },
    [persistActive, applyFlow, pushLog]
  );

  const newFlow = useCallback(async () => {
    try {
      await persistActive();
      const f = await createFlow(`流程 ${flowsRef.current.length + 1}`, emptyGraph());
      setFlows((l) => [...l, f]);
      applyFlow(f);
      pushLog(`＋ 已新建「${f.name}」`);
    } catch (e) {
      pushLog("✗ 新建失败: " + String(e));
    }
  }, [persistActive, applyFlow, pushLog]);

  const dupFlow = useCallback(
    async (id: string) => {
      const src = flowsRef.current.find((f) => f.id === id);
      if (!src) return;
      try {
        const graph = JSON.parse(JSON.stringify(src.graph ?? emptyGraph())) as FlowGraph;
        const f = await createFlow(src.name + " 副本", graph);
        setFlows((l) => [...l, f]);
        applyFlow(f);
        pushLog(`⧉ 已复制为「${f.name}」`);
      } catch (e) {
        pushLog("✗ 复制失败: " + String(e));
      }
    },
    [applyFlow, pushLog]
  );

  const delFlow = useCallback(
    async (id: string) => {
      try {
        await deleteFlow(id);
        const rest = flowsRef.current.filter((f) => f.id !== id);
        if (id === activeRef.current) {
          if (rest.length) {
            await setActiveFlow(rest[0].id);
            setFlows(rest);
            applyFlow(rest[0]);
          } else {
            const f = await createFlow("默认流程", emptyGraph());
            setFlows([f]);
            applyFlow(f);
          }
        } else {
          setFlows(rest);
        }
        pushLog("🗑 已删除流程");
      } catch (e) {
        pushLog("✗ 删除失败: " + String(e));
      }
    },
    [applyFlow, pushLog]
  );

  const togglePublish = useCallback(
    async (id: string, published: boolean) => {
      try {
        const f = await publishFlow(id, published);
        setFlows((l) => l.map((x) => (x.id === id ? f : x)));
        if (published) {
          // 发布成功 -> 生成聊天分享链接（bridge 同源托管 /chat.html，fetch 无跨域）
          const bridge = (await getBridgeUrl()).replace(/\/$/, "");
          const params = new URLSearchParams({
            flowId: id,
            name: f.name || "未命名流程",
          });
          setShareUrl(`${bridge}/chat.html?${params.toString()}`);
          pushLog(`🚀 「${f.name}」已发布 v${f.version} · 已生成聊天链接`);
        } else {
          setShareUrl(null);
          pushLog(`↩ 「${f.name}」已取消发布`);
        }
      } catch (e) {
        pushLog("✗ 发布失败: " + String(e));
      }
    },
    [pushLog]
  );

  const doExport = useCallback(async () => {
    const id = activeRef.current;
    if (!id) return;
    try {
      await persistActive();
      const path = await exportFlow(id);
      pushLog("📦 已导出: " + path);
    } catch (e) {
      pushLog("✗ 导出失败: " + String(e));
    }
  }, [persistActive, pushLog]);

  /**
   * wf:event → UI 状态。run 与 resume 共用 —— 两条路径的事件语义完全一致
   * （续跑也只是同一套 STEP_* / TOOL_CALL_* / node:awaiting 事件流的另一段）。
   */
  const handleWfEvent = useCallback(
    (e: WfEvent) => {
      // 事件归属过滤：只认当前激活流程（多流程并发/切换不串）
      if (e.flowId && e.flowId !== activeRef.current) return;

      if (e.type === "node:start") {
        setNodes((nds) =>
          nds.map((n) =>
            n.id === e.id
              ? { ...n, data: { ...n.data, status: "running", awaiting: false } }
              : n
          )
        );
        pushLog(`● 节点 ${e.id} (${e.nodeType}) 开始`);
      } else if (e.type === "tool:start") {
        const name = e.toolName ?? "工具";
        if (e.toolCallId) toolCalls.current.set(e.toolCallId, { name, args: "" });
        pushLog(`→ 调用工具 ${name}`);
      } else if (e.type === "tool:args") {
        const rec = e.toolCallId ? toolCalls.current.get(e.toolCallId) : undefined;
        if (rec) rec.args = e.delta ?? "";
      } else if (e.type === "tool:result") {
        const rec = e.toolCallId ? toolCalls.current.get(e.toolCallId) : undefined;
        const name = rec?.name ?? e.toolName ?? "工具";
        const raw = (rec?.args ?? "").trim().replace(/\s+/g, " ");
        const argText = raw ? `「${raw.length > 60 ? raw.slice(0, 60) + "…" : raw}」` : "";
        pushLog(`← ${name}${argText} 返回: ${e.content ?? ""}`);
        if (e.toolCallId) toolCalls.current.delete(e.toolCallId);
      } else if (e.type === "token") {
        setNodes((nds) =>
          nds.map((n) =>
            n.id === e.id
              ? { ...n, data: { ...n.data, output: (n.data.output ?? "") + (e.token ?? "") } }
              : n
          )
        );
      } else if (e.type === "node:done") {
        // condition 节点会带 branch/branchLabel/branchReason（bridge 的模型判定结果）
        const branch = e.branch ? e.branchLabel || e.branch : "";
        setNodes((nds) =>
          nds.map((n) =>
            n.id === e.id
              ? {
                  ...n,
                  data: {
                    ...n.data,
                    status: "done",
                    output: e.output,
                    ...(branch ? { branch, branchReason: e.branchReason ?? "" } : {}),
                  },
                }
              : n
          )
        );
        pushLog(`✓ 节点 ${e.id} 完成`);
        if (branch) {
          pushLog(`◆ 分支判定 → ${branch}${e.branchReason ? `（${e.branchReason}）` : ""}`);
        }
      } else if (e.type === "node:awaiting") {
        // HITL：人工审批节点挂起 —— 把挂起现场写进节点，卡片上会直接长出审批控件
        setNodes((nds) =>
          nds.map((n) =>
            n.id === e.id
              ? {
                  ...n,
                  data: {
                    ...n.data,
                    status: "awaiting",
                    awaiting: true,
                    hitlPrompt: (n.data as AgentNodeData).hitlPrompt || e.prompt || "",
                    hitlInput: e.input ?? "",
                    hitlThreadId: e.threadId ?? "",   // 续跑必须原样回传
                    hitlDecision: "",
                    hitlNote: "",
                    actions:
                      e.actions && e.actions.length
                        ? e.actions
                        : (n.data as AgentNodeData).actions,
                  },
                }
              : n
          )
        );
        pushLog(`🙋 节点 ${e.id} 等待人工审批：${e.prompt ?? ""}`);
      } else if (e.type === "warn") {
        pushLog(`⚠ ${e.message ?? "引擎告警"}`);
      } else if (e.type === "workflow:done") {
        if (e.paused) {
          // 暂停 ≠ 完成：本次 run 结束了，但流程停在审批处等人 —— 两者对用户的含义不同
          pushLog("⏸ 已暂停，等待人工审批（在审批节点上点「批准 / 驳回」）");
        } else {
          pushLog("■ 流程执行完成");
        }
        pushLog("结果:\n" + (e.result ?? ""));
      }
    },
    [setNodes, pushLog]
  );

  /** 提交人工审批决策并续跑（HITL 的第二半）。 */
  const resumeApproval = useCallback(
    async (nodeId: string, action: string, value: string) => {
      const id = activeRef.current;
      if (!id || runningRef.current) return;
      const node = nodesRef.current.find((n) => n.id === nodeId);
      const threadId = (node?.data as AgentNodeData | undefined)?.hitlThreadId ?? "";
      if (!threadId) {
        pushLog("✗ 无法续跑：该节点已不在等待态（缺少 threadId）");
        return;
      }
      setRunning(true);
      pushLog(`🙋 审批「${nodeId}」→ ${action}${value ? "（含人工修改）" : ""}`);
      // 先摘掉等待态，避免审批按钮被重复点击（续跑后由 node:start 重新置 running）
      setNodes((nds) =>
        nds.map((n) =>
          n.id === nodeId
            ? {
                ...n,
                data: {
                  ...n.data,
                  status: "running",
                  awaiting: false,
                  hitlDecision: action,
                },
              }
            : n
        )
      );

      const unlisten = await onWfEvent(handleWfEvent);
      try {
        await resumeFlow(id, threadId, { decision: action, value: value || undefined });
      } catch (err) {
        pushLog("✗ 续跑出错: " + String(err));
      } finally {
        unlisten();
        setRunning(false);
      }
    },
    [setNodes, setRunning, pushLog, handleWfEvent]
  );

  // 审批回调通过包装组件注入 —— React Flow 的节点组件拿不到 App 上下文。
  // 依赖 resumeApproval（自身稳定，见上面的 runningRef 用法），故 nodeTypes 不会反复重建。
  const nodeTypes = useMemo(
    () => ({
      ...baseNodeTypes,
      human: (props: NodeProps) => <HumanNode {...props} onResume={resumeApproval} />,
    }),
    [resumeApproval]
  );

  const run = useCallback(async () => {
    const id = activeRef.current;
    if (!id || running) return;
    try {
      await persistActive();
    } catch {
      /* 保存失败不阻断执行 */
    }
    setRunning(true);
    clearLogs();
    pushLog("▶ 开始执行流程…");
    setNodes((nds) => nds.map((n) => ({ ...n, data: resetNodeRuntime(n.data) })));
    toolCalls.current.clear();

    const unlisten = await onWfEvent(handleWfEvent);

    try {
      await runFlow(id);
    } catch (err) {
      pushLog("✗ 执行出错: " + String(err));
    } finally {
      unlisten();
      setRunning(false);
    }
  }, [running, persistActive, setNodes, setRunning, clearLogs, pushLog, handleWfEvent]);

  const save = useCallback(async () => {
    try {
      await persistActive();
      pushLog("💾 流程已保存");
    } catch (e) {
      pushLog("✗ 保存失败: " + String(e));
    }
  }, [persistActive, pushLog]);

  const selectedNode = useMemo(() => {
    const n = nodes.find((x) => x.id === selectedId);
    if (!n) return null;
    return { id: n.id, type: (n.type ?? "llm") as never, data: n.data };
  }, [nodes, selectedId]);

  const activeFlow = useMemo(
    () => flows.find((f) => f.id === activeId) ?? null,
    [flows, activeId]
  );

  return (
    <div className="af-app">
      <Toolbar
        onAddNode={addNode}
        onRun={run}
        onSave={save}
        onPublish={() => activeId && togglePublish(activeId, activeFlow?.status !== "published")}
        onExport={doExport}
        onOpenSettings={() => setSettingsOpen(true)}
        running={running}
        published={activeFlow?.status === "published"}
      />
      <div className="af-main">
        <FlowList
          flows={flows}
          activeId={activeId}
          running={running}
          onSelect={selectFlow}
          onCreate={newFlow}
          onDuplicate={dupFlow}
          onDelete={delFlow}
          onPublish={togglePublish}
        />
        <div className="af-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            nodeTypes={nodeTypes}
            onNodeClick={(_, n) => setSelectedId(n.id)}
            onPaneClick={() => setSelectedId(null)}
            defaultEdgeOptions={{ markerEnd: { type: MarkerType.ArrowClosed } }}
            fitView
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={16} />
            <Controls />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>
        <aside className="af-side">
          <Inspector node={selectedNode} onChange={updateSelected} onDelete={deleteSelected} />
          <Console />
        </aside>
      </div>
      <Settings open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      {shareUrl && (
        <div className="af-modal" onClick={() => setShareUrl(null)}>
          <div className="af-modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="af-modal-title">🚀 发布成功 · 聊天分享链接</div>
            <p className="af-modal-desc">
              任何人打开此链接即可进入聊天页，主动输入内容运行「{activeFlow?.name ?? ""}」。
              链接指向本地前端页面（http://localhost:1420），仅本机/同网可访问。
            </p>
            <input
              className="af-modal-input"
              readOnly
              value={shareUrl}
              onFocus={(e) => e.currentTarget.select()}
            />
            <div className="af-modal-actions">
              <button
                className="af-btn"
                onClick={() => {
                  navigator.clipboard?.writeText(shareUrl);
                  pushLog("📋 聊天链接已复制");
                }}
              >
                复制链接
              </button>
              <button className="af-btn af-btn-ghost" onClick={() => setShareUrl(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
