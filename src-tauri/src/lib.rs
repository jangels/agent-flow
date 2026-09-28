mod llm;
mod workflow;
mod bridge;

use std::sync::Mutex;

use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::Manager;

#[derive(Clone, Serialize, Deserialize)]
struct AppConfig {
    base_url: String,
    api_key: String,
    model: String,
    temperature: f32,
}

impl Default for AppConfig {
    fn default() -> Self {
        AppConfig {
            base_url: "https://api.openai.com/v1".to_string(),
            api_key: String::new(),
            model: "gpt-4o-mini".to_string(),
            temperature: 0.7,
        }
    }
}

struct AppState {
    config: Mutex<AppConfig>,
}

// ---------------------------------------------------------------------------
// 多流程模型：一个 Flow = 一个可独立编辑 / 发布 / 执行的 DAG
// ---------------------------------------------------------------------------

fn one() -> u32 {
    1
}
fn draft_status() -> String {
    "draft".to_string()
}

#[derive(Clone, Serialize, Deserialize)]
struct Flow {
    id: String,
    name: String,
    #[serde(default = "one")]
    version: u32,
    #[serde(default)]
    graph: Value,
    /// "draft" | "published"
    #[serde(default = "draft_status")]
    status: String,
    #[serde(default)]
    updated_at: String,
}

/// 旧版 JSON 存储的集合结构 —— 仅用于一次性迁移到 SQLite。
#[derive(Clone, Serialize, Deserialize, Default)]
struct FlowStore {
    #[serde(default)]
    flows: Vec<Flow>,
    #[serde(default)]
    active_id: String,
}

// ---------------------------------------------------------------------------
// 数据目录
//
// 刻意【不依赖系统 app 数据目录】（macOS `~/Library/Application Support`、
// Windows `%APPDATA%`、Linux `$XDG_CONFIG_HOME`）——那些位置平台各异、藏得深，
// 外部工具（bridge / MCP server / sqlite3 / 备份脚本）要找数据得各自猜一遍。
//
// 统一放在用户主目录下的固定目录，一处可查、跨平台一致、可被直接读取：
//
//     ~/.agent-flow/agent_flow.db     流程 + 配置（SQLite）
//     ~/.agent-flow/exports/          导出的流程 JSON
//
// 可用环境变量 AGENT_FLOW_HOME 覆盖整个目录（相对路径按进程 cwd 解析）。
// Python 侧（flow_store.py）必须使用同一变量与同一默认值，两端靠此对齐。
// ---------------------------------------------------------------------------

const DATA_DIR_NAME: &str = ".agent-flow";

/// 数据目录解析的**纯逻辑**部分（与构建环境无关，便于单测 + 与 Python 侧对齐）：
/// `AGENT_FLOW_HOME` 非空则用它，否则 `<home>/.agent-flow`。
fn resolve_data_dir(home: &std::path::Path, override_home: Option<&str>) -> std::path::PathBuf {
    if let Some(raw) = override_home {
        let raw = raw.trim();
        if !raw.is_empty() {
            return std::path::PathBuf::from(raw);
        }
    }
    home.join(DATA_DIR_NAME)
}

/// 数据目录：`AGENT_FLOW_HOME` 优先，否则 `<用户主目录>/.agent-flow`。
fn data_dir(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    let home = app
        .path()
        .home_dir()
        .map_err(|e| format!("cannot resolve home dir: {e}"))?;
    let override_home = std::env::var("AGENT_FLOW_HOME").ok();
    Ok(resolve_data_dir(&home, override_home.as_deref()))
}

/// 数据目录下的文件路径（顺带确保目录存在）。
fn data_file(app: &tauri::AppHandle, name: &str) -> Result<std::path::PathBuf, String> {
    let dir = data_dir(app)?;
    std::fs::create_dir_all(&dir).map_err(|e| format!("create data dir failed: {e}"))?;
    Ok(dir.join(name))
}

/// 旧的「系统配置目录」——**仅用于一次性迁移**，日常读写一律走 `data_dir`。
fn legacy_config_dir(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    app.path().app_config_dir().ok()
}

fn now_ms() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis().to_string())
        .unwrap_or_else(|_| "0".to_string())
}

fn new_id() -> String {
    let ns = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("flow_{ns}")
}

fn empty_graph() -> Value {
    json!({ "nodes": [], "edges": [] })
}

// ---------------------------------------------------------------------------
// SQLite 存储层
//
// 数据库：<data_dir>/agent_flow.db   →  默认 ~/.agent-flow/agent_flow.db
//   flows(id PK, name, version, graph JSON, status, updated_at)
//   kv(key PK, value)   -- active_id / config / legacy_workflow
//
// 开启 WAL + busy_timeout：app(Rust) 与 MCP server(Python) 是两个进程，
// 同时读写同一份数据，WAL 让「读不阻塞写」，busy_timeout 自动等锁。
// ---------------------------------------------------------------------------

const SCHEMA: &str = "
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS flows (
    id         TEXT    PRIMARY KEY,
    name       TEXT    NOT NULL DEFAULT '',
    version    INTEGER NOT NULL DEFAULT 1,
    graph      TEXT    NOT NULL DEFAULT '{\"nodes\":[],\"edges\":[]}',
    status     TEXT    NOT NULL DEFAULT 'draft',
    updated_at TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
";

fn db_file(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    data_file(app, "agent_flow.db")
}

/// 若新数据目录还没有库、而旧系统目录里已经有一个，则整体搬过来（一次性）。
/// 必须在 `Connection::open` **之前**调用——连接一旦建立就不能再换文件。
fn import_legacy_db(app: &tauri::AppHandle, target: &std::path::Path) {
    if target.exists() {
        return;
    }
    let Some(legacy) = legacy_config_dir(app) else {
        return;
    };
    let old = legacy.join("agent_flow.db");
    if !old.exists() {
        return;
    }
    if std::fs::copy(&old, target).is_ok() {
        // WAL 边车文件尽力一并搬（WAL 里可能有尚未 checkpoint 的数据）。
        for suffix in ["-wal", "-shm"] {
            let from = legacy.join(format!("agent_flow.db{suffix}"));
            if from.exists() {
                let to = std::path::PathBuf::from(format!("{}{suffix}", target.display()));
                let _ = std::fs::copy(&from, &to);
            }
        }
    }
}

/// 打开数据库连接：建表 + WAL + （首次）迁移旧 JSON。
fn open_db(app: &tauri::AppHandle) -> Result<Connection, String> {
    let p = db_file(app)?;
    import_legacy_db(app, &p);
    let conn = Connection::open(&p).map_err(|e| format!("open sqlite failed: {e}"))?;
    conn.execute_batch(SCHEMA)
        .map_err(|e| format!("init schema failed: {e}"))?;
    migrate_legacy(app, &conn)?;
    Ok(conn)
}

fn row_to_flow(row: &rusqlite::Row<'_>) -> rusqlite::Result<Flow> {
    let graph_s: String = row.get("graph")?;
    Ok(Flow {
        id: row.get("id")?,
        name: row.get("name")?,
        version: row.get("version")?,
        graph: serde_json::from_str(&graph_s).unwrap_or_else(|_| empty_graph()),
        status: row.get("status")?,
        updated_at: row.get("updated_at")?,
    })
}

fn get_kv(conn: &Connection, key: &str) -> Option<String> {
    conn.query_row("SELECT value FROM kv WHERE key=?1", params![key], |r| {
        r.get(0)
    })
    .ok()
}

fn set_kv(conn: &Connection, key: &str, value: &str) -> Result<(), String> {
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?1,?2)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        params![key, value],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn get_flow(conn: &Connection, id: &str) -> Option<Flow> {
    conn.query_row("SELECT * FROM flows WHERE id=?1", params![id], row_to_flow)
        .ok()
}

fn all_flows(conn: &Connection) -> Vec<Flow> {
    let mut out = Vec::new();
    if let Ok(mut stmt) = conn.prepare("SELECT * FROM flows ORDER BY updated_at DESC") {
        if let Ok(rows) = stmt.query_map([], row_to_flow) {
            for r in rows.flatten() {
                out.push(r);
            }
        }
    }
    out
}

fn upsert_flow(conn: &Connection, f: &Flow) -> Result<(), String> {
    let graph = serde_json::to_string(&f.graph).map_err(|e| e.to_string())?;
    conn.execute(
        "INSERT INTO flows(id,name,version,graph,status,updated_at) VALUES(?1,?2,?3,?4,?5,?6)
         ON CONFLICT(id) DO UPDATE SET name=excluded.name, version=excluded.version,
             graph=excluded.graph, status=excluded.status, updated_at=excluded.updated_at",
        params![f.id, f.name, f.version, graph, f.status, f.updated_at],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

/// 把已迁移的旧 JSON 文件改名，避免下次重复导入（保留原数据备查）。
fn mark_migrated(p: &std::path::Path) {
    let mut to = p.as_os_str().to_owned();
    to.push(".migrated");
    let _ = std::fs::rename(p, std::path::PathBuf::from(to));
}

/// 首次运行：把旧系统目录里的 flows.json / workflow.json / config.json 导入 SQLite。
/// 源目录是旧的 `app_config_dir`——这里只是**读旧数据**，不构成对系统路径的依赖。
fn migrate_legacy(app: &tauri::AppHandle, conn: &Connection) -> Result<(), String> {
    let Some(legacy) = legacy_config_dir(app) else {
        return Ok(());
    };
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM flows", [], |r| r.get(0))
        .unwrap_or(0);

    // 1) 旧的多流程集合 flows.json
    if count == 0 {
        let p = legacy.join("flows.json");
        if let Ok(s) = std::fs::read_to_string(&p) {
            if let Ok(st) = serde_json::from_str::<FlowStore>(&s) {
                if !st.flows.is_empty() {
                    for f in &st.flows {
                        let _ = upsert_flow(conn, f);
                    }
                    if !st.active_id.is_empty() {
                        let _ = set_kv(conn, "active_id", &st.active_id);
                    }
                    mark_migrated(&p);
                }
            }
        }
    }

    // 2) 更早的单流程 workflow.json -> 一条 published 流程
    let count2: i64 = conn
        .query_row("SELECT COUNT(*) FROM flows", [], |r| r.get(0))
        .unwrap_or(0);
    if count2 == 0 {
        let p = legacy.join("workflow.json");
        if let Ok(s) = std::fs::read_to_string(&p) {
            if let Ok(g) = serde_json::from_str::<Value>(&s) {
                if g["nodes"].as_array().map(|a| !a.is_empty()).unwrap_or(false) {
                    let f = Flow {
                        id: new_id(),
                        name: "默认流程".to_string(),
                        version: 1,
                        graph: g,
                        status: "published".to_string(),
                        updated_at: now_ms(),
                    };
                    let _ = upsert_flow(conn, &f);
                    let _ = set_kv(conn, "active_id", &f.id);
                    mark_migrated(&p);
                }
            }
        }
    }

    // 3) config.json -> kv("config")
    if get_kv(conn, "config").is_none() {
        let p = legacy.join("config.json");
        if let Ok(s) = std::fs::read_to_string(&p) {
            if serde_json::from_str::<AppConfig>(&s).is_ok() {
                let _ = set_kv(conn, "config", &s);
                mark_migrated(&p);
            }
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

fn load_config(app: &tauri::AppHandle) -> AppConfig {
    if let Ok(conn) = open_db(app) {
        if let Some(s) = get_kv(&conn, "config") {
            if let Ok(c) = serde_json::from_str::<AppConfig>(&s) {
                return c;
            }
        }
    }
    AppConfig::default()
}

fn save_config(app: &tauri::AppHandle, cfg: &AppConfig) {
    if let Ok(conn) = open_db(app) {
        if let Ok(s) = serde_json::to_string(cfg) {
            let _ = set_kv(&conn, "config", &s);
        }
    }
}

#[tauri::command]
fn get_config(state: tauri::State<AppState>) -> AppConfig {
    state.config.lock().unwrap().clone()
}

#[tauri::command]
fn get_bridge_url() -> String {
    // 与 bridge.rs / start.sh 共用同一环境变量：聊天分享链接、Rust 调 bridge 都对齐到这里。
    // 默认 8010（与 start.sh 的 AGENT_FLOW_BRIDGE_PORT 一致），即使该变量未设置也能拼出正确链接。
    std::env::var("LANGGRAPH_BRIDGE_URL")
        .map(|u| u.trim_end_matches('/').to_string())
        .unwrap_or_else(|_| "http://127.0.0.1:8010".to_string())
}

#[tauri::command]
fn set_config(
    app: tauri::AppHandle,
    state: tauri::State<AppState>,
    config: AppConfig,
) -> Result<(), String> {
    {
        let mut c = state
            .config
            .lock()
            .map_err(|_| "config lock poisoned".to_string())?;
        *c = config.clone();
    }
    save_config(&app, &config);
    Ok(())
}

// ---------------------------------------------------------------------------
// 流程 CRUD / 发布 / 执行 / 导出
// ---------------------------------------------------------------------------

#[tauri::command]
fn list_flows(app: tauri::AppHandle) -> Vec<Flow> {
    open_db(&app).map(|c| all_flows(&c)).unwrap_or_default()
}

#[tauri::command]
fn get_active_flow(app: tauri::AppHandle) -> Result<Flow, String> {
    let conn = open_db(&app)?;
    let active = get_kv(&conn, "active_id").unwrap_or_default();
    get_flow(&conn, &active)
        .or_else(|| all_flows(&conn).into_iter().next())
        .ok_or_else(|| "还没有任何流程".to_string())
}

#[tauri::command]
fn create_flow(app: tauri::AppHandle, name: String, graph: Value) -> Flow {
    let f = Flow {
        id: new_id(),
        name: if name.trim().is_empty() {
            "未命名流程".to_string()
        } else {
            name
        },
        version: 1,
        graph,
        status: "draft".to_string(),
        updated_at: now_ms(),
    };
    if let Ok(conn) = open_db(&app) {
        let _ = upsert_flow(&conn, &f);
        let _ = set_kv(&conn, "active_id", &f.id);
    }
    f
}

#[tauri::command]
fn save_flow(app: tauri::AppHandle, flow: Flow) -> Result<Flow, String> {
    let conn = open_db(&app)?;
    let mut f = flow;
    f.updated_at = now_ms();
    upsert_flow(&conn, &f)?;
    Ok(f)
}

#[tauri::command]
fn delete_flow(app: tauri::AppHandle, id: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    conn.execute("DELETE FROM flows WHERE id=?1", params![id])
        .map_err(|e| e.to_string())?;
    let active = get_kv(&conn, "active_id").unwrap_or_default();
    if active == id {
        let next: Option<String> = conn
            .query_row(
                "SELECT id FROM flows ORDER BY updated_at DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())?;
        set_kv(&conn, "active_id", &next.unwrap_or_default())?;
    }
    Ok(())
}

#[tauri::command]
fn set_active_flow(app: tauri::AppHandle, id: String) -> Result<(), String> {
    let conn = open_db(&app)?;
    set_kv(&conn, "active_id", &id)
}

#[tauri::command]
fn publish_flow(app: tauri::AppHandle, id: String, published: bool) -> Result<Flow, String> {
    let conn = open_db(&app)?;
    let mut f = get_flow(&conn, &id).ok_or_else(|| "流程不存在".to_string())?;
    if published {
        f.status = "published".to_string();
        f.version += 1;
    } else {
        f.status = "draft".to_string();
    }
    f.updated_at = now_ms();
    upsert_flow(&conn, &f)?;
    Ok(f)
}

#[tauri::command]
fn export_flow(app: tauri::AppHandle, id: String) -> Result<String, String> {
    let conn = open_db(&app)?;
    let f = get_flow(&conn, &id).ok_or_else(|| "流程不存在".to_string())?;
    let dir = data_dir(&app)?.join("exports");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let safe: String = f
        .name
        .chars()
        .map(|c| if c.is_alphanumeric() || c == '_' || c == '-' { c } else { '_' })
        .collect();
    let path = dir.join(format!("{}-{}.json", safe, f.id));
    let s = serde_json::to_string_pretty(&f).map_err(|e| e.to_string())?;
    std::fs::write(&path, s).map_err(|e| e.to_string())?;
    Ok(path.to_string_lossy().to_string())
}

#[tauri::command]
async fn run_flow(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    id: String,
) -> Result<Value, String> {
    let flow = {
        let conn = open_db(&app)?;
        get_flow(&conn, &id).ok_or_else(|| "流程不存在".to_string())?
    };
    let config = state
        .config
        .lock()
        .map_err(|_| "config lock poisoned".to_string())?
        .clone();
    workflow::execute(&app, &config, flow.graph, Some(flow.id)).await
}

/// 恢复一个「等待人工审批」而挂起的运行（HITL 续跑）。
///
/// 必须走 langgraph bridge：挂起现场（checkpoint）在 bridge 侧的 checkpointer 里，
/// Rust 原生执行器是确定性 DAG，没有中断/恢复能力。所以这里不经 workflow::execute，
/// 直接把 decision 交给 bridge，由它用 `Command(resume=...)` + 同一 thread_id 续跑。
#[tauri::command]
async fn resume_flow(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    id: String,
    thread_id: String,
    decision: Value,
) -> Result<String, String> {
    if thread_id.trim().is_empty() {
        return Err("缺少 threadId —— 挂起时由 node:awaiting 事件带出，续跑必须原样回传".into());
    }
    {
        // 存在性前置校验：否则用户拿到的是 bridge 的 404 文本，不如这里直说
        let conn = open_db(&app)?;
        get_flow(&conn, &id).ok_or_else(|| "流程不存在".to_string())?;
    }
    let config = state
        .config
        .lock()
        .map_err(|_| "config lock poisoned".to_string())?
        .clone();
    crate::bridge::resume_via_langgraph(&app, &config, &id, &thread_id, decision).await
}

// ---------------------------------------------------------------------------
// 兼容旧接口（单流程直接执行 / 存读），保留避免破坏老调用。
// 存储同样走 SQLite（kv 表），不再写 workflow.json。
// ---------------------------------------------------------------------------

#[tauri::command]
fn save_workflow(app: tauri::AppHandle, workflow: Value) -> Result<(), String> {
    let conn = open_db(&app)?;
    let s = serde_json::to_string(&workflow).map_err(|e| e.to_string())?;
    set_kv(&conn, "legacy_workflow", &s)
}

#[tauri::command]
fn load_workflow(app: tauri::AppHandle) -> Result<Value, String> {
    let conn = open_db(&app)?;
    match get_kv(&conn, "legacy_workflow") {
        Some(s) => serde_json::from_str(&s).map_err(|e| e.to_string()),
        None => Ok(empty_graph()),
    }
}

#[tauri::command]
async fn run_workflow(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    workflow: Value,
) -> Result<Value, String> {
    let config = state
        .config
        .lock()
        .map_err(|_| "config lock poisoned".to_string())?
        .clone();
    workflow::execute(&app, &config, workflow, None).await
}

// ---------------------------------------------------------------------------
// 测试：数据目录解析必须与 Python 侧 flow_store.py 完全一致
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn data_dir_defaults_to_home_dot_agent_flow() {
        let d = resolve_data_dir(Path::new("/Users/example"), None);
        assert_eq!(d, Path::new("/Users/example/.agent-flow"));

        // 空串 / 全空白视作「未设置」，仍回落到默认目录
        assert_eq!(
            resolve_data_dir(Path::new("/Users/example"), Some("   ")),
            Path::new("/Users/example/.agent-flow")
        );
    }

    #[test]
    fn data_dir_respects_env_override() {
        let d = resolve_data_dir(Path::new("/Users/example"), Some("/tmp/af-home"));
        assert_eq!(d, Path::new("/tmp/af-home"));

        // 相对路径按原样返回（Rust 侧不做 expanduser；绝对路径是推荐用法）
        let rel = resolve_data_dir(Path::new("/Users/example"), Some("agent-flow-data"));
        assert_eq!(rel, Path::new("agent-flow-data"));
    }

    #[test]
    fn data_dir_name_matches_python_side() {
        // 两端共用的常量：改这里必须同步改 flow_store.py 的 DATA_DIR_NAME
        assert_eq!(DATA_DIR_NAME, ".agent-flow");
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let cfg = load_config(&handle);
            app.manage(AppState {
                config: Mutex::new(cfg),
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            run_workflow,
            save_workflow,
            load_workflow,
            get_config,
            set_config,
            list_flows,
            get_active_flow,
            create_flow,
            save_flow,
            delete_flow,
            set_active_flow,
            publish_flow,
            export_flow,
            run_flow,
            resume_flow,
            get_bridge_url
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
