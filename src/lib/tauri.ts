import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { AppConfig, Flow, FlowGraph, WfEvent } from "../types";

// ---- 配置 ----
export const getConfig = () => invoke<AppConfig>("get_config");
export const setConfig = (config: AppConfig) => invoke<void>("set_config", { config });

// ---- 多流程 ----
export const listFlows = () => invoke<Flow[]>("list_flows");
export const getActiveFlow = () => invoke<Flow>("get_active_flow");
export const createFlow = (name: string, graph: FlowGraph) =>
  invoke<Flow>("create_flow", { name, graph });
export const saveFlow = (flow: Flow) => invoke<Flow>("save_flow", { flow });
export const deleteFlow = (id: string) => invoke<void>("delete_flow", { id });
export const setActiveFlow = (id: string) => invoke<void>("set_active_flow", { id });
export const publishFlow = (id: string, published: boolean) =>
  invoke<Flow>("publish_flow", { id, published });
/** 聊天分享链接用的 bridge 地址（与 bridge.rs / start.sh 同一环境变量）。 */
export const getBridgeUrl = () => invoke<string>("get_bridge_url");
export const exportFlow = (id: string) => invoke<string>("export_flow", { id });
export const runFlow = (id: string) =>
  invoke<{ result: string; nodeOutputs: Record<string, string> }>("run_flow", { id });

/** 恢复一个「等待人工审批」而挂起的运行（HITL 续跑）。
 *
 *  threadId 来自 node:awaiting 事件；decision 形如
 *  { decision: "approve", value: "人工改过的内容", note: "意见" }。
 *  续跑过程同样以 wf:event 流式回推（可能又挂在下一个审批节点）。
 */
export const resumeFlow = (
  id: string,
  threadId: string,
  decision: { decision: string; value?: string; note?: string }
) => invoke<string>("resume_flow", { id, threadId, decision });

// ---- 兼容旧接口（单流程）----
export const saveWorkflow = (workflow: unknown) => invoke<void>("save_workflow", { workflow });
export const loadWorkflow = () => invoke<unknown>("load_workflow");
export const runWorkflow = (workflow: unknown) =>
  invoke<{ result: string; nodeOutputs: Record<string, string> }>("run_workflow", { workflow });

// ---- 事件流 ----
export const onWfEvent = (cb: (e: WfEvent) => void): Promise<UnlistenFn> =>
  listen<WfEvent>("wf:event", (e) => cb(e.payload));
