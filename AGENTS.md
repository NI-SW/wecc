# AGENTS.md

Workspace guidance for future ZCode agents. Project-specific facts only.

## What this is

A pipeline that turns **企业微信 (WeCom) group-chat history** into structured problem-summaries. It reads locally-decrypted WeCom databases, slices messages by day, sends each day to an LLM "agent" (OpenAI-compatible API) with a prompt template, parses the JSON response, and writes one summary file per session-day. Summaries can then be uploaded to a "DataCop" platform.

## Repository layout

| Path | Role |
|------|------|
| `main.py` | Orchestrator: read WeCom DBs → group by day → call agent API → parse JSON → save summary → track progress. Uses **stdlib only** (urllib, sqlite3) + `wecom_reader`. |
| `wecom-reader/` | Nested library providing `WeComReader` (decrypts & reads WeCom local SQLite). **Its own git repo**, tracked as a gitlink — see gotcha below. |
| `summary/upload.py` | Uploads summary JSONs to the DataCop platform (uses `requests`). Reads `summary/upload.env`. Skips problems whose `solution` is empty (same placeholder rule as `main.py`'s `_field_filled`); `--cleanup` deletes DataCop's pending (`待定`) problems with empty `solution`. |
| `prompt.md` | LLM prompt template — defines the exact JSON problem-summary schema (`name`, `description`, `scenario`, `trigger_method`, `symptoms`, `cause`, `solution`, `verification`, `notes`, `status`). Changing this changes output structure. |
| `conversation.list` | Group-chat names to process, one per line. Edit to select which groups are summarized. |
| `initdb.sql` | Schema for the progress DB (`sessions` table). Applied lazily by `main.py`. |
| `data/wecom.db` | Runtime SQLite tracking per-session progress (`last_sequence`, `last_msg_timestamp`). Gitignored (`*.db`). |
| `data/*.log` | Run logs. |
| `API.md` | DataCop REST API reference (consumed by `upload.py`). |
| `.env` | `AGENT_URL`, `AGENT_API_KEY`, `WXWORK_DIR` (gitignored). See `.env.example`. |
| `summary/upload.env` | `API_URL`, `USERNAME`, `PASSWORD`, `PROJECT_ID` for DataCop (gitignored). |

## Running

- **Interpreter:** the project venv is `.venv` (Python 3.11). Use `.venv/Scripts/python.exe`; the bare `python`/`python3` on PATH hits the Windows Store stub and fails. The `py` launcher works (`py --version`).
- `wecom-reader` is installed **editable** in the venv (egg-info → local `wecom-reader/` dir), so edits to the nested package take effect without reinstall.
- **Main run:** `python main.py` — see `python main.py -h` for flags (`-d/--days`, `-l/--limit`, `-o/--output-dir`, `--dry-run`, `--temperature`, `--agent-url`, `--decrypted-dir`, `--db-dir`, `--wxwork-dir`). Config priority: CLI args > `.env` > defaults.
- **Upload:** `cd summary && python upload.py [--dry-run] [files...]`. Problems with an empty `solution` (empty or placeholder-only, e.g. 无/暂无/待定) are never uploaded. Data cleanup: `python upload.py --cleanup [--dry-run]` sweeps every visible project and deletes pending problems with empty `solution`.
- **Scratch decrypt test:** `parsedb.py` (throwaway script, not part of the pipeline).

There is no lint/typecheck/test setup, no `requirements.txt` (deps live in `wecom-reader/pyproject.toml` + editable install), and no CI. The venv already has `pycryptodomex`, `click`, `Flask`, `requests`, `wecom-reader`.

## Data flow & boundaries

`WeComReader` (in `wecom-reader/`) owns **all** WeCom DB access and decryption. `main.py` never opens WeCom SQLite directly — it calls `reader.list_sessions / get_messages / get_image_path / download_image`. The only DB `main.py` writes itself is the small progress DB at `data/wecom.db`, which holds two tables: `sessions` (per-session fetch progress) and `cases` (cross-day problem-case state).

Per-session loop: fetch new messages since last progress → filter to `sequence > last_sequence` → group by **Beijing** day → for each day, merge if it overlaps the prior progress day → build prompt (chat text + **injected open backlog** + `prompt.md`) → call agent → parse → save to `summary/<YYMMDD>/summary_<safe_name>.json` → **upsert cases table** → advance progress **only on success** (failures resume next run).

### Cross-day problem continuity (`cases` table)

A problem may span multiple days (reported Mon, diagnosed Tue, solved Wed). To let each day's summary see prior context, `main.py` maintains a `cases` table keyed by `(session_id, name)`:

- **Before** calling the agent for a day, `fetch_open_cases` reads `status='open'` cases for that session (excluding today) and `build_open_cases_block` injects a *trimmed* view (name + description + symptoms + cause, **not** solution/verification) into the prompt. The prompt instructs the agent to keep `name` stable for ongoing problems and update fields; only brand-new problems get a fresh name. Name stability is what makes the eventual aggregation key work.
- **After** a successful parse, `upsert_cases` writes each output case by `(session_id, name)`: new rows inserted, existing rows overwritten with the latest content.
- `status` is an **internal pipeline field** (`open`/`resolved`), decided by `case_status_from_summary` — a **heuristic**, not agent output: `solution` **and** `verification` both non-empty and non-placeholder ⇒ `resolved`, else `open`. It is deliberately conservative (prefer re-injecting an extra round over breaking a chain). `status` never appears in the summary JSON and is never uploaded to DataCop (DataCop has its own `pending`/`valid` review status). The 9 summary fields are exactly what `prompt.md` defines; `upload.py` only forwards those 9.
- `resolved` cases stop being injected but stay in the table for upload/traceability. The table's final per-`name` row is the authoritative "one case" for that problem.

## Conventions

- Docstrings/comments and many data values are **Chinese**; identifiers are English. Preserve this when editing.
- `main.py` networking uses `urllib` (stdlib), not `requests` — keep it dependency-free.
- Type hints use Python 3.10+ syntax (`list[dict]`, `X | None`).
- Message `send_time` is a **UTC epoch** (seconds). `wecom-reader` stores/returns it raw, no timezone conversion.
- **Day slicing uses Beijing time (UTC+8)** so a "day" matches the users' natural day, not the UTC day whose cutoff falls at 08:00 Beijing. `main.py` defines `CST = timezone(timedelta(hours=8))` and uses it for `group_messages_by_day`, `format_messages_for_prompt`, and the merge-recompute day boundaries. The progress DB still stores timestamps as `"%Y-%m-%d %H:%M:%S"` strings interpreted as **UTC** (progress points are instants, not day labels) — keep that split: progress = UTC instant, day labels = Beijing.
- **Incremental fetch is ascending.** `main.py` calls `reader.get_messages(..., ascending=True)` so the oldest unprocessed batch comes back first; progress advances to the max processed sequence and the rest is picked up next run. Never switch the main fetch back to `ascending=False` (newest-first) — that silently drops the oldest messages when a day exceeds `--limit` and advances progress past them. The merge-recompute fetch also uses `ascending=True` with a large `MERGE_FETCH_LIMIT` for the same reason.
- **`sequence` and `send_time` can be out of order.** WeCom's `sequence` is the insert order; `send_time` is the send instant. A message delayed at ingest has a larger `sequence` but an *earlier* `send_time` than an already-processed one. Incremental fetch therefore filters by **`after_sequence` (sequence > last_sequence)**, NOT `since` (send_time >= last_ts) — `since` would skip those reordered messages and, since progress then advances past them, lose them permanently. `since`/`until` are only used for the **first-run lookback window** and the **merge-recompute full-day range** (where a time range is semantically correct). Don't "simplify" the incremental fetch back to a single `since` filter.
- Output filenames: session name is sanitized (special chars → `_`, truncated to 80 chars); date dir is `YYMMDD` derived from the **Beijing** date (`2026-08-14` → `260814`).

## Gotchas

- **`wecom-reader` is a nested git repo, not a registered submodule.** There is no `.gitmodules`; the parent records a gitlink. `git submodule status` errors. To commit changes inside it, commit in the nested repo first, then the parent will show `M wecom-reader` for the updated pointer. `git status` in the parent will not show individual file changes under `wecom-reader/`.
- **WeCom decryption is Windows-only** and requires running as **admin** with **WXWork.exe running** (key is pulled from its process memory). `--decrypted-dir` defaults to `<脚本目录>/wecom-reader/wxwork_decrypted` (relative, machine-independent). Re-decryption rules in `main.py`: explicit `--db-dir` forces a full re-decrypt; otherwise `main.py` auto-re-decrypts (incrementally) when `message.db`/`session.db`/`user.db` in the source (`--db-dir` or `WXWORK_DIR/Data`) are newer than the decrypted copies. A missing/partial decrypt fails loudly (it used to silently yield "匹配到 0 个会话").
- **The agent is agentic.** It may emit intermediate reasoning ("我先检索知识库…") before the final JSON. `parse_agent_response` + a continuation loop (`max_continuations=3`) handle this. `_extract_json_candidates` prefers later ```json blocks; `repair_json_quotes` fixes unescaped inner quotes; `_parse_concatenated_objects` handles `{..}{..}` output. If you touch parsing, keep all three fallbacks.
- **Images:** two sources — CDN URL (type=4) and inline bytes (type=36). Base64 inline payload is hard-capped at ~8MB total; excess images are skipped. Falls back to the raw URL if encoding/download fails.
- **`.env` and `summary/upload.env` are gitignored and hold real secrets.** `.env.example` shows the shape; never commit values.
- `main.py.bak` is a stale backup (differs from `main.py`); `zh-6.7.20.jar` is an untracked binary — leave both alone unless explicitly asked.
