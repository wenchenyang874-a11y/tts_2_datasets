# Windows 迁移架构与当前实现

## 已查证的 Web 基线

基线提交：`fcbcbb22f84797dfe9b17b81da0c9eea444fe38d`，提交说明 `feat: release interactive sample review v1.2.3`。该说明是原提交信息，不代表本轮发布了版本。

| 模块 | 当前职责 |
| --- | --- |
| `app.py` | FastAPI 路由、参数模型、任务池、生成、样本、增强和导出 |
| `providers.py`、`voice_catalog.py` | 云供应商与 Mock 适配器、参数能力、音色目录 |
| `audio_utils.py` | ffmpeg 转码、WAV 检查、分析和确定性噪声混合 |
| `db.py` | SQLite 元数据、schema 与迁移 |
| `settings_store.py` | 供应商凭据、脱敏查询和配置保存 |
| `project_settings.py` | 唤醒词配置与标签推导 |
| `static/` | HTML/CSS/JavaScript，浏览器录音与交互 |
| `tests/` | 现有离线业务和接口回归测试 |

当前使用 Python/FastAPI、进程内线程池和 SQLite；`TTS_APP_WORKERS` 控制后台任务线程数，Web 服务只使用一个 Uvicorn worker。录音依赖浏览器安全上下文和 MediaRecorder。这些是当前实现，不能直接当成原生桌面设计。

## 迁移边界

目标依赖方向为原生 UI → 应用用例/任务调度 → 供应商、音频和持久化。UI 负责输入与反馈，业务模块负责规则和资源生命周期；复用现有适配器与数据契约，不在多个界面事件中重复生成、导出或数据库逻辑。

方案以维护成本、Windows 10/11 兼容、麦克风/播放、打包体积、退出清理和测试能力为依据，当前决策见下节。内部通信方案不能改变“原生 UI”要求，用户启动应用不应被要求打开 Web 页面或部署 Docker。

## 2026-10-12 桌面设计决策

用户已确认 x64 Windows 10 1809 及以上、Windows 11，以及包含运行依赖的便携包。采用 PySide6 6.8.3 / Qt Widgets 原生桌面控件；Qt 6.8 的 [官方支持表](https://doc.qt.io/qt-6.8/supported-platforms.html)覆盖该 Windows 范围。框架支持与项目实际验收分开记录。

`desktop_main.py` 显式创建数据目录、持有每数据目录的 QLockFile、创建业务子进程和原生窗口。UI 不导入 app.py。业务子进程先设置数据位置和凭据保护策略，再导入 app；通过内存 ASGI 调用复用现有接口、校验和任务语义。进程间使用 multiprocessing Pipe，不监听 HTTP 端口、不启动网页。

原生 UI 的请求通过单个后台线程队列串行发送，业务本身继续真实并发执行父子任务；主线程定时取已完成 Future，后台不操作 Qt 对象。内存网关使用 [HTTPX ASGITransport](https://www.python-httpx.org/advanced/transports/)，显式拥有事件循环；目前 app.py 无 lifespan 处理器，未来引入时须同步驱动生命周期。

服务调用超时后停止服务，不能自动重发可能计费的请求。关闭先停录音/播放，给业务子进程有限退出时间，超时则终止；未完成任务在下次启动如实标为中断。Windows 使用 [Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)管理后端及其 FFmpeg 子进程，最后一个 Job handle 关闭时终止进程树，避免只杀 Python 而留下仍写文件的音频进程；测试覆盖真实派生子进程退出。

默认数据在当前用户 LocalAppData，可通过 `--data-dir` 配置。供应商配置在桌面进程中以 [Windows DPAPI 用户绑定方式](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)加密，密钥不放在应用旁；导入明文旧配置后立即加密。Web/Linux 配置保持原有存储方式，保护开启条件明确，不把 chmod 当成 Windows ACL。

录音使用 QtMultimedia 的 QAudioSource，按设备实际支持的格式写 PCM16 WAV，再经现有 importer 归一化。播放使用 QMediaPlayer，资源复制到应用自有临时目录；退出停止播放并释放缓存。Plot 使用 QPainter 绘制波形和频谱，没有网页组件。

PyInstaller 生成 one-directory 包，含 Python、Qt、业务依赖和 imageio-ffmpeg 的 FFmpeg 二进制。共享 Qt DLL 可替换；实际版本清单与许可证从本次构建环境收集。构建脚本为 `scripts/build_windows.ps1`，源自检/产物自检入口为 `--smoke-test`。真实设备、系统和云端验证不被离线自检代替。

旧数据通过 `desktop/data_transfer.py` 导入空目录，SQLite.backup 保留一致快照，复制文件并将文件路径重定位到新根；拒绝目录包含关系、符号链接、NTFS reparse/Junction、越界路径和活跃记录缺文件，原数据不写入。调用前必须停止原 Web 服务保证文件与数据库一致。

## 2026-10-13 安装与版本设计

Inno Setup 的每用户安装器将程序放在 LocalAppData/Programs，应用数据仍在独立数据目录；升级覆盖登记的程序文件，卸载只删除安装器记录的内容，保留数据库、音频、凭据和未知用户文件。固定 AppId 维持升级身份，版本字段在 ssPostInstall 写入卸载登记，避免被安装器重建登记覆盖。

启动入口在导入 Qt 前持有 `desktop/installer_guard.py` 的 Local 命名 mutex，直到 UI 及业务进程退出才释放。该标记对应当前 Windows 交互会话的安装/卸载保护；每数据目录的 QLockFile 另行保护同一数据目录，不禁止使用不同目录的多个实例。安装器不通过 Restart Manager 强制关闭应用。

`app_version.py` 为应用/API、PE、安装器、文件名和 Tag 的单一版本来源。构建清单绑定源提交与资产哈希；安装验证和回读证据绑定同一清单。先建 Release 草稿、上传并校验完整远程资产，再公开；发布工具故障修复可追加开发提交，已发布 Tag 不移动。详细流程见 [安装与版本发布](../releases/windows-installer-release.md)。

## 已发现的迁移注意点

- `app.py` 在模块导入时设置 umask、创建数据目录、初始化数据库、修改中断任务并创建线程池。迁移时把初始化/关闭职责显式设计出来，避免 UI 引用模块就触发持久化副作用；进程重启不能伪造任务续跑。
- 直接 Python 运行的默认数据路径在源码目录的 `data/`；Docker Compose 的常见宿主机路径为 `var/`。README 对默认数据位置的描述需要按运行方式区分，不把 `var/` 当作所有入口的统一默认值。
- POSIX `0600` 或 umask 不能证明 Windows 凭据已受保护。确定 Windows 数据目录和保护方案后验证实际读写权限、日志脱敏及备份行为。
- ffmpeg 为当前转码外部依赖；桌面版需确定发现/分发方式、缺失提示和第三方许可，不能只在开发机 PATH 可用时宣布安装包可用。
- Windows 路径、中文、空格、反斜杠、只读目录、文件占用和导出路径必须实际覆盖。无音频复制的清单须明确训练端可访问路径。
- Win10/11 的麦克风权限、设备不可用、设备切换、播放/录音关闭路径不能沿用浏览器 HTTPS 机制作为验收。

旧文档说明 Web 现状；需求说明目标行为。发现二者或源码之间有差异时记录事实并核对，不能通过改写需求消除差异。

## 2.0.0-beta.2 决策

桌面数据路径偏好放在当前用户应用配置目录的 desktop_preferences.json，独立于数据目录和供应商凭据。原子写入并探测目标可写性，仅更改下次启动路径；不在后台任务运行期间切换数据库。命令行路径优先，错误偏好不静默回退到空数据目录；冻结程序禁止数据目录与安装树重叠。

表单验证通过请求体传递未保存内容，服务端合并已存秘密并保留留空的密钥，仅做参数校验、不持久化；保存成功和后续音色获取结果分开反馈，目录失败不将有效保存回滚。小米未公开账号音色枚举接口，本版维护官方预置快照，不伪造远端账号可用性。

小米通过 [MiMo TTS 官方接口](https://mimo.mi.com/docs/zh-CN/api/audio/tts)发送 assistant 目标文本、可选 user 风格指令并解码 Base64 音频。豆包 1.0 请求不变，2.0 按 [V3 HTTP 官方接口](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-http?lang=zh)使用 X-Api-Key 和模型资源 ID；有界收集 JSON 音频分段，任一分段错误即丢弃部分音频，计费 POST 不重试。两类新请求都进入现有归一化、样本与分组导出流程。

帮助控件不只依赖平台延迟的 ToolTip 事件，通过真实 Enter/Hover 与键盘聚焦驱动不激活窗口的 Qt Tooltip；纯文本、屏幕边界约束、移出/失焦清理。验收需要实际鼠标位置命中当前窗口控件，不能直接注入帮助事件。设置已移除 Web 导入 UI 入口，内部数据复制仍供维护回归。
