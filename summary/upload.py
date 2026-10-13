#!/usr/bin/env python3
"""
批量上传 summary JSON 问题到 DataCop 平台。

解决方案为空的问题一律不上传（判空口径与 main.py 的 _field_filled 一致：
空串或"无/暂无/待定"等纯占位词都视为无解决方案）。

用法:
    python upload.py                          # 上传 summary/ 下所有 summary JSON（含日期子目录）
    python upload.py summary_华安基金_*.json   # 仅上传指定文件（递归匹配日期子目录）
    python upload.py --dry-run                # 仅预览，不实际上传
    python upload.py --cleanup --dry-run      # 预览 DataCop 中解决方案为空的待定问题
    python upload.py --cleanup                # 删除 DataCop 中解决方案为空的待定问题
"""

import os
import re
import sys
import glob
import json
import hashlib
import argparse
from datetime import datetime
from pathlib import Path

import requests

# ── 加载 upload.env ──────────────────────────────────────────────
ENV_FILE = Path(__file__).parent / "upload.env"

# 已上传记录文件：实现幂等，重复运行不会在 DataCop 造重复问题
STATE_FILE = Path(__file__).parent / ".upload_state.json"

# 登录/上传单次请求超时秒数。不设的话服务端无响应时进程会永久挂起。
HTTP_TIMEOUT = 60

# 上传到 DataCop 的 9 个字段（与 prompt.md 定义的 summary 结构一致）
FIELDS = [
    "name", "description", "scenario", "trigger_method",
    "symptoms", "cause", "solution", "verification", "notes",
]


def load_env(path: Path) -> dict:
    """解析 KEY=VALUE 格式的 .env 文件，忽略注释和空行。"""
    env = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


# ── API 客户端 ───────────────────────────────────────────────────
class DataCopClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.api = f"{self.base_url}/api"
        self.token = None
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"
        self._login(username, password)

    def _login(self, username: str, password: str):
        resp = self.session.post(
            f"{self.api}/auth/login",
            json={"username": username, "password": password},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        print(f"[OK] 登录成功: {data['user']['username']} ({data['user']['role']})")

    def create_problem(self, project_id: int, problem: dict) -> dict:
        """向指定项目上传单条问题。"""
        payload = {k: problem.get(k, "") for k in FIELDS}
        if not payload.get("name"):
            raise ValueError("问题 name 不能为空")
        resp = self.session.post(
            f"{self.api}/projects/{project_id}/problems",
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def list_projects(self) -> list[dict]:
        """列出当前账号可见的全部项目。"""
        resp = self.session.get(f"{self.api}/projects", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def list_problems(self, project_id: int, status: str | None = None) -> list[dict]:
        """列出项目下的问题，可按状态筛选（pending=待定 / valid=已保留）。"""
        params = {"status": status} if status else None
        resp = self.session.get(
            f"{self.api}/projects/{project_id}/problems",
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def delete_problems(self, project_id: int, ids: list[int]) -> dict:
        """批量删除项目下的问题。"""
        resp = self.session.delete(
            f"{self.api}/projects/{project_id}/problems",
            json={"ids": ids},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()


# ── 主流程 ────────────────────────────────────────────────────────
def collect_files(patterns: list[str] | None, summary_dir: Path) -> list[Path]:
    """收集要上传的 JSON 文件列表。

    默认递归扫描 summary/ 下所有日期子目录与根目录中的 summary_*.json，
    兼容新旧两种目录结构；显式指定文件名/通配符时同样递归匹配。
    """
    if patterns:
        files: list[Path] = []
        for p in patterns:
            if any(c in p for c in "*?[]"):
                matched = list(summary_dir.glob(p))          # 用户自带子目录前缀
                matched += list(summary_dir.glob(f"**/{p}")) # 递归匹配日期子目录
            else:
                matched = [summary_dir / p]
                if not matched[0].is_file():
                    matched = list(summary_dir.glob(f"**/{p}"))
            files.extend(m for m in matched if m.is_file())
        # 去重（保留顺序，避免 glob 重复命中同一文件）
        seen = set()
        unique = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        return unique
    return sorted(summary_dir.glob("**/summary_*.json"))


def _display_path(filepath: Path, summary_dir: Path) -> str:
    """返回相对于 summary 目录的路径（如 260814/summary_xxx.json），
    便于区分不同日期目录下同名文件。"""
    try:
        return filepath.relative_to(summary_dir).as_posix()
    except ValueError:
        return filepath.name


def _field_to_text(value) -> str:
    """把 agent 可能输出的数组/数字等非字符串字段规范为字符串，列表按行拼接。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    if isinstance(value, str):
        return value
    return str(value)


# 解决方案判空：空串或纯占位词视为"无解决方案"，与 main.py 的 _field_filled 同口径
_PLACEHOLDER_SOLUTION = re.compile(
    r"^\s*(待定|未知|暂无|无|none|n\/a|null|tbd|待确认|待排查|暂未|未定|-\s*)\s*$",
    re.IGNORECASE,
)


def _field_filled(value) -> bool:
    """字段非空、非纯占位词视为有效。"""
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    return not _PLACEHOLDER_SOLUTION.match(s)


def normalize_problem(problem: dict) -> dict:
    """规范化单条问题：仅保留 9 个上传字段，非字符串一律转为字符串。"""
    return {k: _field_to_text(problem.get(k, "")) for k in FIELDS}


def extract_problems(filepath: Path) -> list[dict]:
    """从 summary JSON 文件中提取问题列表。

    返回空列表表示该文件没有可上传的问题；单个文件出问题只跳过它，
    不能让整个批次中断（此前 dict 结构和坏 JSON 都会让进程直接崩掉）。
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"  [SKIP] {filepath.name}: 文件无法解析 ({type(e).__name__}: {e})")
        return []

    problems = data.get("summary", []) if isinstance(data, dict) else []

    # main.py 解析失败时会把 summary 写成 {"raw_response": ...}（dict 而非 list）。
    # 直接遍历 dict 得到的是它的键（字符串），随后 str.get 会抛 AttributeError。
    if isinstance(problems, dict):
        print(f"  [SKIP] {filepath.name}: summary 是对象而非数组"
              f"（解析失败的原始响应），需人工处理")
        return []
    if not isinstance(problems, list):
        print(f"  [SKIP] {filepath.name}: summary 结构异常 ({type(problems).__name__})")
        return []

    # 过滤非 dict 元素，避免单条脏数据让整批上传中断
    valid = [p for p in problems if isinstance(p, dict)]
    dropped = len(problems) - len(valid)
    if dropped:
        print(f"  [WARN] {filepath.name}: 跳过 {dropped} 条非对象记录")
    if not valid:
        print(f"  [SKIP] {filepath.name}: 无 summary 数据")
        return []

    normalized = [normalize_problem(p) for p in valid]
    # 上传约束：解决方案为空的问题不上传
    uploadable = [p for p in normalized if _field_filled(p.get("solution"))]
    no_solution = len(normalized) - len(uploadable)
    if no_solution:
        print(f"  [SKIP] {filepath.name}: 跳过 {no_solution} 条解决方案为空的问题")
    return uploadable


# ── 上传幂等状态 ─────────────────────────────────────────────────
def load_state() -> dict:
    """读取已上传记录：{问题键: {"hash", "id", "at"}}。

    用于幂等——重复运行时跳过内容未变的已上传问题。此前没有任何标记，
    重跑一次就把 summary/ 下所有问题在 DataCop 里再造一遍。
    状态文件损坏时当作"全未上传"处理：宁可重复上传，也不静默丢数据。
    """
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        print(f"[WARN] 上传状态文件无法读取（{e}），本次按未上传处理: {STATE_FILE}")
        return {}


def save_state(state: dict) -> None:
    """原子写回状态文件（先写临时文件再替换，避免中途崩溃留下半截 JSON）。"""
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"[WARN] 上传状态写入失败（{e}），下次运行可能会重复上传")


def problem_key(filepath: Path, summary_dir: Path, name: str) -> str:
    """问题的唯一键：相对路径 + name。跨日期目录下同名问题互不干扰。"""
    return f"{_display_path(filepath, summary_dir)}::{name}"


def problem_hash(problem: dict) -> str:
    """问题内容指纹。同名问题在后续日期会被更新，内容变了就该重新上传。"""
    payload = json.dumps(problem, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── 存量数据清理 ─────────────────────────────────────────────────
DELETE_BATCH = 100  # 批量删除接口单次最多携带的 id 数


def cleanup_empty_solution(client: DataCopClient, dry_run: bool) -> tuple[int, int, int]:
    """遍历 DataCop 已有问题，删除解决方案为空的待定（pending）案例。

    只动 status='pending'（待定）的问题，已保留（valid）的即使没方案也不删。
    返回 (命中数, 实际删除数, 失败数)；dry_run 模式只预览不删除。
    """
    projects = client.list_projects()
    print(f"[INFO] 遍历 {len(projects)} 个项目，查找解决方案为空的待定问题")

    total_match = 0
    total_deleted = 0
    total_fail = 0

    for proj in projects:
        pid = proj.get("id")
        pname = proj.get("name", "(无名项目)")
        try:
            problems = client.list_problems(pid, status="pending")
        except requests.HTTPError as e:
            err_body = e.response.text[:200] if e.response is not None else str(e)
            code = e.response.status_code if e.response is not None else "?"
            print(f"  [FAIL] 项目 {pname} (id={pid}) 问题列表获取失败 -> HTTP {code}: {err_body}")
            total_fail += 1
            continue

        victims = [
            p for p in problems
            if isinstance(p, dict)
            and p.get("status", "pending") == "pending"
            and not _field_filled(p.get("solution"))
        ]
        if not victims:
            continue

        total_match += len(victims)
        print(f"  [PROJ] {pname} (id={pid}): 待定 {len(problems)} 条，其中解决方案为空 {len(victims)} 条")
        for p in victims:
            sol = _field_to_text(p.get("solution"))
            extra = f"  solution={sol!r}" if sol.strip() else ""
            print(f"         - id={p.get('id')}  {p.get('name', '(无名称)')}{extra}")

        ids = [p["id"] for p in victims if p.get("id") is not None]
        if dry_run:
            print(f"         [DRY] 将删除 {len(ids)} 条（未实际删除）")
            continue

        for i in range(0, len(ids), DELETE_BATCH):
            batch = ids[i:i + DELETE_BATCH]
            try:
                result = client.delete_problems(pid, batch)
            except requests.HTTPError as e:
                err_body = e.response.text[:200] if e.response is not None else str(e)
                code = e.response.status_code if e.response is not None else "?"
                print(f"         ✗ 删除 {len(batch)} 条 -> HTTP {code}: {err_body}")
                total_fail += 1
            except Exception as e:
                print(f"         ✗ 删除 {len(batch)} 条 -> {e}")
                total_fail += 1
            else:
                total_deleted += len(batch)
                print(f"         ✓ 删除 {len(batch)} 条: {result.get('message', 'ok')}")

    return total_match, total_deleted, total_fail


def main():
    parser = argparse.ArgumentParser(description="上传 summary 问题到 DataCop 平台")
    parser.add_argument("files", nargs="*", help="要上传的 JSON 文件名（支持通配符）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际上传")
    parser.add_argument("--reupload", action="store_true",
                        help="忽略已上传记录，强制重新上传全部问题")
    parser.add_argument("--cleanup", action="store_true",
                        help="清理模式：删除 DataCop 中解决方案为空的待定问题（不上传）")
    args = parser.parse_args()

    # 加载配置
    if not ENV_FILE.exists():
        print(f"[ERROR] 未找到配置文件: {ENV_FILE}")
        sys.exit(1)

    env = load_env(ENV_FILE)
    api_url = env.get("API_URL", "")
    username = env.get("USERNAME", "")
    password = env.get("PASSWORD", "")
    project_id = env.get("PROJECT_ID", "")

    if not all([api_url, username, password, project_id]):
        print("[ERROR] upload.env 中缺少必要配置 (API_URL / USERNAME / PASSWORD / PROJECT_ID)")
        sys.exit(1)

    project_id = int(project_id)
    summary_dir = Path(__file__).parent

    # ── 清理模式：只清理 DataCop 存量问题，不做上传 ──────────────
    if args.cleanup:
        if args.files:
            print("[WARN] --cleanup 模式忽略文件参数")
        print(f"[INFO] 目标: {api_url}")
        client = DataCopClient(api_url, username, password)
        total_match, total_deleted, total_fail = cleanup_empty_solution(client, args.dry_run)
        if args.dry_run:
            print(f"\n[DRY-RUN] 共 {total_match} 条解决方案为空的待定问题待删除（未实际删除）")
        else:
            print(f"\n[DONE] 清理完成: 命中 {total_match} 条, 删除 {total_deleted} 条, 失败 {total_fail} 条")
        if total_fail:
            sys.exit(1)
        return

    # 收集文件
    files = collect_files(args.files if args.files else None, summary_dir)
    if not files:
        print("[WARN] 未找到任何 JSON 文件")
        sys.exit(0)

    print(f"[INFO] 目标: {api_url}  项目ID: {project_id}")
    print(f"[INFO] 待处理文件: {len(files)} 个\n")

    # 统计
    total_ok = 0
    total_fail = 0
    total_skip = 0

    state = {} if args.reupload else load_state()

    if args.dry_run:
        # Dry-run 模式：仅预览，同时标注哪些会因已上传而跳过
        for filepath in files:
            problems = extract_problems(filepath)
            if not problems:
                continue
            done_flags = [
                state.get(problem_key(filepath, summary_dir, p.get("name", "")), {})
                .get("hash") == problem_hash(p)
                for p in problems
            ]
            n_skip = sum(done_flags)
            print(f"  [DRY] {_display_path(filepath, summary_dir)}: {len(problems)} 条问题"
                  f"（待上传 {len(problems) - n_skip}，已上传跳过 {n_skip}）")
            for i, (p, done) in enumerate(zip(problems, done_flags), 1):
                tag = "（已上传）" if done else ""
                print(f"         {i}. {p.get('name', '(无名称)')}{tag}")
            total_ok += len(problems) - n_skip
            total_skip += n_skip
        print(f"\n[DRY-RUN] 待上传 {total_ok} 条，已上传跳过 {total_skip} 条（未实际上传）")
        return

    # 正式上传
    client = DataCopClient(api_url, username, password)

    for filepath in files:
        problems = extract_problems(filepath)
        if not problems:
            continue

        print(f"  [FILE] {_display_path(filepath, summary_dir)} ({len(problems)} 条)")
        for i, problem in enumerate(problems, 1):
            name = problem.get("name", "(无名称)")
            key = problem_key(filepath, summary_dir, name)
            digest = problem_hash(problem)

            # 内容未变的已上传问题直接跳过，避免在 DataCop 里造重复数据
            if state.get(key, {}).get("hash") == digest:
                total_skip += 1
                continue

            try:
                result = client.create_problem(project_id, problem)
            except requests.HTTPError as e:
                err_body = e.response.text[:200] if e.response is not None else str(e)
                code = e.response.status_code if e.response is not None else "?"
                print(f"    ✗ {i}. {name} -> HTTP {code}: {err_body}")
                total_fail += 1
            except Exception as e:
                print(f"    ✗ {i}. {name} -> {e}")
                total_fail += 1
            else:
                print(f"    ✓ {i}. {name} -> id={result.get('id')}")
                total_ok += 1
                # 每条成功立刻落盘：中途崩溃也不会重复上传已成功的部分
                state[key] = {
                    "hash": digest,
                    "id": result.get("id"),
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                save_state(state)

    # 汇总
    print(f"\n[DONE] 上传完成: 成功 {total_ok}, 失败 {total_fail}, 已上传跳过 {total_skip}")
    if total_fail:
        print("[INFO] 失败项未记入状态文件，下次运行会自动重试")
        sys.exit(1)


if __name__ == "__main__":
    main()
