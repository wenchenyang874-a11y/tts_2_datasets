# Third-Party Notices

本仓库不包含或再分发云厂商模型权重、账号音色、生成音频或真人录音。

## Runtime dependencies

运行时依赖列在 `requirements.txt`，主要包括 FastAPI、Uvicorn、HTTPX、NumPy 和 python-multipart。Docker 镜像还安装 Python 官方基础镜像、Debian 系统组件、FFmpeg 与 tini。它们分别受各自许可证约束；本文件不是完整许可证清单，也不会改变任何第三方许可证。

部署者在重新分发容器镜像前，应对实际构建版本生成软件物料清单并核对许可证与安全公告。

## Windows desktop dependencies

桌面依赖列在 `requirements-desktop.txt`。原生 UI 使用 PySide6 / Qt 6.8.3（LGPLv3/GPLv3 或商业许可），以 one-directory 形式动态加载 Qt DLL，不使用 Qt WebEngine。完整包中的 `licenses/` 收集实际安装依赖自带的许可文件，`dependencies.json` 记录构建版本；Apache-2.0 项目许可不替代这些第三方许可。

音频转码通过 imageio-ffmpeg 0.6.0 包中的 FFmpeg 可执行文件（或用户配置的 FFmpeg）。Python 包与 FFmpeg 二进制使用各自许可，重新分发时核对实际二进制的 `-version`、`-buildconf` 和 `-L` 输出，并保留对应许可和源码入口，不把包装模块的许可视为二进制许可。

- [Qt for Python 许可与项目](https://doc.qt.io/qtforpython-6/)
- [imageio-ffmpeg 源码、二进制说明与构建工具](https://github.com/imageio/imageio-ffmpeg)
- [FFmpeg 许可与源码](https://ffmpeg.org/legal.html)

Windows Release 同时包含完整许可目录与依赖清单。安装器使用 Inno Setup 6.7.3，简体中文社区译文源自其 is-6_7_3 标签，保留上游署名；版本与实际分发状态见 docs/releases/。

## Cloud services

火山引擎和阿里云接口、商标、音色、模型及生成内容受各自服务协议和账号授权约束。代码能够列出音色或发起请求，不代表：

- 账号已经获得该音色或模型的使用权限；
- 生成音频可以不受限制地商业使用或再分发；
- 复刻音色已经获得原说话人授权；
- 本项目得到相关厂商官方认可或背书。

使用者应自行核对最新服务条款、地域规则、价格、数据处理要求以及音频来源许可。

云端请求可能产生费用。本项目不销售、代收或代付第三方服务；凭据所属账号的费用由账号持有人和实际使用者承担。完整说明见 [使用、计费与责任说明](DISCLAIMER.md)。
