CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    session_name TEXT NOT NULL,
    last_sequence INTEGER NOT NULL,
    last_msg_timestamp timestamp NOT NULL
);