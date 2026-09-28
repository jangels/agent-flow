"""agent-flow 流程存储（SQLite）的共享只读访问层。

agent-flow 桌面应用（Rust）把流程与配置写在 SQLite 里：

    <data_dir>/agent_flow.db
      flows(id, name, version, graph, status, updated_at)   -- graph 为 JSON 文本
      kv(key, value)                                        -- active_id / config / legacy_workflow

本模块被两个 Python 侧消费者共用，避免重复实现：

  - server.py（bridge，HTTP / MCP 入口）      -> 按 flow_id 读图后执行
  - mcp_servers/flows_server.py（stdio MCP）  -> MCP 工具 list_published_flows / run_flow

并发说明：agent-flow(Rust) 与本侧(Python) 是两个进程访问同一 SQLite。数据库以 WAL
模式打开，本模块统一使用【只读连接】（URI 参数 mode=ro），读写互不阻塞。

路径解析（必须与 Rust 侧 `lib.rs::data_dir()` 完全一致）：
  1. 环境变量 AGENT_FLOW_DB    —— 直接指定 .db 文件（支持 ~ 展开）
  2. 环境变量 AGENT_FLOW_HOME  —— 指定数据目录（相对路径按 cwd 解析）
  3. 默认                       —— ~/.agent-flow/agent_flow.db

刻意【不依赖系统 app 数据目录】（macOS `~/Library/Application Support`、
Windows `%APPDATA%`、Linux `$XDG_CONFIG_HOME`）——那些位置平台各异、藏得深，
Rust 与 Python 两边都得各自实现一遍平台分支才能对齐。固定目录只需一个常量。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

DB_NAME = "agent_flow.db"
DATA_DIR_NAME = ".agent-flow"

FLOW_COLS = "id, name, version, graph, status, updated_at"


def default_data_dir() -> Path:
    """数据目录：`AGENT_FLOW_HOME` 优先，否则 `<用户主目录>/.agent-flow`。"""
    raw = os.environ.get("AGENT_FLOW_HOME")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path.home() / DATA_DIR_NAME


def default_db_path() -> Path:
    """数据目录下的数据库路径。"""
    return default_data_dir() / DB_NAME


def db_path() -> Path:
    """实际使用的数据库路径（`AGENT_FLOW_DB` 优先，支持 ~ 展开）。"""
    raw = os.environ.get("AGENT_FLOW_DB")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return default_db_path()


def db_exists() -> bool:
    return db_path().exists()


def connect() -> sqlite3.Connection:
    """只读连接：WAL 模式下与 agent-flow 的写入互不阻塞。"""
    uri = f"file:{db_path()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql: str, args: tuple = ()) -> list[dict]:
    """执行只读查询；库不存在或查询失败时返回空列表（不抛异常，便于降级提示）。"""
    if not db_exists():
        return []
    try:
        with closing(connect()) as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def list_flows(published_only: bool = True) -> list[dict]:
    """流程元信息列表（不含 graph，避免响应过大）。"""
    where = "WHERE status='published'" if published_only else ""
    return query(f"SELECT {FLOW_COLS} FROM flows {where} ORDER BY updated_at DESC")


def get_flow(flow_id: str) -> dict | None:
    """按 id 取单条流程（含 graph 字段）。"""
    rows = query(f"SELECT {FLOW_COLS} FROM flows WHERE id=?", (flow_id,))
    return rows[0] if rows else None


def graph_of(flow: dict) -> dict:
    """把 flow.graph（可能是 dict 或 JSON 文本）规整为 {nodes, edges} 字典。"""
    g = flow.get("graph")
    if isinstance(g, dict):
        return g
    if isinstance(g, str) and g.strip():
        try:
            return json.loads(g)
        except Exception:  # noqa: BLE001
            return {}
    return {}


def stored_config() -> dict:
    """读取 app 里配置好的 LLM 配置（kv['config']）。

    返回 {base_url, api_key, model, temperature}；未配置时返回空 dict
    （调用方据此决定是走真实模型还是零 key 的 MockChatModel）。
    """
    rows = query("SELECT value FROM kv WHERE key='config'")
    if not rows:
        return {}
    try:
        cfg = json.loads(rows[0]["value"])
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(cfg, dict):
        return {}
    out = {
        "base_url": cfg.get("base_url", ""),
        "api_key": cfg.get("api_key", ""),
        "model": cfg.get("model", "") or "gpt-4o-mini",
        "temperature": cfg.get("temperature", 0.7),
    }
    # 没有 key 就没有意义，直接视作未配置（让调用方走 Mock）
    return out if out["api_key"] else {}
