# AGENTS.md

Workspace guidance for future ZCode agents. Project-specific facts only.

## What this is

A pipeline that turns **企业微信 (WeCom) group-chat history** into structured problem-summaries. It reads locally-decrypted WeCom databases, slices messages by day, sends each day to an LLM "agent" (OpenAI-compatible API) with a prompt template, parses the JSON response, and writes one summary file per session-day. Summaries can then be uploaded to a "DataCop" platform.

## Repository layout

| Path | Role |
|------|------|
| `main.py` | Orchestrator: read WeCom DBs → group by day → call agent API → parse JSON → save summary → track progress. Uses **stdlib only** (urllib, sqlite3) + `wecom_reader`. |
| `wecom-reader/` | Nested library providing `WeComReader` (decrypts & reads WeCom local SQLite). **Its own git repo**, tracked as a gitlink — see gotcha below. |
| `summary/upload.py` | Uploads summary JSONs to the DataCop platform (uses `requests`). Reads `summary/upload.env`. |
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
- **Upload:** `cd summary && python upload.py [--dry-run] [files...]`
- **Scratch decrypt test:** `parsedb.py` (throwaway script, not part of the pipeline).

There is no lint/typecheck/test setup, no `requirements.txt` (deps live in `wecom-reader/pyproject.toml` + editable install), and no CI. The venv already has `pycryptodomex`, `click`, `Flask`, `requests`, `wecom-reader`.

## Data flow & boundaries

`WeComReader` (in `wecom-reader/`) owns **all** WeCom DB access and decryption. `main.py` never opens WeCom SQLite directly — it calls `reader.list_sessions / get_messages / get_image_path / download_image`. The only DB `main.py` writes itself is the small progress DB at `data/wecom.db`.

Per-session loop: fetch new messages since last progress → filter to `sequence > last_sequence` → group by UTC day → for each day, merge if it overlaps the prior progress day → build prompt (chat text + `prompt.md`) → call agent → parse → save to `summary/<YYMMDD>/summary_<safe_name>.json` → advance progress **only on success** (failures resume next run).

## Conventions

- Docstrings/comments and many data values are **Chinese**; identifiers are English. Preserve this when editing.
- `main.py` networking uses `urllib` (stdlib), not `requests` — keep it dependency-free.
- Type hints use Python 3.10+ syntax (`list[dict]`, `X | None`).
- Timestamps are consistently **UTC** (`datetime.fromtimestamp(..., tz=timezone.utc)`). The progress DB stores them as `"%Y-%m-%d %H:%M:%S"` strings interpreted as UTC. Don't mix in local time.
- Output filenames: session name is sanitized (special chars → `_`, truncated to 80 chars); date dir is `YYMMDD` (`2026-08-14` → `260809`).

## Gotchas

- **`wecom-reader` is a nested git repo, not a registered submodule.** There is no `.gitmodules`; the parent records a gitlink. `git submodule status` errors. To commit changes inside it, commit in the nested repo first, then the parent will show `M wecom-reader` for the updated pointer. `git status` in the parent will not show individual file changes under `wecom-reader/`.
- **WeCom decryption is Windows-only** and requires running as **admin** with **WXWork.exe running** (key is pulled from its process memory). `--decrypted-dir` defaults to `<脚本目录>/wecom-reader/wxwork_decrypted` (relative, machine-independent). Re-decryption rules in `main.py`: explicit `--db-dir` forces a full re-decrypt; otherwise `main.py` auto-re-decrypts (incrementally) when `message.db`/`session.db`/`user.db` in the source (`--db-dir` or `WXWORK_DIR/Data`) are newer than the decrypted copies. A missing/partial decrypt fails loudly (it used to silently yield "匹配到 0 个会话").
- **The agent is agentic.** It may emit intermediate reasoning ("我先检索知识库…") before the final JSON. `parse_agent_response` + a continuation loop (`max_continuations=3`) handle this. `_extract_json_candidates` prefers later ```json blocks; `repair_json_quotes` fixes unescaped inner quotes; `_parse_concatenated_objects` handles `{..}{..}` output. If you touch parsing, keep all three fallbacks.
- **Images:** two sources — CDN URL (type=4) and inline bytes (type=36). Base64 inline payload is hard-capped at ~8MB total; excess images are skipped. Falls back to the raw URL if encoding/download fails.
- **`.env` and `summary/upload.env` are gitignored and hold real secrets.** `.env.example` shows the shape; never commit values.
- `main.py.bak` is a stale backup (differs from `main.py`); `zh-6.7.20.jar` is an untracked binary — leave both alone unless explicitly asked.
