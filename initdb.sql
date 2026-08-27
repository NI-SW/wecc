CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    session_name TEXT NOT NULL,
    last_sequence INTEGER NOT NULL,
    last_msg_timestamp TEXT NOT NULL
);

-- 跨天问题案例的状态存储：每个会话每条问题一行，按 name 去重。
-- status 是管线内部字段（open/resolved），不进 summary、不传 DataCop，
-- 仅用于决定次日 prompt 是否注入该问题。
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open / resolved
    last_seen_date TEXT NOT NULL,          -- 北京日期 YYYY-MM-DD
    updated_at TEXT NOT NULL,              -- "%Y-%m-%d %H:%M:%S" UTC
    summary_json TEXT NOT NULL,            -- 该问题最新一条完整 case 的 JSON
    UNIQUE(session_id, name)
);
