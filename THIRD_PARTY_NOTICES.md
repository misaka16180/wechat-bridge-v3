# 第三方依赖说明

WeChat Bridge v3 本身采用 MIT License。运行时会通过 `requirements.txt`
安装以下第三方 Python 软件包；这些软件包分别遵循其上游许可证。轻量源码 ZIP 不携带这些
wheel；另行发布的完整依赖包与整合版会原样收录上游 wheel，wheel 内保留其许可证和版权文件。

| 软件包 | 用途 | 上游许可证 |
| --- | --- | --- |
| pywin32 | Windows 窗口、剪贴板和系统接口 | PSF License |
| comtypes | 只读获取 Windows 系统托盘按钮名称与矩形 | MIT License |
| Pillow | 截图与图像格式处理 | HPND License |
| NumPy | 图像数组运算 | BSD-3-Clause |
| opencv-python-headless / OpenCV | 模板匹配 | MIT / Apache-2.0 |
| Requests | WeFlow HTTP/SSE 通信 | Apache-2.0 |
| certifi | HTTPS 证书集合 | MPL-2.0 |
| charset-normalizer | HTTP 文本编码识别 | MIT License |
| idna | 国际化域名处理 | BSD-3-Clause |
| urllib3 | HTTP 连接基础组件 | MIT License |
| websockets | AstrBot WebSocket 通信 | BSD-3-Clause |

完整许可证文本、版权信息和最新依赖关系以各软件包安装内容及其上游项目为准：

- https://github.com/mhammond/pywin32
- https://github.com/enthought/comtypes
- https://github.com/python-pillow/Pillow
- https://github.com/numpy/numpy
- https://github.com/opencv/opencv-python
- https://github.com/psf/requests
- https://github.com/certifi/python-certifi
- https://github.com/jawah/charset_normalizer
- https://github.com/kjd/idna
- https://github.com/urllib3/urllib3
- https://github.com/python-websockets/websockets
