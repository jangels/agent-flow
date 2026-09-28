use std::collections::{HashMap, VecDeque};

use serde_json::{json, Value};
use tauri::Emitter;

use crate::AppConfig;

/// Execute a workflow graph: topological sort over nodes, run each node,
/// propagate outputs along edges, and stream progress events to the frontend.
///
/// `flow_id` 可选：执行某个「流程」时带上，所有 wf:event 会附 `flowId`，
/// 前端据此把事件路由到对应流程（多流程并发 / 切换时不串）。
pub async fn execute(
    app: &tauri::AppHandle,
    config: &AppConfig,
    wf: Value,
    flow_id: Option<String>,
) -> Result<Value, String> {
    let nodes = wf["nodes"].as_array().ok_or("workflow 缺少 nodes 字段")?;
    let edges = wf["edges"].as_array().cloned().unwrap_or_default();
    let fid = &flow_id;

    // index nodes by id
    let mut node_map: HashMap<String, Value> = HashMap::new();
    for n in nodes {
        let id = n["id"].as_str().unwrap_or("").to_string();
        node_map.insert(id, n.clone());
    }
    if node_map.is_empty() {
        return Err("workflow 没有任何节点".into());
    }

    // 图级预检：human（人工审批）节点需要「挂起 → 等人 → 恢复」，而这三件事只存在于
    // langgraph bridge（checkpointer + interrupt + Command(resume)）。Rust 原生执行器是
    // 确定性 DAG，既不能中断也不能恢复 —— 若在这里直通，等于【静默跳过审批】，
    // 那是安全事故而非功能缺失。所以检测到 human 节点就直接整图委托 bridge。
    if let Some(human_id) = node_map
        .iter()
        .find(|(_, n)| n.get("type").and_then(|t| t.as_str()) == Some("human"))
        .map(|(id, _)| id.clone())
    {
        app.emit(
            "wf:event",
            json!({
                "type": "warn",
                "id": human_id,
                "message": format!(
                    "检测到人工审批节点（{human_id}）：本地引擎不支持挂起/恢复，\
                     已自动整图委托 langgraph bridge 执行。审批挂起后节点会显示为「等待审批」。"
                ),
                "flowId": fid,
            }),
        )
        .ok();
        // input 传空串：start 节点的输出取自它自己的 data.input，与本地执行器一致
        return crate::bridge::run_via_langgraph(app, config, &wf, "", flow_id.clone())
            .await
            .map(|result| json!({ "result": result, "nodeOutputs": {} }));
    }

    // adjacency + in-degree for Kahn topological sort
    let mut adj: HashMap<String, Vec<String>> = HashMap::new();
    let mut indeg: HashMap<String, usize> = node_map.keys().map(|k| (k.clone(), 0)).collect();
    for e in &edges {
        let s = e["source"].as_str().unwrap_or("").to_string();
        let t = e["target"].as_str().unwrap_or("").to_string();
        if node_map.contains_key(&s) && node_map.contains_key(&t) {
            adj.entry(s).or_default().push(t.clone());
            if let Some(d) = indeg.get_mut(&t) {
                *d += 1;
            }
        }
    }

    let mut queue: VecDeque<String> = indeg
        .iter()
        .filter(|(_, &d)| d == 0)
        .map(|(k, _)| k.clone())
        .collect();
    let mut order: Vec<String> = Vec::new();
    let mut tmp = indeg.clone();
    while let Some(n) = queue.pop_front() {
        order.push(n.clone());
        if let Some(succs) = adj.get(&n) {
            for s in succs {
                if let Some(d) = tmp.get_mut(s) {
                    *d -= 1;
                    if *d == 0 {
                        queue.push_back(s.clone());
                    }
                }
            }
        }
    }
    if order.len() != node_map.len() {
        // Kahn 排序排不完全只可能是「有环」——孤立节点 in-degree 为 0，照样会被排出队列。
        // （旧文案写成「存在环或孤立节点」，把两种情形混为一谈，已修正）
        return Err(
            "workflow 存在环，无法拓扑排序。Rust 原生执行器只支持 DAG；\
             需要条件分支或循环，请把流程委托给 langgraph bridge 执行（tool 节点设 tool=\"langgraph\"）"
                .into(),
        );
    }

    let mut outputs: HashMap<String, String> = HashMap::new();
    let mut final_result = String::new();

    for id in &order {
        let node = node_map.get(id).unwrap();
        let ntype = node["type"].as_str().unwrap_or("").to_string();
        let data = &node["data"];

        // gather inputs from predecessor outputs
        let mut input_parts: Vec<String> = Vec::new();
        for e in &edges {
            if e["target"].as_str() == Some(id.as_str()) {
                let src = e["source"].as_str().unwrap_or("").to_string();
                if let Some(out) = outputs.get(&src) {
                    input_parts.push(out.clone());
                }
            }
        }
        let input = input_parts.join("\n---\n");

        app.emit(
            "wf:event",
            json!({ "type": "node:start", "id": id, "nodeType": ntype, "flowId": fid }),
        )
        .ok();

        let output = match ntype.as_str() {
            "start" => data["input"].as_str().unwrap_or("").to_string(),
            "llm" => {
                let prompt = data["prompt"].as_str().unwrap_or("").to_string();
                let user_msg = if input.is_empty() {
                    prompt
                } else {
                    format!("{prompt}\n\n[上游上下文]\n{input}")
                };
                let system = data["system"]
                    .as_str()
                    .unwrap_or("你是一个高效、可靠的 AI 助手。")
                    .to_string();
                // allow per-node model override of the global config
                let call_config = match data["model"].as_str().filter(|m| !m.is_empty()) {
                    Some(m) => {
                        let mut c = config.clone();
                        c.model = m.to_string();
                        c
                    }
                    None => config.clone(),
                };
                let mut collected = String::new();
                let res = crate::llm::chat_stream(&call_config, &system, &user_msg, |tok| {
                    collected.push_str(tok);
                    app.emit(
                        "wf:event",
                        json!({ "type": "token", "id": id, "token": tok, "flowId": fid }),
                    )
                    .ok();
                })
                .await;
                match res {
                    Ok(text) => text,
                    Err(e) => format!("[LLM 错误] {e}"),
                }
            }
            "condition" => {
                // Rust 原生执行器是确定性 DAG：condition 只做直通，且【所有】出边都会被执行。
                // 「按判据选一条边」由 langgraph bridge（S2，模型判定）承担；这里主动发 warn
                // 而不是静默直通 —— 否则用户会以为配了判据就生效了，其实是假接口。
                let out_degree = edges
                    .iter()
                    .filter(|e| e["source"].as_str() == Some(id.as_str()))
                    .count();
                if out_degree >= 2 {
                    app.emit(
                        "wf:event",
                        json!({
                            "type": "warn",
                            "id": id,
                            "message": format!(
                                "节点 {id} 是条件分支（{out_degree} 条出边），但 Rust 原生执行器只支持 DAG：\
                                 这里会直通并执行全部出边、不按判据选边。需要真正的分支路由，请把流程委托给 \
                                 langgraph bridge 执行（任一 tool 节点配置 tools 白名单 / mcp_tool / tool=\"langgraph\" 即可触发）。"
                            ),
                            "flowId": fid,
                        }),
                    )
                    .ok();
                }
                input.clone()
            }
            "tool" => {
                let tool = data["tool"].as_str().unwrap_or("unknown").to_string();
                let has_react_tools = data["tools"]
                    .as_array()
                    .map(|a| !a.is_empty())
                    .unwrap_or(false);
                let has_mcp_tool = data["mcp_tool"]
                    .as_str()
                    .map(|s| !s.is_empty())
                    .unwrap_or(false);
                // 路径 A 集成：tool 节点委托 langgraph-bridge 服务执行整图
                // 触发条件：显式 bridge 开关 / 配置了 ReAct 白名单 / 配置了 MCP 单工具
                if tool == "langgraph"
                    || data["bridge"].as_bool().unwrap_or(false)
                    || has_react_tools
                    || has_mcp_tool
                {
                    match crate::bridge::run_via_langgraph(
                        app,
                        config,
                        &wf,
                        &input,
                        flow_id.clone(),
                    )
                    .await
                    {
                        Ok(result) => {
                            // 标记 tool 节点完成；workflow:done 由 bridge 内部已 emit
                            app.emit(
                                "wf:event",
                                json!({ "type": "node:done", "id": id, "output": result, "flowId": fid }),
                            )
                            .ok();
                            return Ok(json!({ "result": result, "nodeOutputs": outputs }));
                        }
                        Err(e) => format!("[langgraph bridge 错误] {e}"),
                    }
                } else {
                    format!("[tool:{tool}] (占位实现) 收到输入:\n{input}")
                }
            }
            "end" => input.clone(),
            _ => input.clone(),
        };

        outputs.insert(id.clone(), output.clone());
        app.emit(
            "wf:event",
            json!({ "type": "node:done", "id": id, "output": output, "flowId": fid }),
        )
        .ok();

        if ntype == "end" {
            final_result = output;
        }
    }

    if final_result.is_empty() {
        final_result = outputs.values().last().cloned().unwrap_or_default();
    }

    app.emit(
        "wf:event",
        json!({ "type": "workflow:done", "result": final_result, "flowId": fid }),
    )
    .ok();

    Ok(json!({ "result": final_result, "nodeOutputs": outputs }))
}
