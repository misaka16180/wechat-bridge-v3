# 主动发送 API：Agent 接入说明

v3 提供独立于 WeFlow/AstrBot 的主动发送接口。Agent 可以主动提交私聊或群聊的文字、图片和文件，并通过任务接口查询排队位置、执行进度、逐步日志和最终结果。

## Agent 必须遵守的调用约定

1. 图片和文件默认使用“两步上传”：先把原始二进制上传到 `/api/v1/uploads`，再把返回的 `upload_id` 交给 `/api/v1/messages`。不要默认传本地 `path/source`，也不要为了发送文件自行修改 `media.allowed_local_roots`。
2. 每个发送意图生成一个唯一 `request_id`。网络超时后重试同一条消息时，必须复用原 `request_id` 和原请求内容，防止微信重复发送。
3. 创建消息任务只代表进入队列。Agent 必须保存返回的 `task_id`，按 `poll_after_ms` 轮询，直到状态成为 `succeeded`、`failed` 或 `cancelled`。
4. `sent_unverified` 和 `media_sent_unverified` 表示发送动作已提交，只是程序没有把聊天气泡当作最终送达证明；这是成功的终态，严禁因此换新 `request_id` 重发。
5. 同一时间只执行一个桌面任务，其余请求排队。Agent 不应并行重试，也不应因为 `queued` 或进度暂时不变就重复创建任务。
6. 如果 `/api/v1/uploads` 返回 404，而 `/api/v1/messages` 仍存在，通常是 8765 端口上的旧 v3 进程尚未重启。停止该监听进程并用当前版本重新启动；不要通过更换端口绕开旧进程。

## 使用前配置

在网页控制台完成以下操作：

1. 在“连接设置 → 主动发送 API”中开启主动发送 API。
2. 配置 Token 并保存。Token 可为任意长度；为了避免本机其它程序误调用，推荐使用“随机生成”并复制给 Agent。
3. 在首页开启“微信自动化”。主动发送 API 不要求启动 WeFlow/AstrBot 桥接，但必须保持 v3 控制台进程和微信自动化运行。
4. 按使用说明完成微信字体、浅色主题、`Ctrl+Enter` 发送快捷键、DPI/模板兼容性检查等设置，并保持微信已登录。
5. 如需让 Agent 直接读取本文档，可开启“公开 API 文档（无需登录）”并保存。该选项默认关闭；开启后可访问 `/docs/active-api` 网页版和 `/docs/active-api.md` 原始 Markdown。若控制台监听局域网，同网段设备也能阅读这份静态说明，但页面不会包含 Token、连接配置、任务或日志。

Token 留空时，接口只接受本机回环地址调用，而且请求不能携带空的或任意 Bearer Token。配置 Token 后，每个请求都必须携带：

```http
Authorization: Bearer <API_TOKEN>
```

默认基址为 `http://127.0.0.1:8765`。若修改了控制台端口或启用了 HTTPS，调用方必须同步修改基址。

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/v1/messages` | 创建文字、图片或文件发送任务 |
| `GET` | `/api/v1/messages/<task_id>` | 查询状态、进度、日志和结果 |
| `POST` | `/api/v1/messages/<task_id>/cancel` | 取消排队中或尚未提交发送动作的任务 |
| `POST` | `/api/v1/uploads` | 上传图片或文件原始二进制，获取一次性 `upload_id` |
| `POST` | `/api/v1/uploads/<upload_id>/cancel` | 清理尚未用于消息任务的上传 |

## 最短调用流程

### 发送文字

```http
POST /api/v1/messages
Authorization: Bearer <API_TOKEN>
Content-Type: application/json

{
  "request_id": "agent-20260817-0001",
  "kind": "private",
  "name": "文件传输助手",
  "message_type": "text",
  "text": "你好"
}
```

创建成功返回 HTTP `202`。保存 `task_id`，然后查询：

```http
GET /api/v1/messages/<task_id>
Authorization: Bearer <API_TOKEN>
```

### 发送图片或文件：必须优先采用两步上传

第一步，把文件本身作为请求体上传。不要把本地文件路径写进 JSON：

```http
POST /api/v1/uploads
Authorization: Bearer <API_TOKEN>
Content-Type: application/octet-stream
Content-Length: <文件字节数>
X-Media-Type: file
X-File-Name: %E6%B5%8B%E8%AF%95%E8%B5%84%E6%96%99.zip

<文件原始字节>
```

`X-Media-Type` 只能是 `image` 或 `file`；`X-File-Name` 使用 UTF-8 URL 编码。上传成功返回 HTTP `201`：

```json
{
  "ok": true,
  "upload_id": "一次性上传标识",
  "name": "测试资料.zip",
  "size": 123456,
  "media_type": "file",
  "expires_in_seconds": 1800
}
```

第二步，用 `upload_id` 创建消息任务：

```http
POST /api/v1/messages
Authorization: Bearer <API_TOKEN>
Content-Type: application/json

{
  "request_id": "agent-file-20260817-0001",
  "kind": "group",
  "name": "测试群聊",
  "message_type": "file",
  "upload_id": "一次性上传标识"
}
```

发送图片时只需把两处类型都改为 `image`。当前配置默认按图片最大 20 MiB、文件最大 100 MiB 校验；二进制上传不受普通 JSON 请求体 64 KiB 上限影响。

`upload_id` 的规则：

- 30 分钟内未使用会过期并清理。
- 只能被第一条成功创建的消息任务消费一次。
- 上传类型必须与消息的 `message_type` 一致。
- 相同 `request_id`、相同请求内容的网络重试仍返回原任务，不会再次消费或再次发送。
- 上传成功但决定不发送时，应调用 `/api/v1/uploads/<upload_id>/cancel`。

## 可直接交给 Agent 的 Python 示例

下面只使用 Python 标准库，演示上传文件、创建任务和轮询终态。调用方应把 `BASE_URL`、`API_TOKEN`、`FILE_PATH`、`CHAT_NAME` 作为自己的配置，不要把 Token 提交到仓库。

```python
import json
import mimetypes
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:8765"
API_TOKEN = "替换为控制台中配置的主动发送 API Token"
FILE_PATH = Path(r"C:\path\to\测试资料.zip")
CHAT_NAME = "文件传输助手"


def call(path, *, method="GET", json_body=None, binary_body=None, headers=None):
    request_headers = dict(headers or {})
    if API_TOKEN:
        request_headers["Authorization"] = f"Bearer {API_TOKEN}"
    data = binary_body
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(
        BASE_URL + path,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=130) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def wait_for_task(task_id):
    while True:
        task = call(f"/api/v1/messages/{task_id}")
        progress = task.get("progress") or {}
        print(
            task["status"],
            f'{progress.get("percent", 0)}%',
            progress.get("stage", ""),
            progress.get("message", ""),
        )
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(max(0.1, task.get("poll_after_ms", 500) / 1000))


payload = FILE_PATH.read_bytes()
media_type = "image" if (mimetypes.guess_type(FILE_PATH.name)[0] or "").startswith("image/") else "file"
uploaded = call(
    "/api/v1/uploads",
    method="POST",
    binary_body=payload,
    headers={
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(payload)),
        "X-Media-Type": media_type,
        "X-File-Name": quote(FILE_PATH.name, safe=""),
    },
)

created = call(
    "/api/v1/messages",
    method="POST",
    json_body={
        "request_id": "agent-" + uuid.uuid4().hex,
        "kind": "private",
        "name": CHAT_NAME,
        "message_type": media_type,
        "upload_id": uploaded["upload_id"],
    },
)

finished = wait_for_task(created["task_id"])
print(json.dumps(finished, ensure_ascii=False, indent=2))
if finished["status"] != "succeeded":
    raise SystemExit("发送任务未成功，请根据 result 和 logs 处理；不要盲目重发。")
```

## 创建消息字段

| 字段 | 是否必填 | 说明 |
|---|---:|---|
| `request_id` | 是 | 8–128 个字母、数字、点、下划线、冒号或短横线；同一发送意图在重试时必须保持不变 |
| `kind` | 是 | `private` 或 `group` |
| `name` | 是 | 微信会话名称；同名结果当前选择搜索结果最上方一项 |
| `message_type` | 是 | `text`、`image` 或 `file` |
| `text` | 文字必填 | 要输入的文字；沿用当前真实 `@`、输入方式和随机等待配置 |
| `upload_id` | 媒体推荐 | `/api/v1/uploads` 返回的一次性标识 |
| `source/file/url/path` | 高级兼容 | 与 `upload_id` 二选一；仅用于已明确配置的 URL、Base64 或受控本地路径 |
| `filename` | 否 | 媒体显示名称提示，最长 180 字符；使用上传接口时默认沿用上传文件名 |

不要同时提供 `upload_id` 与 `source/file/url/path`。本地路径仅在其规范化结果位于 `media.allowed_local_roots` 时才允许；普通 Agent 集成不需要使用这一模式。公网 URL 受 HTTPS、重定向、大小和地址安全规则约束。`data:*;base64,...` 与 `base64://...` 只适合不超过 JSON 请求体限制的极小内容。

## 异步队列、进度与日志

任务状态共有：

- `queued`：等待前面的桌面任务完成。
- `running`：正在操作微信。
- `succeeded`：发送动作已按协议完成。
- `failed`：发送前或执行中失败，查看 `result.code`、`result.message` 和 `logs`。
- `cancelled`：任务在可取消阶段被停止。

任务查询结果示例：

```json
{
  "ok": true,
  "task_id": "...",
  "request_id": "agent-20260817-0001",
  "status": "running",
  "queue_position": 0,
  "progress": {
    "percent": 76,
    "stage": "点击聊天输入区",
    "message": "聊天输入区域点击完成；本步 734 ms"
  },
  "logs": [],
  "poll_after_ms": 500,
  "result": null
}
```

`progress.percent` 是按桌面自动化阶段计算的进度，不是剩余时间预测；整体重试时百分比不会倒退。日志包含时间、累计耗时、操作名和单步耗时。多个请求严格按提交顺序逐条执行，队列最多保留 20 个待处理任务。

“收到消息至开始发送的最短间隔”只属于 WeFlow → AstrBot 的被动回复链路，不对主动发送 API 生效。点击等待、逐字/剪贴板输入、真实 `@`、发送前检查停顿、整体重试、窗口/DPI 和输入保护等自动化配置仍然生效。

## 取消任务

```http
POST /api/v1/messages/<task_id>/cancel
Authorization: Bearer <API_TOKEN>
```

停止微信自动化会立即取消尚未开始桌面操作的任务；正在执行的任务通过取消信号尽快停止。若发送按钮已经点击，任务会保留成功或 `sent_unverified`/`media_sent_unverified` 结果，避免调用方因取消结果误重发。重新启动自动化后，Agent 应先查询原任务，再决定是否创建新的发送意图。

## 常见错误与正确处理

| HTTP/错误码 | 原因 | Agent 应如何处理 |
|---|---|---|
| `401 active_api_unauthorized` | Token 错误，或未配置 Token 时仍发送了 Bearer | 核对当前页面保存的 Token；不要猜测或改写配置 |
| `401 active_api_local_only` | Token 留空时从非回环地址调用 | 配置 Token；不要开放无鉴权的远程调用 |
| `403 active_api_disabled` | 主动发送 API 未开启 | 提醒用户到控制台开启并保存 |
| `409 automation_stopped` | 微信自动化未开启 | 提醒用户在首页开启微信自动化；不要求启动桥接 |
| `409 active_api_queue_full` | 已有 20 个待处理任务 | 保留当前发送意图，等待后重试；不要生成一批新 `request_id` |
| `409 request_id_conflict` | 同一个 `request_id` 被用于不同内容 | 查询原任务；新发送意图必须换新 ID |
| `400 active_api_upload_not_found` | `upload_id` 未完成、已消费或已过期 | 若不是相同请求的网络重试，重新上传并创建新的发送意图 |
| `400 active_api_upload_type_mismatch` | 上传类型与消息类型不同 | 使用相同的 `image` 或 `file` 类型重试；此时上传仍保留 |
| `400 media_local_path_blocked` | Agent 错误地传了未授权本地路径 | 改用 `/api/v1/uploads`；不要为了绕过错误扩大白名单 |
| `413 media_too_large` | 超过当前图片或文件大小上限 | 缩小文件或由用户调整媒体大小配置 |
| `404 Not found`（仅上传端点） | 当前监听进程仍是旧版本 | 停止占用当前端口的旧 v3 进程并重新启动当前版本；不要换端口 |

对于未知错误，Agent 应返回 HTTP 状态、响应中的 `code/error/details`、原 `request_id` 和任务日志摘要，但不得打印 Token，也不得擅自重启、改白名单或重复发送。

## 发送结果的安全边界

文字任务的 `result.code=sent_unverified`、媒体任务的 `result.code=media_sent_unverified` 都表示微信中的发送动作已经提交。项目刻意不依赖聊天气泡检测来证明最终送达，因为气泡检测失败后自动重发可能造成刷屏。Agent 应把它们作为“已提交但最终送达未验证”呈现给用户，而不是重试。

当前 API 进度和自动化日志能证明程序执行到了哪一步，但不能证明对方设备已经收到消息。真实 WeFlow → AstrBot → 微信链路是否正常，也不能仅靠主动 API 的成功结果推断。
