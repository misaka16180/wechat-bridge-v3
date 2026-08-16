# WeChat Bridge v3.0.0-rc.11

这是 Windows 电脑网页端发布候选版。rc.11 修复了关闭网页后重新打开时，页面长期停在“读取状态中”的会话恢复问题，并完善公开项目的功能介绍、软件截图与国内镜像下载入口。

## 本次修复

- 将“服务端已确认登录”和当前标签页临时 Token 分开管理。即使关闭页面后 `sessionStorage` 已清空，只要 HttpOnly Cookie 会话仍有效，首页状态、连接设置、视觉兼容性、识别快照、日志和媒体上传都会正常恢复。
- 页面统一显式携带同源 Cookie；注销不再依赖临时 Token，浏览器从页面缓存恢复时会重新确认登录状态。
- 刷新定时器集中启停，页面恢复不会叠加多个轮询任务。
- 补充 Cookie 独立鉴权、关页恢复、视觉状态恢复、注销与内联 JavaScript 语法回归。
- 主动发送 API 已与 WeFlow/AstrBot 桥接生命周期完全解耦：桥接从未启动或已经停止时，API 仍能创建、排队和执行任务；停止微信自动化才会拒绝或取消任务。
- 任务栏最小化的微信会优先通过已有主窗口句柄恢复，即使 Windows 暂时把该窗口报告为不可见，也不会误入通知区域分支；托盘失败文案也不再要求用户必须“缩到托盘”。

## 下载

| 文件 | 大小（字节） | SHA-256 |
| --- | ---: | --- |
| `wechat-bridge-v3.0.0-rc.11-source.zip` | 331884 | `cea05567fb6da062ee476fe25213c79a5f0defced39fb8765f1da712f6813dad` |
| `wechat-bridge-v3.0.0-rc.11-dependencies-win64.zip` | 177700699 | `eef7b005aae49cb65989fbd3324084742f295ad041933fb9b8687a4065fe5c44` |
| `wechat-bridge-v3.0.0-rc.11-integrated-win64.zip` | 178034451 | `f3b80571d9b6da7e21dcc399985b9e7e3bd97262f919fadb6e2752b637e76da3` |

国内镜像：

- 仅环境包：[夸克网盘](https://pan.quark.cn/s/786afbc34739)，提取码 `5HSf`
- 整合包：[夸克网盘](https://pan.quark.cn/s/fdafe9b0af54)，提取码 `G666`
- GitHub 源码镜像：[夸克网盘](https://pan.quark.cn/s/ab90b8029f4b)，提取码 `CtvM`

使用镜像时请确认文件版本、大小和 SHA-256 与本页一致；校验不一致的压缩包不要运行。

## 验证结果

- `320` 项自动化回归全部通过，Python `compileall` 通过。
- 发布白名单检查通过：`68` 个产品文件。
- 源码包隔离首次运行检查通过：`69` 个 ZIP 文件，包含内置 SHA-256 清单。
- 仅环境包和整合包离线安装检查通过：`31` 个 wheel，覆盖 Windows x64 / Python 3.10–3.14，pip 强制 `--no-index`。

真实微信发送操作由使用者在自己的测试会话中验收；离线测试和网页渲染检查不等于完整 WeFlow → AstrBot → 微信现场链已经通过。
