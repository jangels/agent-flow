import type { Edge, Node } from "@xyflow/react";

export type NodeType = "start" | "llm" | "condition" | "tool" | "human" | "end";

export interface AgentNodeData {
  label: string;
  // start
  input?: string;
  // llm
  system?: string;
  prompt?: string;
  model?: string;
  // condition
  expression?: string;      // 分支判据（自然语言）。候选分支 = 该节点的出边，走 langgraph bridge 时由模型判定
  // tool
  tool?: string;            // 本地 mock 回退用的工具名
  mcp_tool?: string;        // direct 模式：MCP 工具全名（server__tool），单工具直调
  tools?: string[];         // ReAct 模式：工具白名单，非空即启用节点内循环（['*'] = 全部）
  max_iterations?: number;  // ReAct 循环硬上限（默认 6）
  // human（人工审批 / HITL）
  hitlPrompt?: string;      // 审批提示语，如「是否批准本次退款？」
  actions?: string[];       // 候选动作（默认 approve / reject）。approve 可带人工编辑的新内容
  allow_edit?: boolean;     // 是否允许审批时直接改内容（默认 true）
  // runtime (not persisted as config)
  status?: "idle" | "running" | "awaiting" | "done" | "error";
  output?: string;
  branch?: string;          // condition 运行期：选中分支的可读名（bridge 的 branchLabel）
  branchReason?: string;    // condition 运行期：模型给出的判定理由
  // human 运行期：挂起现场（node:awaiting 写入，resume 时回传）
  awaiting?: boolean;
  hitlInput?: string;       // 待审内容（挂起时的上游产物）
  hitlThreadId?: string;    // 挂起线程 id，续跑必须带
  hitlDecision?: string;    // 已提交的决策（approve / reject / 自定义），用于回显
  hitlNote?: string;        // 审批意见
  [key: string]: unknown;
}

export interface AppConfig {
  base_url: string;
  api_key: string;
  model: string;
  temperature: number;
}

export interface WfEvent {
  type: string;
  id?: string;
  nodeType?: string;
  token?: string;
  output?: string;
  result?: string;
  flowId?: string | null;
  // 工具调用细节（tool:start / tool:args / tool:result）
  toolCallId?: string;
  toolName?: string;
  delta?: string;
  content?: string;
  // condition 分支判定（node:done 扩展字段，由 bridge 的 branch* 透传）
  branch?: string;
  branchLabel?: string;
  branchReason?: string;
  // human / HITL（node:awaiting 扩展字段，由 bridge 的 RUN_PAUSED 透传）
  threadId?: string;        // 挂起线程 id —— 续跑必须带
  prompt?: string;          // 审批提示语
  input?: string;           // 待审内容（挂起时的上游产物）
  actions?: string[];       // 候选动作
  allowEdit?: boolean;      // 是否允许改内容
  paused?: boolean;         // workflow:done 上的标记：本次 run 是「暂停」而非「跑完」
  // 引擎告警（warn 事件）
  message?: string;
}

export type FlowStatus = "draft" | "published";

export interface FlowGraph {
  nodes: Node<AgentNodeData>[];
  edges: Edge[];
}

/** 一个可独立编辑 / 发布 / 执行的流程 */
export interface Flow {
  id: string;
  name: string;
  version: number;
  graph: FlowGraph;
  status: FlowStatus;
  updated_at: string;
}
