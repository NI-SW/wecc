# 企业微信聊天记录智能总结

从本地企业微信（WeCom）解密数据库读取群聊记录，按天划分后发送给智能体（OpenAI 兼容 API）进行问题总结，结果以 JSON 文件落盘，并可批量上传到 DataCop 知识库平台。

## 工作流程

```
企业微信本地数据库 → WeComReader 解密读取 → 按天划分消息 → 拼接 prompt.md
   → 调用智能体 API → 解析 JSON 响应 → 写入 summary/<日期>/summary_<群名>.json
   → (可选) summary/upload.py 上传到 DataCop 平台
```

`main.py` 维护每个会话的处理进度（`data/wecom.db`），下次运行只处理新增消息；当天总结失败时进度停留在最后一个成功日期，下次自动重试，不会丢数据。

## 目录结构

| 路径 | 作用 |
|------|------|
| `main.py` | 主脚本：读库 → 按天分组 → 调用智能体 → 解析 → 落盘 → 推进进度。仅依赖标准库 + `wecom_reader` |
| `wecom-reader/` | 企微本地数据库解密与读取库（`WeComReader`），嵌套的独立 git 仓库 |
| `summary/upload.py` | 将 `summary/` 下的 JSON 批量上传到 DataCop（依赖 `requests`） |
| `prompt.md` | 智能体提示词模板，定义问题总结的 JSON 字段结构 |
| `conversation.list` | 要处理的群聊名称，每行一个 |
| `initdb.sql` | 进度库 `sessions` 表建表语句（`main.py` 会自动建表） |
| `data/wecom.db` | 运行时进度数据库（gitignored） |
| `API.md` | DataCop 平台 REST API 文档 |
| `.env` | 智能体配置（`AGENT_URL` / `AGENT_API_KEY` / `WXWORK_DIR`），见 `.env.example` |
| `summary/upload.env` | DataCop 上传配置（`API_URL` / `USERNAME` / `PASSWORD` / `PROJECT_ID`） |

## 环境要求

- **Windows**：解密依赖从 `WXWork.exe` 进程内存提取密钥，仅 Windows 可用
- **Python 3.11**（`wecom-reader` 要求 ≥ 3.10）
- 运行解密需 **管理员权限**，且 **企业微信客户端处于运行中**
- 依赖：`pycryptodomex`、`click`、`Flask`、`requests`、`wecom-reader`（均在项目 `.venv` 中）

## 安装

```bash
# 1. 创建虚拟环境
py -m venv .venv

# 2. 安装依赖（wecom-reader 以可编辑模式安装，改其代码即时生效）
.venv/Scripts/python.exe -m pip install -e wecom-reader
.venv/Scripts/python.exe -m pip install requests
```

## 配置

### `.env`（项目根目录，智能体调用用）

```ini
AGENT_URL="http://your-agent-host:port"
AGENT_API_KEY="your-api-key"
WXWORK_DIR=C:\Users\<用户名>\Documents\WXWork\<企微账号ID>
```

`WXWORK_DIR` 用于本地图片缓存查找；不配图片相关功能会回退到 CDN 下载。

### `summary/upload.env`（上传到 DataCop 用）

```ini
API_URL=http://your-datacop-host
USERNAME=your-username
PASSWORD=your-password
PROJECT_ID=2
```

### `conversation.list`

每行一个群聊名称，`main.py` 会用关键词模糊匹配会话：

```
8097 【9.1.6】【产品规划】原OBSharding组件引入
测试群聊
联合银行项目－交付阶段
```

### `prompt.md`

智能体提示词模板，定义输出 JSON 数组结构：`name` / `description` / `scenario` / `trigger_method` / `symptoms` / `cause` / `solution` / `verification` / `notes`。修改此文件会直接影响总结结果的结构。

## 运行

### 生成总结

> 注意：直接用 `python` / `python3` 在 Windows 上会命中应用商店的占位程序而失败，请用 `.venv` 的解释器或 `py` 启动器。

```bash
# 查看帮助
.venv/Scripts/python.exe main.py -h

# 使用默认配置运行（拉取最近 7 天，每会话最多 300 条）
.venv/Scripts/python.exe main.py

# 指定群聊列表，拉取最近 14 天
.venv/Scripts/python.exe main.py -c my.list -d 14

# 仅列出匹配到的会话，不调用智能体（用于验证配置）
.venv/Scripts/python.exe main.py --dry-run

# 自定义输出目录、模型温度
.venv/Scripts/python.exe main.py -o results --temperature 0.3
```

常用参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `-c/--conversation-list` | `conversation.list` | 群聊名称列表文件 |
| `-p/--prompt` | `prompt.md` | 提示词模板文件 |
| `-o/--output-dir` | `summary` | 总结 JSON 输出目录 |
| `--db` | `data/wecom.db` | 进度数据库路径 |
| `--decrypted-dir` | `…/wecom-reader/wxwork_decrypted` | 已解密数据库目录 |
| `--db-dir` | 无 | 企微原始 Data 目录（首次运行自动解密用） |
| `--wxwork-dir` | 无 | 企微 Profile 目录（本地图片缓存） |
| `-d/--days` | `7` | 拉取消息的时间窗口（天） |
| `-l/--limit` | `300` | 每个会话最多拉取的消息条数 |
| `--agent-url` / `--agent-api-key` | 无 | 覆盖 `.env` 中的智能体配置 |
| `--model` | `hermes-agent` | 智能体模型名 |
| `--temperature` | `0.1` | 模型温度 |
| `--dry-run` | - | 仅列出会话，不调用智能体 |

配置优先级：命令行参数 > `.env` > 默认值。

### 上传到 DataCop

```bash
cd summary
# 预览（不实际上传）
../.venv/Scripts/python.exe upload.py --dry-run

# 上传当前目录所有 summary JSON
../.venv/Scripts/python.exe upload.py

# 仅上传指定文件（支持通配符）
../.venv/Scripts/python.exe upload.py summary_测试群聊_*.json
```

## 输出格式

总结按日期分目录保存，文件名形如 `summary/<YYMMDD>/summary_<群名>.json`：

```
summary/260814/summary_测试群聊.json
```

每个文件结构：

```json
{
  "session_id": "R:165301171863848",
  "session_name": "测试群聊",
  "date": "2026-08-14",
  "message_count": 146,
  "summary": [
    {
      "name": "问题名称",
      "description": "问题描述",
      "scenario": "应用场景",
      "trigger_method": "触发方式",
      "symptoms": "问题症状",
      "cause": "问题原因",
      "solution": "解决方案",
      "verification": "验证方式",
      "notes": "备注"
    }
  ],
  "generated_at": "2026-08-14 12:00:00"
}
```

## 设计要点

- **智能体是 agentic 模型**：可能先输出中间推理过程（"我先检索知识库…"）再给出最终 JSON。`parse_agent_response` 会从多个 ```` ```json ```` 代码块中按可信度筛选，并通过续问循环（最多 3 次）推动模型给出最终结果；还会修复字符串值内部未转义的双引号、处理 `{..}{..}` 连续对象等异常输出。
- **多模态图片**：支持 CDN URL（type=4）和内嵌字节（type=36）两种来源，转 base64 data URL 后随消息一起发送。单次请求的图片 base64 总量硬上限约 8MB，超出部分自动跳过；下载/编码失败时回退到原始 URL。
- **进度安全**：进度只在当天总结成功后推进，失败时保留在最后一个成功日期，下次运行从该处重试，且会补拉重叠当天的完整消息重新总结。
- **UTC 时间**：所有时间戳统一使用 UTC，进度库中以 `"%Y-%m-%d %H:%M:%S"` 字符串存储并按 UTC 解析，不要混入本地时区。

## 说明

- `wecom-reader` 是嵌套的独立 git 仓库（父仓库以 gitlink 形式记录，未配置 `.gitmodules`）。修改其代码时需在嵌套仓库内提交，父仓库随后才会显示 `wecom-reader` 指针更新。
- `.env` 与 `summary/upload.env` 含真实密钥，已加入 `.gitignore`，切勿提交。
