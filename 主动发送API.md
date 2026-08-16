# 主动发送 API

v3 提供一个与 WeFlow/AstrBot 独立的机器调用入口。它不会等待收到微信消息，Agent 可以主动提交要发送的私聊或群聊消息。

## 启用与鉴权

在网页控制台的“连接设置 → 主动发送 API”中启用。Token 不限制长度，可手动填写，也可点击“生成”。随机生成后会直接显示，并支持一键复制；保存成功后当前页面继续保留，方便再次查看或复制，刷新整个网页后才清空。服务端只返回“是否已配置”，不会把已经保存的 Token 重新传回网页。

- 配置了 Token：请求必须带 `Authorization: Bearer <API_TOKEN>`。
- Token 留空：只允许本机回环地址调用；即使控制台监听局域网，也不会接受远程无 Token 请求。
- API 是否启用、Token 是否配置与 AstrBot/WeFlow 连接状态互不影响。主动发送 API 不要求启动桥接服务；只要本地网页控制台仍在运行、主动 API 已启用且微信自动化已启动，就可以创建和执行发送任务。停止桥接只断开 WeFlow/AstrBot 链路，不会取消主动 API 队列；停止微信自动化才会拒绝新任务并取消尚未提交的任务。

默认接口地址为：`http://127.0.0.1:8765/api/v1/messages`。若控制台启用了 HTTPS，应使用 `https`。

## 创建发送任务

```http
POST /api/v1/messages
Authorization: Bearer <API_TOKEN>
Content-Type: application/json

{
  "request_id": "agent-20260816-0001",
  "kind": "private",
  "name": "文件传输助手",
  "message_type": "text",
  "text": "你好"
}
```

`request_id` 必填，且同一请求重试时必须保持不变。相同 `request_id` 和相同内容只会返回原任务，不会再次点击微信；同一 `request_id` 换了内容会被拒绝。

`kind` 为 `private` 或 `group`；`message_type` 支持 `text`、`image`、`file`。图片和文件可以使用 `source`、`file`、`url` 或 `path`：

```json
{
  "request_id": "agent-image-0001",
  "kind": "group",
  "name": "测试群聊",
  "message_type": "image",
  "source": "<已加入 allowed_local_roots 的目录>/demo.png",
  "filename": "demo.png"
}
```

本地媒体路径仍受 `media.allowed_local_roots` 限制；公网下载遵循媒体 URL 的 HTTPS、大小和重定向安全规则。真实 @ 继续使用 v3 当前配置的候选框等待、自动降级和重试策略。

创建成功返回 `202`，例如：

```json
{
  "ok": true,
  "task_id": "…",
  "request_id": "agent-20260816-0001",
  "status": "queued"
}
```

接口是异步队列：请求提交后立刻返回，不会让 HTTP 连接一直阻塞到微信操作完成。多个请求严格按提交顺序逐条执行；后来的请求保持 `queued`，不会与正在执行的任务争抢鼠标或键盘。状态结果还包含：

- `queue_position`：当前排队位置；进入执行后为 0。
- `progress.percent`：0–100 的阶段进度估计。
- `progress.stage` / `progress.message`：当前执行阶段与最近动作。
- `logs`：带时间、累计耗时、操作编号和单步耗时的执行日志。
- `poll_after_ms`：任务未结束时建议的下次轮询间隔。

进度是根据“准备窗口、定位搜索框、进入会话、写入内容、等待发送按钮、点击发送”等阶段计算，不是假装精确的剩余时间。出现整体重试时日志会明确记录，百分比不会倒退。

“收到消息至开始发送的最短间隔”只属于 WeFlow → AstrBot 的被动回复链路；主动发送 API 和自动化调试都没有“收到消息”的起点，因此不应用该参数。点击等待、输入方式、真实 @、整体重试、窗口/DPI 和输入保护仍使用当前自动化配置。

## 查询与取消

```http
GET /api/v1/messages/<task_id>
Authorization: Bearer <API_TOKEN>
```

任务状态为 `queued`、`running`、`succeeded`、`failed` 或 `cancelled`。`result.code` 为 `sent_unverified`/`media_sent_unverified` 时，表示发送按钮动作已提交，但没有把气泡观察当作最终送达证明；不要因为这个状态再次提交新 `request_id`。

需要取消尚未完成的任务时：

```http
POST /api/v1/messages/<task_id>/cancel
Authorization: Bearer <API_TOKEN>
```

停止微信自动化会立即把尚未开始桌面操作的主动 API 任务标记为 `cancelled` 并从队列移除；正在执行的任务通过取消信号尽快停止。若发送按钮已经点击，任务会保留成功/`sent_unverified` 结果，避免 Agent 因“取消”误重复发送。重新启动自动化后，Agent 应查询原任务状态，再决定是否创建新的请求。
