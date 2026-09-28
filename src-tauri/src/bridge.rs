use futures_util::StreamExt;
use serde_json::{json, Value};
use std::collections::HashMap;
use tauri::Emitter;

use crate::AppConfig;

/// AG-UI → 内部 `wf:event` 翻译过程中的跨事件状态。
///
/// 单个 AG-UI 事件往往无法独立翻译：流式 token 要靠 messageId 找回所属节点、
/// 工具返回要靠 toolCallId 找回工具名，因此这些映射必须跨事件保存。
#[derive(Default)]
struct TranslateState {
    /// 当前正在执行的 step（即 agent-flow 的 node id）
    active_node: Option<String>,
    /// messageId → node id：把 TEXT_MESSAGE_CONTENT 的流式分片归属到发起节点
    msg_to_node: HashMap<String, String>,
    /// toolCallId → (node id, 工具名)：TOOL_CALL_RESULT 通常不带工具名，需回查
    tool_map: HashMap<String, (String, String)>,
}

/// bridge 服务的 origin（scheme://host:port）。
///
/// `LANGGRAPH_BRIDGE_URL` 允许带路径（历史默认值是 `.../run`），所以 resume 需要
/// 先剥掉路径再拼自己的端点。裸 base 也兼容。
fn bridge_origin() -> String {
    let raw = std::env::var("LANGGRAPH_BRIDGE_URL")
        .unwrap_or_else(|_| "http://127.0.0.1:8000/run".to_string());
    match raw.find("://") {
        Some(i) => {
            let rest = &raw[i + 3..];
            let end = rest.find('/').map(|j| i + 3 + j).unwrap_or(raw.len());
            raw[..end].to_string()
        }
        None => raw,
    }
}

/// 把整张工作流图委托给 langgraph-bridge 服务执行，SSE 流式回传。
///
/// agent-flow 的 Rust 引擎遇到 `tool` 类型且 `tool == "langgraph"`（或 `data.bridge == true`）
/// 的节点时调用本函数：把整张 {nodes, edges} + 上游输入 POST 给 Python 服务，服务用
/// LangGraph 编译执行，再以【AG-UI 标准协议】事件流（RUN_STARTED / STEP_STARTED /
/// TEXT_MESSAGE_CONTENT / TOOL_CALL_* / STEP_FINISHED / RUN_FINISHED / RUN_ERROR 等）推回。
///
/// 本函数在 Rust 侧做「AG-UI → 内部 wf:event」薄适配层：把标准事件翻译回 agent-flow
/// 内部事件模型（node:start / token / tool:start / tool:args / tool:result /
/// node:done / node:awaiting / workflow:done）再 emit 前端。后端已是标准 AG-UI server，
/// 未来可直接对接 CopilotKit 等任意 AG-UI 前端。
///
/// `flow_id` 会被注入每个 wf:event，前端据此把事件归属到对应流程。
pub async fn run_via_langgraph(
    app: &tauri::AppHandle,
    config: &AppConfig,
    wf: &Value,
    input: &str,
    flow_id: Option<String>,
) -> Result<String, String> {
    // 服务地址可通过环境变量覆盖（便于 sidecar / 远程部署）。
    // 用 bridge_origin() 拿 host 再拼 /run，环境变量带不带 /run 都能正确。
    let url = format!("{}/run", bridge_origin());

    let payload = json!({
        "nodes": wf["nodes"],
        "edges": wf["edges"],
        "input": input,
        "config": {
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
            "temperature": config.temperature,
        }
    });

    stream_agui(app, &url, payload, flow_id).await
}

/// 恢复一个「等待人工审批」而挂起的运行（HITL 的第二半）。
///
/// POST `/flows/{flow_id}/resume`，body 带 threadId（挂起时由 RUN_PAUSED 带出）与
/// 人的决策。bridge 用 `Command(resume=...)` + 同一 thread_id 从断点续跑，
/// 续跑过程同样以 AG-UI 事件流推回 —— 因此**复用同一条 SSE 消费链路**。
///
/// 续跑之后可能又挂在下一个审批节点上（此时再收一次 node:awaiting），也可能跑完。
pub async fn resume_via_langgraph(
    app: &tauri::AppHandle,
    config: &AppConfig,
    flow_id: &str,
    thread_id: &str,
    decision: Value,
) -> Result<String, String> {
    let url = format!("{}/flows/{}/resume", bridge_origin(), flow_id);
    let payload = json!({
        "threadId": thread_id,
        "decision": decision,
        "config": {
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
            "temperature": config.temperature,
        }
    });
    stream_agui(app, &url, payload, Some(flow_id.to_string())).await
}

/// POST 一个 AG-UI SSE 端点，把事件翻译成 wf:event 逐个 emit 前端。
///
/// run 与 resume 共用：两者只有 URL 与 body 不同，事件消费逻辑完全一致。
async fn stream_agui(
    app: &tauri::AppHandle,
    url: &str,
    payload: Value,
    flow_id: Option<String>,
) -> Result<String, String> {
    let client = reqwest::Client::new();
    let resp = client
        .post(url)
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("bridge 请求失败: {e}"))?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("bridge 服务错误 {status}: {text}"));
    }

    let mut stream = resp.bytes_stream();
    let mut buf = String::new();
    let mut result = String::new();
    // 是否收到过终止事件（RUN_FINISHED / RUN_ERROR）。
    // 不能靠 result 是否为空来判断：挂起时本次 run 可能没有最终输出，
    // 用空串判据会把正常的「暂停」误报成「bridge 没返回终止事件」。
    let mut saw_terminal = false;
    // AG-UI → 内部 wf:event 薄适配层状态
    let mut st = TranslateState::default();

    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("读取 bridge 流失败: {e}"))?;
        buf.push_str(&String::from_utf8_lossy(&chunk));

        // SSE 帧按行分隔；只解析 `data:` 行
        while let Some(idx) = buf.find('\n') {
            let line: String = buf.drain(..=idx).collect();
            let line = line.trim_end();
            if !line.starts_with("data:") {
                continue;
            }
            let data = line.trim_start_matches("data:").trim();
            if data == "[DONE]" {
                break;
            }
            if let Ok(ev) = serde_json::from_str::<Value>(data) {
                // 翻译为内部事件并 emit（每个 token / 每个工具事件单独推送，前端实时渲染）
                if let Some(Some(mut payload)) = translate_agui(&ev, &mut st) {
                    if let Some(obj) = payload.as_object_mut() {
                        obj.insert("flowId".to_string(), json!(flow_id));
                    }
                    app.emit("wf:event", payload).ok();
                }
                // 最终 result 取自 RUN_FINISHED.result 或 RUN_ERROR.message
                let etype = ev["type"].as_str().unwrap_or("");
                if etype == "RUN_FINISHED" || etype == "RUN_ERROR" {
                    saw_terminal = true;
                    if let Some(r) = ev["result"].as_str().or_else(|| ev["message"].as_str()) {
                        result = r.to_string();
                    }
                }
            }
        }
    }

    if !saw_terminal {
        return Err("bridge 未返回 RUN_FINISHED / RUN_ERROR 事件".into());
    }
    Ok(result)
}

/// 把单个 AG-UI 标准事件翻译为 agent-flow 内部 wf:event。
///
/// - `Some(Some(payload))`：需 emit 该内部事件
/// - `Some(None)`：该 AG-UI 事件已被适配层吸收（如 TEXT_MESSAGE_START 仅记录映射、
///   TOOL_CALL_END 无新增信息），不 emit
/// - `None`：未识别的 AG-UI 事件（STATE_*/CUSTOM/RAW 等），忽略
fn translate_agui(ev: &Value, st: &mut TranslateState) -> Option<Option<Value>> {
    let etype = ev["type"].as_str()?;
    match etype {
        "STEP_STARTED" => {
            let id = ev["stepName"].as_str().unwrap_or("").to_string();
            st.active_node = if id.is_empty() {
                None
            } else {
                Some(id.clone())
            };
            Some(Some(
                json!({"type": "node:start", "id": id, "nodeType": ev["nodeType"]}),
            ))
        }
        "TEXT_MESSAGE_START" => {
            if let Some(mid) = ev["messageId"].as_str() {
                if let Some(n) = st.active_node.clone() {
                    st.msg_to_node.insert(mid.to_string(), n);
                }
            }
            Some(None) // 仅记录 messageId→node 映射，不 emit
        }
        "TEXT_MESSAGE_CONTENT" => {
            let tok = ev["delta"].as_str().unwrap_or("").to_string();
            if tok.is_empty() {
                return Some(None);
            }
            let id = ev["messageId"]
                .as_str()
                .and_then(|m| st.msg_to_node.get(m).cloned())
                .or_else(|| st.active_node.clone())
                .unwrap_or_default();
            Some(Some(json!({"type": "token", "id": id, "token": tok})))
        }
        "TEXT_MESSAGE_END" => Some(None),
        "STEP_FINISHED" => {
            let id = ev["stepName"].as_str().unwrap_or("").to_string();
            let output = ev["output"].as_str().unwrap_or("").to_string();
            st.active_node = None;
            let mut payload = json!({"type": "node:done", "id": id, "output": output});
            // condition 节点（S2）的分支判定结果：应用扩展字段，有才透传
            // （普通节点不带 branch 键，前端据此区分）
            if let Some(b) = ev["branch"].as_str().filter(|s| !s.is_empty()) {
                if let Some(obj) = payload.as_object_mut() {
                    obj.insert("branch".to_string(), json!(b));
                    obj.insert(
                        "branchLabel".to_string(),
                        json!(ev["branchLabel"].as_str().unwrap_or(b)),
                    );
                    obj.insert(
                        "branchReason".to_string(),
                        json!(ev["branchReason"].as_str().unwrap_or("")),
                    );
                }
            }
            Some(Some(payload))
        }
        // ---- 工具调用细节：翻译为 tool:* 事件，供前端 Console / 节点展示 ----
        "TOOL_CALL_START" => {
            let tcid = ev["toolCallId"].as_str().unwrap_or("").to_string();
            let name = ev["toolCallName"].as_str().unwrap_or("工具").to_string();
            let node = st.active_node.clone().unwrap_or_default();
            if !tcid.is_empty() {
                st.tool_map
                    .insert(tcid.clone(), (node.clone(), name.clone()));
            }
            Some(Some(json!({
                "type": "tool:start",
                "id": node,
                "toolCallId": tcid,
                "toolName": name,
            })))
        }
        "TOOL_CALL_ARGS" => {
            let tcid = ev["toolCallId"].as_str().unwrap_or("").to_string();
            let delta = ev["delta"].as_str().unwrap_or("").to_string();
            let node = match st.tool_map.get(&tcid) {
                Some((n, _)) => n.clone(),
                None => st.active_node.clone().unwrap_or_default(),
            };
            Some(Some(json!({
                "type": "tool:args",
                "id": node,
                "toolCallId": tcid,
                "delta": delta,
            })))
        }
        // 参数已由 TOOL_CALL_ARGS 送达，END 本身无新增信息
        "TOOL_CALL_END" => Some(None),
        "TOOL_CALL_RESULT" => {
            let tcid = ev["toolCallId"].as_str().unwrap_or("").to_string();
            let content = ev["content"].as_str().unwrap_or("").to_string();
            let (node, name) = st.tool_map.remove(&tcid).unwrap_or_default();
            Some(Some(json!({
                "type": "tool:result",
                "id": node,
                "toolCallId": tcid,
                "toolName": name,
                "content": content,
            })))
        }
        // ---- HITL：人工审批节点挂起（AG-UI 扩展事件，标准协议没有「暂停」语义）----
        "RUN_PAUSED" => {
            let id = ev["stepName"].as_str().unwrap_or("").to_string();
            // actions 缺失时补默认值，前端不必再兜一次
            let actions = match ev["actions"].as_array() {
                Some(a) if !a.is_empty() => json!(a),
                _ => json!(["approve", "reject"]),
            };
            Some(Some(json!({
                "type": "node:awaiting",
                "id": id,
                "threadId": ev["threadId"].as_str().unwrap_or(""),
                "prompt": ev["prompt"].as_str().unwrap_or("请确认是否继续"),
                "input": ev["input"].as_str().unwrap_or(""),
                "actions": actions,
                "allowEdit": ev["allowEdit"].as_bool().unwrap_or(true),
            })))
        }
        "RUN_FINISHED" => {
            let r = ev["result"].as_str().unwrap_or("").to_string();
            let mut payload = json!({"type": "workflow:done", "result": r});
            // 挂起收尾：本次 run 结束，但流程停在等待人工审批处 —— 前端据此提示「待审批」
            // 而不是「已完成」，两种状态对用户的含义完全不同
            if ev["paused"].as_bool().unwrap_or(false) {
                if let Some(obj) = payload.as_object_mut() {
                    obj.insert("paused".to_string(), json!(true));
                }
            }
            Some(Some(payload))
        }
        "RUN_ERROR" => {
            let msg = ev["message"].as_str().unwrap_or("未知错误").to_string();
            let r = format!("[AG-UI RUN_ERROR] {msg}");
            Some(Some(json!({"type": "workflow:done", "result": r})))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn one(ev: Value, st: &mut TranslateState) -> Option<Value> {
        translate_agui(&ev, st).flatten()
    }

    /// 工具调用完整生命周期：START → ARGS → END → RESULT
    #[test]
    fn tool_call_lifecycle_maps_to_tool_events() {
        let mut st = TranslateState::default();

        let out = one(
            json!({"type": "STEP_STARTED", "stepName": "t1", "nodeType": "tool"}),
            &mut st,
        )
        .expect("STEP_STARTED 应产生事件");
        assert_eq!(out["type"], "node:start");
        assert_eq!(st.active_node.as_deref(), Some("t1"));

        let out = one(
            json!({"type": "TOOL_CALL_START", "toolCallId": "tc_t1", "toolCallName": "demo__add"}),
            &mut st,
        )
        .expect("TOOL_CALL_START 应产生事件");
        assert_eq!(out["type"], "tool:start");
        assert_eq!(out["id"], "t1");
        assert_eq!(out["toolCallId"], "tc_t1");
        assert_eq!(out["toolName"], "demo__add");

        let out = one(
            json!({"type": "TOOL_CALL_ARGS", "toolCallId": "tc_t1", "delta": "计算 3 加 5"}),
            &mut st,
        )
        .expect("TOOL_CALL_ARGS 应产生事件");
        assert_eq!(out["type"], "tool:args");
        assert_eq!(out["id"], "t1");
        assert_eq!(out["delta"], "计算 3 加 5");

        // END 无新增信息，被吸收
        assert!(one(
            json!({"type": "TOOL_CALL_END", "toolCallId": "tc_t1"}),
            &mut st
        )
        .is_none());

        // RESULT 到达时 active_node 已清空，靠 tool_map 回查节点与工具名
        st.active_node = None;
        let out = one(
            json!({"type": "TOOL_CALL_RESULT", "toolCallId": "tc_t1", "content": "3 + 5 = 8"}),
            &mut st,
        )
        .expect("TOOL_CALL_RESULT 应产生事件");
        assert_eq!(out["type"], "tool:result");
        assert_eq!(out["id"], "t1");
        assert_eq!(out["toolName"], "demo__add");
        assert_eq!(out["content"], "3 + 5 = 8");
        assert!(!st.tool_map.contains_key("tc_t1"), "RESULT 后应清理映射");
    }

    /// 流式 token 仍需正确归属到发起节点（原有行为不能被破坏）
    #[test]
    fn text_stream_keeps_node_attribution() {
        let mut st = TranslateState::default();
        one(
            json!({"type": "STEP_STARTED", "stepName": "n_llm"}),
            &mut st,
        );
        assert!(one(
            json!({"type": "TEXT_MESSAGE_START", "messageId": "msg_n_llm"}),
            &mut st
        )
        .is_none());

        let out = one(
            json!({"type": "TEXT_MESSAGE_CONTENT", "messageId": "msg_n_llm", "delta": "你"}),
            &mut st,
        )
        .expect("TEXT_MESSAGE_CONTENT 应产生 token");
        assert_eq!(out["type"], "token");
        assert_eq!(out["id"], "n_llm");
        assert_eq!(out["token"], "你");

        let out = one(
            json!({"type": "STEP_FINISHED", "stepName": "n_llm", "output": "你好"}),
            &mut st,
        )
        .expect("STEP_FINISHED 应产生事件");
        assert_eq!(out["type"], "node:done");
        assert_eq!(out["output"], "你好");
        assert!(st.active_node.is_none(), "节点结束后应清空 active_node");
    }

    #[test]
    fn run_finished_and_error_map_to_workflow_done() {
        let mut st = TranslateState::default();
        let out = one(json!({"type": "RUN_FINISHED", "result": "OK"}), &mut st).unwrap();
        assert_eq!(out["type"], "workflow:done");
        assert_eq!(out["result"], "OK");

        let out = one(json!({"type": "RUN_ERROR", "message": "boom"}), &mut st).unwrap();
        assert_eq!(out["type"], "workflow:done");
        assert_eq!(out["result"], "[AG-UI RUN_ERROR] boom");
    }

    #[test]
    fn unknown_events_are_ignored() {
        let mut st = TranslateState::default();
        assert!(translate_agui(&json!({"type": "STATE_SNAPSHOT"}), &mut st).is_none());
        assert!(translate_agui(&json!({"nope": 1}), &mut st).is_none());
        // RUN_STARTED 带应用扩展字段（recursionLimit / hasCycle / resumed）时同样忽略
        assert!(translate_agui(
            &json!({"type": "RUN_STARTED", "recursionLimit": 25, "hasCycle": false, "resumed": true}),
            &mut st
        )
        .is_none());
    }

    /// HITL 挂起：RUN_PAUSED 必须翻译成 node:awaiting 并带齐前端审批所需的全部字段
    /// （threadId 缺了就无法续跑，actions 缺了就没有按钮）。
    #[test]
    fn run_paused_maps_to_node_awaiting() {
        let mut st = TranslateState::default();
        let out = one(
            json!({"type": "RUN_PAUSED", "threadId": "t-abc123", "runId": "run-1",
                   "stepName": "n_human", "prompt": "是否批准退款？", "input": "退款 500 元",
                   "actions": ["approve", "reject"], "allowEdit": true}),
            &mut st,
        )
        .expect("RUN_PAUSED 应产生事件");
        assert_eq!(out["type"], "node:awaiting");
        assert_eq!(out["id"], "n_human");
        assert_eq!(out["threadId"], "t-abc123");
        assert_eq!(out["prompt"], "是否批准退款？");
        assert_eq!(out["input"], "退款 500 元");
        assert_eq!(out["allowEdit"], true);
        assert_eq!(out["actions"].as_array().map(|a| a.len()), Some(2));
    }

    /// actions 缺失 / 为空时补默认值，前端不必再兜一次；allowEdit 缺失默认 true。
    #[test]
    fn run_paused_fills_defaults() {
        let mut st = TranslateState::default();
        let out = one(
            json!({"type": "RUN_PAUSED", "threadId": "t1", "stepName": "h", "input": "x"}),
            &mut st,
        )
        .unwrap();
        assert_eq!(out["actions"].as_array().map(|a| a.len()), Some(2));
        assert_eq!(out["actions"][0], "approve");
        assert_eq!(out["actions"][1], "reject");
        assert_eq!(out["allowEdit"], true, "allowEdit 缺失应默认为 true");
        assert_eq!(out["prompt"], "请确认是否继续", "prompt 缺失要有兜底文案");

        // actions 显式给空数组 -> 同样补默认（空数组会让前端渲染不出任何按钮）
        let empty = one(
            json!({"type": "RUN_PAUSED", "threadId": "t1", "stepName": "h", "actions": []}),
            &mut st,
        )
        .unwrap();
        assert_eq!(empty["actions"].as_array().map(|a| a.len()), Some(2));
    }

    /// RUN_FINISHED 的 paused 标记必须透传到 workflow:done —— 前端靠它区分
    /// 「跑完了」和「停在审批处」，两者对用户的含义完全不同。
    #[test]
    fn run_finished_paused_flag_is_forwarded() {
        let mut st = TranslateState::default();
        let paused = one(
            json!({"type": "RUN_FINISHED", "result": "待审内容", "paused": true}),
            &mut st,
        )
        .unwrap();
        assert_eq!(paused["type"], "workflow:done");
        assert_eq!(paused["paused"], true);

        // 正常结束不带 paused 键（前端靠键是否存在区分，不能靠 false 值）
        let normal = one(json!({"type": "RUN_FINISHED", "result": "OK"}), &mut st).unwrap();
        assert_eq!(normal["type"], "workflow:done");
        assert!(normal.get("paused").is_none(), "正常结束不应带 paused 键");
    }

    /// bridge_origin 要能同时处理「带路径」和「裸 base」两种配置，
    /// 否则 resume 会拼出 /run/flows/... 这种错误地址。
    #[test]
    fn bridge_origin_strips_path() {
        // 该函数读环境变量，测试内串行设置/还原
        let saved = std::env::var("LANGGRAPH_BRIDGE_URL").ok();

        std::env::set_var("LANGGRAPH_BRIDGE_URL", "http://127.0.0.1:8000/run");
        assert_eq!(bridge_origin(), "http://127.0.0.1:8000");

        std::env::set_var("LANGGRAPH_BRIDGE_URL", "http://127.0.0.1:8000");
        assert_eq!(bridge_origin(), "http://127.0.0.1:8000");

        std::env::set_var(
            "LANGGRAPH_BRIDGE_URL",
            "https://bridge.example.com:9443/some/base",
        );
        assert_eq!(bridge_origin(), "https://bridge.example.com:9443");

        match saved {
            Some(v) => std::env::set_var("LANGGRAPH_BRIDGE_URL", v),
            None => std::env::remove_var("LANGGRAPH_BRIDGE_URL"),
        }
    }

    /// condition 节点的 STEP_FINISHED 带 branch* 扩展字段时，必须透传到 node:done；
    /// 普通节点不带这些键时，payload 里不应凭空出现 branch（前端靠它区分节点类型）。
    #[test]
    fn condition_branch_is_forwarded_only_when_present() {
        let mut st = TranslateState::default();

        let out = one(
            json!({"type": "STEP_FINISHED", "stepName": "n_cond", "output": "上游内容",
                   "branch": "t_yes", "branchLabel": "查天气", "branchReason": "命中判据"}),
            &mut st,
        )
        .expect("STEP_FINISHED 应产生事件");
        assert_eq!(out["type"], "node:done");
        assert_eq!(out["id"], "n_cond");
        assert_eq!(
            out["output"], "上游内容",
            "condition 的输出应直通上游，不被判定污染"
        );
        assert_eq!(out["branch"], "t_yes");
        assert_eq!(out["branchLabel"], "查天气");
        assert_eq!(out["branchReason"], "命中判据");

        // 普通节点：无 branch 键 -> payload 不含 branch
        let plain = one(
            json!({"type": "STEP_FINISHED", "stepName": "n_llm", "output": "hi"}),
            &mut st,
        )
        .expect("STEP_FINISHED 应产生事件");
        assert!(plain.get("branch").is_none(), "普通节点不应带 branch");

        // branch 为空串（异常路径）-> 同样不透传
        let empty = one(
            json!({"type": "STEP_FINISHED", "stepName": "n_cond", "output": "x", "branch": ""}),
            &mut st,
        )
        .expect("STEP_FINISHED 应产生事件");
        assert!(empty.get("branch").is_none(), "空 branch 不应透传");

        // branchLabel 缺失时退回用 branch 本身，前端不必再兜一次
        let no_label = one(
            json!({"type": "STEP_FINISHED", "stepName": "n_cond", "output": "x",
                   "branch": "t_false"}),
            &mut st,
        )
        .expect("STEP_FINISHED 应产生事件");
        assert_eq!(no_label["branchLabel"], "t_false");
        assert_eq!(no_label["branchReason"], "");
    }

    /// ReAct 节点的多轮工具调用：同一节点内每轮 toolCallId 不同，
    /// tool_map 不能把上一轮的工具名串到下一轮。
    #[test]
    fn react_multi_round_tool_calls_stay_isolated() {
        let mut st = TranslateState::default();
        one(
            json!({"type": "STEP_STARTED", "stepName": "n_react", "nodeType": "tool"}),
            &mut st,
        );

        // 第 0 轮：add
        let a = one(
            json!({"type": "TOOL_CALL_START", "toolCallId": "tc_n_react_r0_0",
                   "toolCallName": "add"}),
            &mut st,
        )
        .unwrap();
        assert_eq!(a["type"], "tool:start");
        assert_eq!(a["id"], "n_react");
        assert_eq!(a["toolName"], "add");
        let r0 = one(
            json!({"type": "TOOL_CALL_RESULT", "toolCallId": "tc_n_react_r0_0",
                   "content": "3 + 5 = 8"}),
            &mut st,
        )
        .unwrap();
        assert_eq!(r0["type"], "tool:result");
        assert_eq!(r0["toolName"], "add");
        assert_eq!(r0["content"], "3 + 5 = 8");

        // 第 1 轮：get_time（同一节点、不同 toolCallId）
        let b = one(
            json!({"type": "TOOL_CALL_START", "toolCallId": "tc_n_react_r1_0",
                   "toolCallName": "get_time"}),
            &mut st,
        )
        .unwrap();
        assert_eq!(b["id"], "n_react", "多轮仍归属同一节点");
        assert_eq!(b["toolName"], "get_time");
        let r1 = one(
            json!({"type": "TOOL_CALL_RESULT", "toolCallId": "tc_n_react_r1_0",
                   "content": "2026-09-24T00:41:00"}),
            &mut st,
        )
        .unwrap();
        assert_eq!(
            r1["toolName"], "get_time",
            "第 1 轮不能被第 0 轮的工具名污染"
        );
        assert_eq!(r1["content"], "2026-09-24T00:41:00");

        // 两轮结束后映射应全部清理，不残留
        assert!(st.tool_map.is_empty(), "多轮结束后 tool_map 应清空");
    }
}
