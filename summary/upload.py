#!/usr/bin/env python3
"""
批量上传 summary JSON 问题到 DataCop 平台。

用法:
    python upload.py                          # 上传 summary/ 下所有 summary JSON（含日期子目录）
    python upload.py summary_华安基金_*.json   # 仅上传指定文件（递归匹配日期子目录）
    python upload.py --dry-run                # 仅预览，不实际上传
"""

import os
import sys
import glob
import json
import argparse
from pathlib import Path

import requests

# ── 加载 upload.env ──────────────────────────────────────────────
ENV_FILE = Path(__file__).parent / "upload.env"

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


def normalize_problem(problem: dict) -> dict:
    """规范化单条问题：仅保留 9 个上传字段，非字符串一律转为字符串。"""
    return {k: _field_to_text(problem.get(k, "")) for k in FIELDS}


def extract_problems(filepath: Path) -> list[dict]:
    """从 summary JSON 文件中提取问题列表。"""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    problems = data.get("summary", [])
    if not problems:
        print(f"  [SKIP] {filepath.name}: 无 summary 数据")
    return [normalize_problem(p) for p in problems]


def main():
    parser = argparse.ArgumentParser(description="上传 summary 问题到 DataCop 平台")
    parser.add_argument("files", nargs="*", help="要上传的 JSON 文件名（支持通配符）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不实际上传")
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

    if args.dry_run:
        # Dry-run 模式：仅预览
        for filepath in files:
            problems = extract_problems(filepath)
            if problems:
                print(f"  [DRY] {_display_path(filepath, summary_dir)}: {len(problems)} 条问题")
                for i, p in enumerate(problems, 1):
                    print(f"         {i}. {p.get('name', '(无名称)')}")
                total_ok += len(problems)
        print(f"\n[DRY-RUN] 共计 {total_ok} 条问题待上传（未实际上传）")
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
            try:
                result = client.create_problem(project_id, problem)
                print(f"    ✓ {i}. {name} -> id={result.get('id')}")
                total_ok += 1
            except requests.HTTPError as e:
                err_body = e.response.text[:200] if e.response is not None else str(e)
                print(f"    ✗ {i}. {name} -> HTTP {e.response.status_code}: {err_body}")
                total_fail += 1
            except Exception as e:
                print(f"    ✗ {i}. {name} -> {e}")
                total_fail += 1

    # 汇总
    print(f"\n[DONE] 上传完成: 成功 {total_ok}, 失败 {total_fail}")
    if total_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
