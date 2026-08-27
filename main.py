#!/usr/bin/env python3
"""
企业微信聊天记录智能总结脚本
==================================
功能：
1. 从企业微信解密数据库读取聊天记录（含图片信息）
2. 从 conversation.list 读取会话名称（群名或人名），按名称匹配会话（含单聊）
3. 将聊天记录按天划分，尾部插入 prompt.md 提示词，发送到智能体进行问题总结
4. 将智能体返回的问题总结写入 JSON 文件，并更新数据库中的会话处理进度

环境变量（.env）：
    AGENT_URL      — 智能体 API 地址
    AGENT_API_KEY  — 智能体 API 密钥
"""

import argparse
import base64
import json
import os
import re
import shutil
import sys
import time
import urllib.request
import urllib.error
import sqlite3 as sq
from datetime import datetime, timedelta, timezone
from typing import Optional

from wecom_reader import WeComReader

# 企业微信消息时间戳为 UTC epoch；按"北京自然日"切分与展示，避免 UTC 换日线
# （北京时间 08:00）把早晨对话劈成两天。进度库的时间戳字符串仍按 UTC 存储/解析。
CST = timezone(timedelta(hours=8))
# 合并重算某一天时单次拉取的消息上限：远大于 --limit，避免忙天前半段被截断
MERGE_FETCH_LIMIT = 10000


# ============================================================
# 1. 从 .env 读取智能体环境变量
# ============================================================
def load_env(path: str = ".env") -> dict:
    """加载 .env 文件，返回键值对字典（忽略空行和注释）"""
    env = {}
    if not os.path.isfile(path):
        print(f"[WARNING] .env file not found at {path}")
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# ============================================================
# 2. 调用智能体 API（OpenAI 兼容格式）
# ============================================================
# 智能体服务端超时/故障时，会把错误信息作为 content 返回，需识别并当作失败处理
_ERROR_CONTENT_PATTERNS = (
    "api call failed",
    "request timed out",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "connection reset",
)


def _is_error_content(text: str) -> bool:
    """判断响应内容是否是服务端返回的错误信息（而非正常模型输出）"""
    t = text.strip().lower()
    if not t:
        return True
    return any(p in t for p in _ERROR_CONTENT_PATTERNS)


def call_agent(
    messages: list[dict],
    agent_url: str,
    api_key: str,
    model: str = "hermes-agent",
    temperature: float = 0.1,
    timeout: int = 600,
    retries: int = 3,
) -> Optional[str]:
    """调用 OpenAI 兼容的智能体 API，返回回复文本；失败自动重试。

    timeout 默认 600 秒：多模态大请求（单日数百条消息+多张图片）的 agent
    处理耗时较长，300 秒易触发 socket 读取超时。可通过 --agent-timeout 覆盖。

    返回 None 表示失败；服务端返回错误信息（如内部超时）时立即判定失败，
    不再做客户端重试（服务端通常已经重试过）。
    """
    url = agent_url.rstrip("/") + "/v1/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }).encode("utf-8")

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            # 兼容不同响应格式
            if "choices" in result and len(result["choices"]) > 0:
                content = result["choices"][0].get("message", {}).get("content", "")
            elif "response" in result:
                content = result["response"]
            else:
                print(
                    f"[ERROR] Unexpected API response format: "
                    f"{json.dumps(result, ensure_ascii=False)[:200]}"
                )
                return None
            # 服务端内部已重试过（错误信息即证明），仅容忍瞬时抖动：
            # 等待后重试一次，仍失败则判定失败
            if _is_error_content(content):
                print(
                    f"[ERROR] 服务端返回错误信息（内部超时/故障）: {content[:120]}"
                )
                if attempt < retries:
                    delay = 30 * attempt
                    print(f"  [RETRY] 服务端可能瞬时抖动，{delay} 秒后重试...")
                    time.sleep(delay)
                    continue
                return None
            return content
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:500]
            print(f"[ERROR] API HTTP {e.code}: {body}")
            # 服务端错误（5xx）才重试；4xx 是请求问题，重试无意义
            if e.code < 500 or attempt >= retries:
                return None
        except Exception as e:
            print(f"[ERROR] API call failed: {e}")
            if attempt >= retries:
                return None
        delay = 10 * attempt
        print(f"  [RETRY] 第 {attempt}/{retries} 次调用失败，{delay} 秒后重试...")
        time.sleep(delay)
    return None


def _extract_json_candidates(response: str) -> list[str]:
    """从响应中提取所有可能的 JSON 候选文本，按可信度从高到低排列。

    智能体（agentic 模型）的响应可能包含中间过程文本和多个代码块，
    如：
      - "我先检索知识库... ```json {...} ``` ... 最终结论：```json [...] ```"
      - "处理步骤：1. xxx  2. yyy ... 结果：[...]"
      - 纯 JSON 数组

    候选来源：
      1. 所有 ```json 代码块（靠后的优先，最终答案通常在末尾）
      2. 所有无标记代码块
      3. 第一个 [/{ 到最后一个 ]/} 之间的整段文本
    每个候选去重后返回。
    """
    candidates: list[str] = []
    cleaned = response.strip()

    # 1. ```json 代码块（多个时取靠后的）
    json_blocks = re.findall(r"```json\s*\n?([\s\S]*?)```", cleaned, re.IGNORECASE)
    for block in reversed(json_blocks):
        candidates.append(block.strip())

    # 2. 无语言标记的代码块
    plain_blocks = re.findall(r"```\s*\n?([\s\S]*?)```", cleaned)
    for block in reversed(plain_blocks):
        candidates.append(block.strip())

    # 3. 整段文本中第一个 [/{ 到最后一个 ]/} 的跨度
    starts = [i for i in (cleaned.find("["), cleaned.find("{")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(cleaned.rfind("]"), cleaned.rfind("}"))
        if end > start:
            candidates.append(cleaned[start : end + 1].strip())

    # 去重（保持顺序）
    seen = set()
    unique = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


# 期望的问题总结结构中应出现的字段（用于校验候选是否为有效总结而非中间噪声）
_EXPECTED_KEYS = ("name", "description", "scenario", "trigger_method",
                  "symptoms", "cause", "solution", "verification", "notes", "status")


def _looks_like_summary(data) -> bool:
    """校验解析结果是否符合 prompt.md 要求的问题总结结构"""
    if isinstance(data, list):
        # 空数组合法（表示当天没有问题）
        if not data:
            return True
        return all(
            isinstance(item, dict) and any(k in item for k in _EXPECTED_KEYS)
            for item in data
        )
    if isinstance(data, dict):
        # 单条问题也可能以对象形式返回
        return any(k in data for k in _EXPECTED_KEYS)
    return False


def _parse_concatenated_objects(text: str):
    """解析连续的多个 JSON 对象（模型可能输出 {..}{..}{..} 而非 [..]）。

    用 JSONDecoder.raw_decode 逐个解析，若能解析出 ≥1 个对象，
    返回列表；否则返回 None。这是对模型不按数组格式输出的兜底。
    """
    text = text.strip()
    dec = json.JSONDecoder()
    objects = []
    idx = 0
    n = len(text)
    while idx < n:
        # 跳过空白和分隔符
        while idx < n and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= n:
            break
        # 遇到 { 或 [ 时尝试解析
        if text[idx] in "{[":
            try:
                obj, end = dec.raw_decode(text, idx)
                objects.append(obj)
                idx = end
                continue
            except json.JSONDecodeError:
                pass
        # 跳过非 JSON 字符，继续寻找下一个对象
        idx += 1
    if len(objects) >= 1:
        return objects
    return None


def parse_agent_response(response: str):
    """解析智能体响应为 JSON 数据。

    依次尝试所有候选文本（代码块优先、靠后的代码块优先），
    解析成功且结构符合问题总结格式才接受；失败时尝试修复未转义引号后再次尝试；
    仍失败时尝试解析连续的多个对象（模型可能输出 {..}{..} 而非 [..]）并合并为数组；
    全部失败则返回 {"raw_response": 原文} 以便人工处理。

    数组结果优先于单对象：若同时存在多个候选，优先采纳含更多记录的数组。
    """
    if _is_error_content(response):
        return {"raw_response": response}, False

    candidates = _extract_json_candidates(response)

    # 第一轮：直接解析，收集所有"看起来像总结"的结果
    found = []  # [(data, is_array)]
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if _looks_like_summary(data):
            found.append((data, isinstance(data, list)))

    # 第二轮：修复未转义引号后重试
    for cand in candidates:
        repaired = repair_json_quotes(cand)
        if repaired == cand:
            continue
        try:
            data = json.loads(repaired)
        except json.JSONDecodeError:
            continue
        if _looks_like_summary(data):
            found.append((data, isinstance(data, list)))

    # 第三轮：模型可能输出 {..}{..}{..}（未包裹在 [] 中），逐个解析后合并
    # 同时处理"多个代码块各含单对象"的情况：把所有候选解析出的对象汇总去重
    all_objects = []
    seen_names = set()
    for cand in candidates:
        objs = _parse_concatenated_objects(cand)
        if objs is None:
            continue
        for o in objs:
            if isinstance(o, dict) and any(k in o for k in _EXPECTED_KEYS):
                # 用 name 字段去重（不同候选可能解析出同一对象）
                key = o.get("name", id(o))
                if key not in seen_names:
                    seen_names.add(key)
                    all_objects.append(o)
    if all_objects:
        found.append((all_objects, True))

    if found:
        # 优先选数组；同为数组时选记录数最多的；单对象转为数组统一输出
        arrays = [(d, True) for d, is_arr in found if is_arr]
        if arrays:
            best = max(arrays, key=lambda x: len(x[0]) if isinstance(x[0], list) else 1)
            return best[0], True
        # 没有数组，取第一个单对象（转成数组形式，与 prompt 要求一致）
        single = found[0][0]
        return [single] if isinstance(single, dict) else single, True

    return {"raw_response": response}, False


def repair_json_quotes(text: str) -> str:
    """修复字符串值内部未转义的英文双引号（智能体常见输出错误）。

    智能体生成的 JSON 中常出现 `内容为"xxx"（说明）` 这类值内部未转义的
    双引号。修复策略：把前后都紧跟中文/全角标点/字母/数字的引号转义为 \\"。
    合法的结构引号前后是 { [ , : } ] 或空白，不会命中该规则。
    """
    pattern = re.compile(
        r'(?<=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef0-9a-zA-Z])'
        r'"(?=[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef0-9a-zA-Z])'
    )
    return pattern.sub(r'\\"', text)


# ============================================================
# 2.5 图片处理（多模态支持）
# ============================================================
_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def resolve_image_to_data_url(reader: WeComReader, url: str) -> Optional[str]:
    """将图片 URL 解析为 base64 data URL，供多模态 API 调用使用。

    尝试顺序：
    1. 从本地缓存获取（需 reader 配置了 wxwork_dir 且 CacheMapping 存在）
    2. 从 CDN 下载
    3. 回退到原始 URL（让 API 端直接访问，但可能因鉴权/过期而失败）
    """
    # 1. 尝试本地缓存
    local_path = reader.get_image_path(url)

    # 2. 尝试从 CDN 下载
    if local_path is None or not os.path.isfile(local_path):
        local_path = reader.download_image(url)

    # 3. 有本地文件 → 转 base64 data URL
    if local_path and os.path.isfile(local_path):
        ext = os.path.splitext(local_path)[1].lower()
        mime = _MIME_MAP.get(ext, "image/jpeg")
        try:
            with open(local_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return f"data:{mime};base64,{b64}"
        except Exception:
            pass

    # 4. 回退：返回原始 URL
    return url if url.startswith(("http://", "https://")) else None


def inline_image_to_data_url(img: dict) -> Optional[str]:
    """将内嵌图片（type=36，已含 raw bytes）转为 base64 data URL。"""
    data = img.get("inline_data")
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    mime = img.get("mime", "image/jpeg")
    try:
        b64 = base64.b64encode(bytes(data)).decode()
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def collect_images(msgs: list[dict]) -> list[dict]:
    """从消息列表中收集所有图片消息，返回 [{msg, kind, ...}, ...]。

    同时支持两种图片来源：
      - type=4  CDN URL（kind=url）     → 后续需下载/查缓存
      - type=36 内嵌字节（kind=inline） → 直接转 base64，无需下载
    """
    result = []
    for msg in msgs:
        if msg.get("type_name") != "image":
            continue
        for img in msg.get("images", []):
            if img.get("url"):
                result.append({"msg": msg, "kind": "url", "url": img["url"]})
            elif img.get("inline_data"):
                result.append({"msg": msg, "kind": "inline", "img": img})
    return result


def build_multimodal_content(full_prompt: str, image_data_urls: list[str]) -> list[dict]:
    """构造 OpenAI 多模态消息内容。

    有图片时返回 [{type: text}, {type: image_url}, ...]；
    无图片时返回纯文本字符串（节省 token）。
    """
    if not image_data_urls:
        return full_prompt # type: ignore
    parts: list[dict] = [{"type": "text", "text": full_prompt}]
    for url in image_data_urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


# ============================================================
# 3. 格式化消息为文本（含图片信息）
# ============================================================
def format_messages_for_prompt(msgs: list[dict]) -> str:
    """将消息列表格式化为适合 LLM 阅读的文本格式"""
    lines = []
    for msg in msgs:
        ts = msg.get("send_time", 0)
        dt = (
            datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%d %H:%M:%S")
            if ts
            else "未知时间"
        )
        sender = msg.get("sender_name", f"user_{msg.get('sender_id', 'unknown')}")
        content = msg.get("content", "")
        type_name = msg.get("type_name", "unknown")

        if type_name == "text":
            lines.append(f"[{dt}] {sender}: {content}")
        elif type_name == "image":
            images = msg.get("images", [])
            img_info = "; ".join(
                img.get("url", "") for img in images if img.get("url")
            )
            if img_info:
                lines.append(f"[{dt}] {sender}: [图片] {img_info}")
            else:
                lines.append(f"[{dt}] {sender}: [图片]")
        else:
            extra = f" ({content})" if content else ""
            lines.append(f"[{dt}] {sender}: [{type_name}]{extra}")

    return "\n".join(lines)


# ============================================================
# 4. 按天划分消息
# ============================================================
def group_messages_by_day(msgs: list[dict]) -> list[tuple[str, list[dict]]]:
    """将消息列表按天分组，返回 [(日期字符串, 消息列表), ...]（按日期升序）

    日期按北京时间（UTC+8）划分，使"一天"与用户的自然日一致。"""
    days: dict[str, list[dict]] = {}
    for msg in msgs:
        ts = msg.get("send_time", 0)
        day = datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%d")
        days.setdefault(day, []).append(msg)
    # 按日期升序排列
    return sorted(days.items(), key=lambda x: x[0])


def msg_key(m: dict) -> str:
    """消息去重键（跨表查询时同一消息可能出现多次）"""
    mid = m.get("message_id")
    return f"id:{mid}" if mid is not None else f"seq:{m.get('sequence')}"


# ============================================================
# 4.5 跨天问题案例状态（cases 表）
# ============================================================
# status 是管线内部排程字段，不进 summary、不传 DataCop。
# 仅用于决定次日 prompt 是否注入该问题，判据是本地启发式，不依赖 agent 标注。
# 判"已解决"的启发式：solution 与 verification 均非空且非占位词才算 resolved。
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


def case_status_from_summary(case: dict) -> str:
    """根据 case 内容判断内部 status：solution+verification 双有效 → resolved，否则 open。

    保守判：宁可多留一轮回注入，不可误判 resolved 后断链。
    """
    return "resolved" if (
        _field_filled(case.get("solution"))
        and _field_filled(case.get("verification"))
    ) else "open"


def fetch_open_cases(db: sq.Connection, session_id: str, today: str, limit: int = 30) -> list[dict]:
    """取本会话截至 today 仍未解决（status='open'）的问题案例，按最近活跃降序。

    返回原始 case dict 列表（含 name 与已有字段），供注入当日 prompt。
    限制条数避免 prompt 膨胀。
    """
    rows = db.execute(
        "SELECT summary_json FROM cases "
        "WHERE session_id = ? AND status = 'open' AND last_seen_date < ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (session_id, today, limit),
    ).fetchall()
    cases = []
    for r in rows:
        try:
            cases.append(json.loads(r[0]))
        except (json.JSONDecodeError, TypeError):
            continue
    return cases


def build_open_cases_block(open_cases: list[dict]) -> str:
    """把未解决案例拼成 prompt 文本块（精简版，不含 solution/verification，
    避免引导 agent 照抄既有结论）。
    """
    if not open_cases:
        return ""
    lines = ["以下是此前尚未解决的问题，请结合今日聊天记录判断是否已有进展；",
              "若与今日内容相关，请保持问题 name 不变并更新相关字段；",
              "若今日未再提及，则无需重复输出该问题：\n"]
    for i, c in enumerate(open_cases, 1):
        parts = [f"{i}. [{c.get('name', '?')}]"]
        for k in ("description", "symptoms", "cause"):
            v = c.get(k)
            if v:
                parts.append(f"{k}: {v}")
        lines.append("\n".join(parts))
    return "\n".join(lines) + "\n"


def upsert_cases(db: sq.Connection, session_id: str, session_name: str,
                 day_str: str, summary_data) -> None:
    """把当日 agent 输出的 case 按 name 写入 cases 表（upsert）。

    已存在的同名问题：用最新内容覆盖，刷新 status / last_seen_date / updated_at。
    新问题：插入。resolved 的不再注入后续 prompt，但仍保留记录用于上传与可追溯。
    """
    if not isinstance(summary_data, list):
        return
    # 与 sessions.last_msg_timestamp 一致，统一按 UTC 存储（见 AGENTS.md 约定）
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for case in summary_data:
        if not isinstance(case, dict):
            continue
        name = case.get("name")
        if not name:
            continue
        status = case_status_from_summary(case)
        db.execute(
            "INSERT INTO cases (session_id, session_name, name, status, "
            "last_seen_date, updated_at, summary_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, name) DO UPDATE SET "
            "status=excluded.status, last_seen_date=excluded.last_seen_date, "
            "updated_at=excluded.updated_at, summary_json=excluded.summary_json",
            (session_id, session_name, name, status, day_str, now_str,
             json.dumps(case, ensure_ascii=False)),
        )
    db.commit()


# ============================================================
# 5. 主流程
# ============================================================
def parse_args():
    """解析命令行参数，支持 -h 查看使用说明"""
    p = argparse.ArgumentParser(
        description="企业微信聊天记录智能总结脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python main.py                          # 使用默认配置运行
  python main.py -h                       # 查看使用说明
  python main.py -c my.list -d 14         # 指定会话列表，拉取最近14天
  python main.py -l 500 --days 30         # 最多拉500条，时间窗口30天
  python main.py -o results               # 总结输出到 results/ 目录
  python main.py --temperature 0.3        # 调高模型温度（更随机）
  python main.py --dry-run                # 仅列出会话，不调用智能体

配置优先级: 命令行参数 > .env 文件 > 默认值
        """,
    )
    p.add_argument("-c", "--conversation-list",
                   default="conversation.list",
                   help="会话名称列表文件路径，每行一个群名或人名 (默认: conversation.list)")
    p.add_argument("-p", "--prompt",
                   default="prompt.md",
                   help="提示词模板文件路径 (默认: prompt.md)")
    p.add_argument("-o", "--output-dir",
                   default="summary",
                   help="总结 JSON 输出目录 (默认: summary/)")
    p.add_argument("--db",
                   default="data/wecom.db",
                   help="进度数据库路径 (默认: data/wecom.db)")
    p.add_argument("--decrypted-dir",
                   default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "wecom-reader", "wxwork_decrypted"),
                   help="解密后的企业微信数据库目录 (默认: <脚本目录>/wecom-reader/wxwork_decrypted)")
    p.add_argument("--db-dir",
                   default=None,
                   help="企业微信原始 Data 目录（首次运行自动解密用）")
    p.add_argument("--wxwork-dir",
                   default=None,
                   help="企业微信 Profile 目录（用于本地图片缓存查找）")
    p.add_argument("-d", "--days",
                   type=int, default=7,
                   help="首次处理时回溯的天数；增量处理时仅用作积压告警阈值，不影响实际拉取范围 (默认: 7)")
    p.add_argument("-l", "--limit",
                   type=int, default=300,
                   help="每轮单会话拉取的消息条数上限；超过则下次续取，不会丢消息 (默认: 300)")
    p.add_argument("--agent-url",
                   default=None,
                   help="智能体 API 地址（覆盖 .env 中的 AGENT_URL）")
    p.add_argument("--agent-api-key",
                   default=None,
                   help="智能体 API 密钥（覆盖 .env 中的 AGENT_API_KEY）")
    p.add_argument("--model",
                   default="hermes-agent",
                   help="智能体模型名称 (默认: hermes-agent)")
    p.add_argument("--temperature",
                   type=float, default=0.1,
                   help="模型温度，越低越确定性 (默认: 0.1)")
    p.add_argument("--agent-timeout",
                   type=int, default=600,
                   help="单次 agent API 调用超时秒数；多模态大请求耗时较长 (默认: 600)")
    p.add_argument("--dry-run",
                   action="store_true",
                   help="仅列出匹配到的会话，不调用智能体（可用于验证配置）")
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("企业微信聊天记录智能总结脚本")
    print("=" * 60)

    # ---- 5.1 加载环境变量 ----
    env = load_env()
    agent_url = args.agent_url or env.get("AGENT_URL", "")
    agent_api_key = args.agent_api_key or env.get("AGENT_API_KEY", "")
    if not agent_url or not agent_api_key:
        print("[ERROR] AGENT_URL 和 AGENT_API_KEY 必须在 .env 中配置，或通过 --agent-url / --agent-api-key 传入")
        return
    print(f"[INFO] Agent URL: {agent_url}")
    print(f"[INFO] API Key: {agent_api_key[:8]}...{agent_api_key[-4:]}")
    print(f"[INFO] 模型: {args.model}, 温度: {args.temperature}")
    print(f"[INFO] 时间窗口: {args.days} 天, 消息上限: {args.limit} 条")

    # ---- 5.2 初始化 WeComReader ----
    wxwork_dir = args.wxwork_dir or env.get("WXWORK_DIR", "")
    reader_kwargs = {"decrypted_dir": args.decrypted_dir}
    if wxwork_dir:
        reader_kwargs["wxwork_dir"] = wxwork_dir
    reader = WeComReader(**reader_kwargs)
    status = reader.status()

    # 解密源目录：--db-dir 优先，其次 .env 的 WXWORK_DIR/Data
    db_dir = args.db_dir
    if not db_dir and wxwork_dir:
        candidate = os.path.join(wxwork_dir, "Data")
        if os.path.isdir(candidate):
            db_dir = candidate

    need_decrypt = not status.get("decrypted")
    if args.db_dir and status.get("decrypted"):
        # 用户显式指定了源库目录，视为要求重新解密（覆盖旧的解密快照）
        print("[INFO] 已指定 --db-dir，将重新解密（忽略已有解密快照）...")
        need_decrypt = True
    elif status.get("decrypted") and db_dir:
        # 未显式指定源库时，源库比解密快照新说明有新消息，增量重新解密
        stale = [
            name for name in ("message.db", "session.db", "user.db")
            if os.path.isfile(os.path.join(db_dir, name))
            and (
                not os.path.isfile(os.path.join(args.decrypted_dir, name))
                or os.path.getmtime(os.path.join(db_dir, name))
                > os.path.getmtime(os.path.join(args.decrypted_dir, name))
            )
        ]
        if stale:
            print(f"[INFO] 源数据库有更新（{', '.join(stale)}），将重新解密...")
            need_decrypt = True

    if need_decrypt:
        if not db_dir:
            print("[ERROR] 未找到解密数据库，请通过 --db-dir 指定企业微信 Data 目录，"
                  "或在 .env 中配置 WXWORK_DIR，或先手动解密")
            return
        print("[INFO] 开始解密...")
        try:
            reader = WeComReader(
                db_dir=db_dir,
                decrypted_dir=args.decrypted_dir,
                wxwork_dir=wxwork_dir or None,
            )
            # 显式指定 --db-dir 时全量重新解密；仅因源库更新时增量解密
            result = reader.init(verbose=True, force=bool(args.db_dir))
        except RuntimeError as e:
            print(f"[ERROR] 解密失败: {e}")
            return
        # 解密不完整（缺关键库）会表现为"匹配到 0 个会话"，这里直接报错提示
        missing = [
            name for name in ("session.db", "message.db", "user.db")
            if not os.path.isfile(os.path.join(args.decrypted_dir, name))
        ]
        if missing or not result.get("success"):
            print(f"[ERROR] 数据库解密不完整，缺少: {', '.join(missing) or '全部'}")
            print("       请确认 WXWork 正在运行且脚本以管理员权限执行")
            return
        print(f"[INFO] 解密完成: {result}")

    # ---- 5.3 读取会话列表 ----
    session_names = []
    list_path = args.conversation_list
    if not os.path.isfile(list_path):
        print(f"[ERROR] 未找到会话列表文件: {list_path}")
        print("        请创建文件（每行一个群名或人名），或通过 -c 参数指定路径")
        return
    with open(list_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                session_names.append(line)
    print(f"[INFO] 从 {list_path} 读取了 {len(session_names)} 个会话名称")
    for s in session_names:
        print(f"       - {s}")

    # ---- 5.4 按名称搜索会话（从解密后的 session.db） ----
    sessions = []
    for name in session_names:
        # 1) 直接按名称/ID 匹配会话（群名、会话 ID 等）
        found = reader.list_sessions(limit=10, keyword=name)
        if found:
            sessions.extend(found)

        # 2) 按人名匹配单聊：S: 会话的 name 字段存的是最后一条消息预览而非
        #    联系人名，所以先在 user.db 里按名字查对方 vid，再用 vid 精确匹配
        #    会话 ID（S:<本人vid>_<对方vid> / M:<对方vid>）。
        #    注意不能把数字 vid 作为 keyword 走 name LIKE——会误命中群名/消息
        #    预览里恰好含该数字的会话。
        person_hits = 0
        for contact in reader.contacts(keyword=name):
            hits = reader.list_sessions(limit=10, peer_id=str(contact["id"]))
            for hit in hits:
                if hit["type"] not in ("single", "wechat_contact"):
                    continue
                # 会话自带 name 为消息预览时保留（更描述性），为空时用联系人名
                if contact["name"] and not hit["name"]:
                    hit = dict(hit)
                    hit["name"] = contact["name"]
                sessions.append(hit)
                person_hits += 1

        if not found and not person_hits:
            print(f"[WARN] 名称未匹配到任何会话: {name}")

    # 去重（同一个会话可能被多个关键词匹配到）
    seen_ids = set()
    unique_sessions = []
    for s in sessions:
        if s["id"] not in seen_ids:
            seen_ids.add(s["id"])
            unique_sessions.append(s)
    sessions = unique_sessions

    # 会话 name 为空时补名（否则输出文件名会冲突/为空）：
    # 单聊优先解析对端联系人名，其余（含解析失败）用会话 ID 兜底
    for s in sessions:
        if s["name"]:
            continue
        if s["type"] in ("single", "wechat_contact"):
            s["name"] = reader.single_chat_peer_name(s["id"]) or ""
        if not s["name"]:
            s["name"] = s["id"].replace(":", "_")

    print(f"[INFO] 匹配到 {len(sessions)} 个会话（已去重）")
    for s in sessions:
        print(f"       - {s['name']} ({s['id']}) [{s['type']}]")

    # ---- dry-run 模式：仅列出会话，不调用智能体 ----
    if args.dry_run:
        print("\n[DRY-RUN] 仅列出会话，不调用智能体。请检查上方匹配结果是否正确。")
        return

    # ---- 5.5 连接本地进度数据库 ----
    db_dir_path = os.path.dirname(args.db)
    if db_dir_path:
        os.makedirs(db_dir_path, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)
    db = sq.connect(args.db)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            session_name TEXT NOT NULL,
            last_sequence INTEGER NOT NULL,
            last_msg_timestamp TEXT NOT NULL
        )
    """)
    # 跨天问题案例状态：按 (session_id, name) 唯一，status 仅用于次日 prompt 注入
    db.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            session_name TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            last_seen_date TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            UNIQUE(session_id, name)
        )
    """)
    db.commit()

    # ---- 5.6 加载 prompt.md ----
    prompt_path = args.prompt
    if not os.path.isfile(prompt_path):
        print(f"[ERROR] 未找到提示词模板文件: {prompt_path}")
        print("        请创建文件，或通过 -p 参数指定路径")
        db.close()
        return
    with open(prompt_path, "r", encoding="utf-8") as f:
        prompt_template = f.read().strip()
    print(f"[INFO] 加载提示词模板: {prompt_path} ({len(prompt_template)} 字符)")

    # ---- 5.7 处理每个会话 ----
    now_ts = int(time.time())
    message_limit = args.limit
    from_time_seconds = 3600 * 24 * args.days

    for session in sessions:
        session_id = session["id"]
        session_name = session["name"]
        print(f"\n{'=' * 50}")
        print(f"[PROCESS] 会话: {session_name} ({session_id})")

        # 查询数据库中的会话处理进度
        cur = db.execute(
            "SELECT id, session_id, session_name, last_sequence, last_msg_timestamp "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()

        if row is None:
            # 新会话，插入默认记录（从最早开始处理）
            db.execute(
                "INSERT INTO sessions (session_id, session_name, last_sequence, last_msg_timestamp) "
                "VALUES (?, ?, ?, ?)",
                (session_id, session_name, 0, "1970-01-01 00:00:00"),
            )
            db.commit()
            last_sequence = 0
            last_timestamp = 0
            print("[INFO] 新会话，将从最早未处理的消息开始")
        else:
            last_sequence = row[3] if row[3] is not None else 0
            last_ts_str = row[4] if row[4] is not None else "1970-01-01 00:00:00"
            try:
                last_dt = datetime.strptime(last_ts_str, "%Y-%m-%d %H:%M:%S")
                last_timestamp = int(last_dt.replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                last_timestamp = 0
            print(
                f"[INFO] 上次处理进度: sequence={last_sequence}, "
                f"timestamp={last_ts_str}"
            )

        # 计算增量取数范围：
        # - 增量按 sequence 过滤（after_sequence），彻底规避 send_time 与 sequence
        #   偶尔倒挂导致的漏消息（延迟入库的消息 sequence 更大但 send_time 更早）。
        # - 首次处理（last_sequence=0）额外用 --days 限定回溯窗口，避免一次拉全部历史。
        if last_sequence > 0:
            fetch_since = None  # 增量不按时间过滤，纯按 sequence 追平积压
            if last_timestamp > 0 and now_ts - last_timestamp > from_time_seconds:
                print(
                    f"[WARN] 进度落后超过 {args.days} 天，将追平全部积压消息；"
                    f"单次最多处理 {message_limit} 条，剩余下次继续"
                )
        else:
            fetch_since = max(0, now_ts - from_time_seconds)

        # 升序取最旧的一批（sequence 升序），超过 limit 时下次续取，绝不丢消息
        msgs = reader.get_messages(
            session_id,
            limit=message_limit,
            since=fetch_since,
            after_sequence=last_sequence,
            ascending=True,
        )
        # 从旧到新排序，便于按天分组
        msgs.sort(key=lambda m: m.get("sequence", 0))

        if not msgs:
            print("[INFO] 没有新消息")
            continue

        print(
            f"[INFO] 获取到 {len(msgs)} 条消息 "
            f"(自 sequence>{last_sequence}"
            + (f", 时间>={datetime.fromtimestamp(fetch_since, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
               if fetch_since is not None else "")
            + ")"
        )

        # 按天分组
        daily_groups = group_messages_by_day(msgs)
        print(f"[INFO] 按天划分为 {len(daily_groups)} 组")

        # 逐天调用智能体
        # 进度只在"当天总结成功"后推进，失败时保留在最后一个成功日期
        success_sequence = last_sequence
        success_timestamp = last_timestamp

        for idx, (day_str, day_msgs) in enumerate(daily_groups):
            # 若首个分组与上次进度落在同一天，说明上次处理时该天尚未结束，
            # 补拉当天完整消息重新总结，避免当天文件只覆盖新增消息而丢失已有总结
            if idx == 0 and last_sequence > 0:
                last_day_str = datetime.fromtimestamp(
                    last_timestamp, tz=CST
                ).strftime("%Y-%m-%d")
                if day_str == last_day_str:
                    day_start = datetime.fromtimestamp(
                        last_timestamp, tz=CST
                    ).replace(hour=0, minute=0, second=0, microsecond=0)
                    day_end = day_start + timedelta(days=1)
                    full_day = reader.get_messages(
                        session_id,
                        limit=MERGE_FETCH_LIMIT,
                        since=int(day_start.timestamp()),
                        until=int(day_end.timestamp()),
                        ascending=True,
                    )
                    merged = {msg_key(m): m for m in full_day}
                    for m in day_msgs:
                        merged.setdefault(msg_key(m), m)
                    day_msgs = sorted(
                        merged.values(), key=lambda m: m.get("sequence", 0)
                    )
                    print(f"  [MERGE] 当天与上次进度重叠，补拉完整一天: {len(day_msgs)} 条消息")

            print(f"\n  --- 日期: {day_str} ({len(day_msgs)} 条消息) ---")

            # 格式化消息文本
            formatted_msgs = format_messages_for_prompt(day_msgs)

            # 构造完整 prompt = 聊天记录 + 未解决问题上下文 + 提示词模板
            # 会话类型不同文案不同：群聊/单聊/应用会话
            session_label = {
                "group": "群聊",
                "single": "单聊",
                "wechat_contact": "单聊",
                "app": "应用会话",
            }.get(session["type"], "会话")
            # 注入此前仍未解决的问题（跨天承接）：保持 name 不变以便聚合去重
            open_cases = fetch_open_cases(db, session_id, day_str)
            open_cases_block = build_open_cases_block(open_cases)
            if open_cases_block:
                print(f"  [BACKLOG] 注入 {len(open_cases)} 条未解决问题作为上下文")
            full_prompt = (
                f"以下是 {session_name} 的{session_label}在 {day_str} 的聊天记录：\n\n"
                f"{formatted_msgs}\n\n"
                f"{open_cases_block}"
                f"{prompt_template}"
            )

            # 收集当天所有图片，解析为 base64 data URL 用于多模态输入
            image_data_urls = []
            raw_images = collect_images(day_msgs)
            if raw_images:
                print(f"  [IMG] 发现 {len(raw_images)} 张图片，正在解析...")
                total_bytes = 0
                # 多模态请求体不宜过大，累计 base64 大小硬上限 ~8MB
                max_total_bytes = 8 * 1024 * 1024
                for item in raw_images:
                    if item["kind"] == "inline":
                        data_url = inline_image_to_data_url(item["img"])
                        kind_label = "内嵌图片"
                    else:  # url
                        data_url = resolve_image_to_data_url(reader, item["url"])
                        kind_label = "CDN图片"
                    if not data_url:
                        continue
                    if data_url.startswith("data:"):
                        total_bytes += len(data_url)
                        if total_bytes > max_total_bytes:
                            print(f"         - 累计图片已达 {total_bytes//1024}KB，超过上限，跳过后续图片")
                            break
                        image_data_urls.append(data_url)
                        sender = item["msg"].get("sender_name", "unknown")
                        print(f"         - {sender}: {kind_label}已转 base64 ({len(data_url)//1024}KB)")
                    else:
                        image_data_urls.append(data_url)
                        print(f"         - {item['msg'].get('sender_name','')}: {kind_label}用原始 URL 回退")
                print(f"  [IMG] 成功解析 {len(image_data_urls)}/{len(raw_images)} 张图片")

            # 构造多模态消息内容
            content = build_multimodal_content(full_prompt, image_data_urls)

            # 调用智能体
            print("  [API] 正在调用智能体...")
            response = call_agent(
                messages=[
                    {"role": "user", "content": content},
                ],
                agent_url=agent_url,
                api_key=agent_api_key,
                model=args.model,
                temperature=args.temperature,
                timeout=args.agent_timeout,
            )

            if response is None:
                print(
                    f"  [ERROR] 智能体调用失败（重试后仍失败），"
                    f"停止处理 {session_name} 的后续日期，"
                    f"进度保留在最后一个成功日期，下次运行从此处重试"
                )
                break

            print(f"  [API] 收到响应 ({len(response)} 字符)")

            # 解析 JSON 响应（兼容说明文字、Markdown 代码块、未转义引号等情况）
            summary_data, parsed_ok = parse_agent_response(response)

            # 解析失败时进入续问循环：智能体是 agentic 模型，可能先输出
            # 中间过程（如"我先查询知识库..."），需要"请继续"推动它完成工作
            # （错误内容/空内容不续问，直接按失败处理）
            raw_fallback = response  # 最后一次正常响应，用于最终兜底保存
            continuation_rounds = 0
            max_continuations = 3
            while not parsed_ok and not _is_error_content(response) and continuation_rounds < max_continuations:
                continuation_rounds += 1
                print(
                    f"  [CONTINUE] 响应不是最终 JSON（可能是中间过程），"
                    f"第 {continuation_rounds}/{max_continuations} 次续问..."
                )
                continue_prompt = (
                    "请继续完成上述任务。不要输出过程、说明或推理过程，"
                    "直接输出最终的问题总结 JSON 数组，字符串内部的英文双引号必须转义；"
                    "如果没有可总结的问题，输出空数组 []。"
                )
                # 中间过程可能很长，续问时只回传末尾部分，避免上下文膨胀
                assistant_content = response
                if len(assistant_content) > 20000:
                    assistant_content = "…（中间过程已省略）…\n" + assistant_content[-20000:]
                next_response = call_agent(
                    messages=[
                        {"role": "user", "content": full_prompt},
                        {"role": "assistant", "content": assistant_content},
                        {"role": "user", "content": continue_prompt},
                    ],
                    agent_url=agent_url,
                    api_key=agent_api_key,
                    model=args.model,
                    temperature=args.temperature,
                    timeout=args.agent_timeout,
                )
                if next_response is None:
                    break
                print(f"  [CONTINUE] 收到续问响应 ({len(next_response)} 字符)")
                response = next_response
                if not _is_error_content(response):
                    raw_fallback = response
                summary_data, parsed_ok = parse_agent_response(response)

            if parsed_ok:
                n_records = len(summary_data) if isinstance(summary_data, list) else 1
                print(f"  [PARSE] 成功解析 JSON，包含 {n_records} 条记录")
            else:
                print("  [WARNING] 响应仍不是合法 JSON，按原始文本保存")
                summary_data = {"raw_response": raw_fallback}

            # 保存到 JSON 文件
            safe_name = (
                session_name.replace("/", "_")
                .replace("\\", "_")
                .replace(":", "_")
                .replace("?", "_")
                .replace("*", "_")
                .replace("<", "_")
                .replace(">", "_")
                .replace("|", "_")
                .replace('"', "_")
            )
            # 限制文件名长度
            if len(safe_name) > 80:
                safe_name = safe_name[:80]
            # 日期格式：260809（年份后两位+月+日），用于按日期分目录保存
            date_dir = day_str[2:].replace("-", "")  # "2026-08-09" -> "260809"
            day_output_dir = os.path.join(args.output_dir, date_dir)
            os.makedirs(day_output_dir, exist_ok=True)
            out_file = os.path.join(day_output_dir, f"summary_{safe_name}.json")

            # 合并重算当天会覆盖已有文件，先备份一份 .bak，避免重算失败时丢失原总结
            if os.path.isfile(out_file):
                bak_file = out_file + ".bak"
                shutil.copy2(out_file, bak_file)

            output = {
                "session_id": session_id,
                "session_name": session_name,
                "date": day_str,
                "message_count": len(day_msgs),
                "summary": summary_data,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            print(f"  [SAVE] 总结已保存到 {out_file}")

            # 按 name 写入 cases 表（upsert）：跨天承接的依据，resolved 的不再注入后续
            if parsed_ok:
                upsert_cases(db, session_id, session_name, day_str, summary_data)

            # 当天处理成功，推进进度到该天最后一条消息
            day_max_seq = max(m.get("sequence", 0) for m in day_msgs)
            day_max_ts = max(m.get("send_time", 0) for m in day_msgs)
            success_sequence = max(success_sequence, day_max_seq)
            success_timestamp = max(success_timestamp, day_max_ts)

        # 更新数据库中的会话处理进度（只推进到成功处理的最后一条消息）
        if success_sequence > last_sequence:
            success_ts_str = datetime.fromtimestamp(
                success_timestamp, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "UPDATE sessions SET last_sequence = ?, last_msg_timestamp = ? "
                "WHERE session_id = ?",
                (success_sequence, success_ts_str, session_id),
            )
            db.commit()
            print(
                f"[INFO] 已更新会话进度: {session_name} -> "
                f"last_sequence={success_sequence}, last_msg_timestamp={success_ts_str}"
            )

    db.close()
    print(f"\n{'=' * 60}")
    print("[DONE] 所有会话处理完成")


if __name__ == "__main__":
    main()