# Windows 迁移架构与当前实现

## beta.6 交互与刷新契约

`desktop/form_wheel.py` 的 FormWheelGuard 由窗口持有，在应用级过滤该窗口及其弹窗拥有的表单控件事件，覆盖数字框内的 line edit、编辑器 viewport 和即时切换后的新页面。控件即使聚焦也不处理滚轮；传统滚轮复制事件到外层 QScrollArea，像素型触控板直接应用原方向位移。没有外层页面的弹窗只保持值不变。下拉框展开后的列表仍可浏览；只读日志内部滚动不受影响。只设置焦点策略会漏掉已经聚焦的控件，单纯吞事件会制造无法滚动的区域。

SampleTable 默认以 currentRow 标识唯一当前行，各列依当前行同步着色；多选模式才依据首列 CheckStateRole 绘制框与整行背景。进入／退出模式重排行高，退出清空跨页批量集合；逐行播放按钮为真正的 40×34 QPushButton，可键盘聚焦，不改变样本选择。右键菜单及异步操作捕获稳定样本 ID，刷新重排不会误操作其他音频。审核子菜单明确父对象并保留 Python 包装，避免 PySide 6.8.3 中原生创建子菜单的生命周期问题。

单条导出使用 QFileDialog 的目录选择和业务进程的 samples copy 操作，直接复制 WAV；不走 datasets export API，不生成 ZIP／JSONL。同名目标使用新文件名，保留旧文件。现有训练集导出和音频契约不变。

任务树刷新期间暂停选中信号，按 ID 恢复当前任务；技术详情内容相同时不调用 setPlainText，内容变化时恢复滚动条和有边界的 cursor／anchor。切换到其他任务才重置阅读位置，避免周期轮询先清空选择再把文档重置到顶部。

费用常驻勾选框移除；预检通过且用户确认本次云批次后才给该不可变请求快照设置 cost_acknowledged。豆包指令名单来自 [官方音色表](https://docs.volcengine.com/docs/DoubaoVoice/Tonelist-1)，仅明确标注“指令遵循”的 93 款 2.0 音色进入自由指令分类，不根据模型名／ID 后缀猜测。原 API additions.context_texts 结构保持 [官方 HTTP 说明](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-http?lang=zh) 要求，原始设置与实际供应商参数继续写入样本元数据；接口成功不等于模型风格效果已验收。

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

桌面数据路径偏好放在当前用户应用配置目录的 desktop_preferences.json，独立于数据目录和供应商凭据。原子写入并探测目标可写性，beta.2 曾仅更改下次启动路径，beta.3 已由下节的即时会话切换替代；任务执行中不允许切换。命令行路径优先，错误偏好不静默回退到空数据目录；冻结程序禁止数据目录与安装树重叠。

表单验证通过请求体传递未保存内容，服务端合并已存秘密并保留留空的密钥，仅做参数校验、不持久化；保存成功和后续音色获取结果分开反馈，目录失败不将有效保存回滚。小米未公开账号音色枚举接口，本版维护官方预置快照，不伪造远端账号可用性。

小米通过 [MiMo TTS 官方接口](https://mimo.mi.com/docs/zh-CN/api/audio/tts)发送 assistant 目标文本、可选 user 风格指令并解码 Base64 音频。豆包 1.0 请求不变，2.0 按 [V3 HTTP 官方接口](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-http?lang=zh)使用 X-Api-Key 和模型资源 ID；有界收集 JSON 音频分段，任一分段错误即丢弃部分音频，计费 POST 不重试。两类新请求都进入现有归一化、样本与分组导出流程。

帮助控件不只依赖平台延迟的 ToolTip 事件，通过真实 Enter/Hover 与键盘聚焦驱动不激活窗口的 Qt Tooltip；纯文本、屏幕边界约束、移出/失焦清理。验收需要实际鼠标位置命中当前窗口控件，不能直接注入帮助事件。设置已移除 Web 导入 UI 入口，内部数据复制仍供维护回归。

## beta.3 即时数据目录切换

目录锁由桌面会话拥有，不绑定启动函数的局部变量。切换期间禁用页面与后台轮询，等待已提交界面请求完成，检查业务任务及录音状态，释放播放资源。先取得目标目录锁；迁移模式建立经 SQLite 完整性检查和路径重定位的暂存数据，新服务就绪且偏好原子写入成功后才移除旧位置对应文件。目录切换事务保留旧锁和回退能力，任何准备或启动失败恢复旧服务。成功后刷新所有数据相关状态并清除跨目录选择。旧目录存在其他实例锁时不可迁移或开启。该操作仅由用户选择目录触发。


beta.3 的两套豆包配置分别存于 volcengine / volcengine_v2，独立保存、清空和估价。桌面服务 ID 为 volcengine_v1 / volcengine_v2，语音行按对应模型过滤；目录刷新只更新对应范围，不能禁用另一版本音色。实际合成前进行模型对应凭据预检，缺失不创建任务，不发请求。当前阶段不做旧配置转换。

Base URL 与请求路径分离：小米 /v1 → /v1/chat/completions，豆包基础主机分别补齐 /api/v1/tts 和 /api/v3/tts/unidirectional，阿里 /api/v1 → /api/v1/services/audio/tts/SpeechSynthesizer。主机允许列表与 HTTPS 校验仍执行，完整路径为显式高级配置。阿里路由依据[官方 HTTP 文档](https://www.alibabacloud.com/help/en/model-studio/audio-generation-api)，不通过修改标签而继续向基础主机根路径发送合成请求。

## beta.4 统一 V3 与地域路由

用户要求取消地址输入，并指定 [当前 HTTP 单向流式文档](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-http?lang=zh)。本节替代 beta.3 的地址表单和旧 1.0 合成方式。两个豆包模型使用同一固定 V3 地址，通过 X-Api-Key、X-Api-Resource-Id 与随机请求 ID 调用；资源与独立凭据按所选模型绑定，不能在一套密钥缺失时借用另一套。1.0 字符版资源为 seed-tts-1.0，2.0 字符版为 seed-tts-2.0；账号复刻 1.0 使用 seed-icl-1.0。复刻音色管理仍有独立 AK/SK 与应用 ID，不能将其误作合成要求。

WAV 请求使用 PCM，分段组装后一次写入单声道 PCM16 WAV 文件头，避免 V3 流式 WAV 重复文件头导致损坏。请求显式要求用量；支持官方当前示例的 code=0 与最终 usage，以及 V3 传输参考的 20000000 完成记录。任何错误分段、无音频或缺失完成信息都不返回部分结果，计费 POST 不隐式重试。此处理是当前云端协议的响应解析，不是旧应用配置兼容。

供应商配置持久化不再接受 endpoint/v3_endpoint/catalog_endpoint/customization_endpoint、Access Token 或 Cluster。阿里云地域枚举为 cn-beijing/ap-southeast-1，合成和音色查询共用同一地域与可选 Workspace ID；按官方域名规则生成两个完整路径。界面默认北京，问号说明密钥与地域一致；格式预检不发云请求，也不把地域切换视为账号授权成功。

## beta.5 音色能力与交互

`voice_features.py` 是 UI 和后端共同使用的纯规则模块。语言来自音色目录，中文方言代码归中文，其他明确语言归外语，未知保持未知；多语言可属于两类。`GET /api/voices` 增加 language_group / capability 筛选和 expression_features 元数据，能力按供应商、模型、音色及目录情感声明共同判定。

合成按钮同步校验全部已选音色，弹窗保留表单；后端预检、单任务和批任务创建再次验证数据库中的实际音色，拒绝请求发生在任务提交之前。指令草稿不会在勾选不支持的音色时被清空。固定句式与阿里云字符计数也在共享规则中验证，不能把 unsupported 当作 harmless no-op。

豆包 2.0 自由指令使用 additions.context_texts；原生情感只依据该音色 ListSpeakers.Emotions 的 Value/Label，发送 audio_params.emotion / emotion_scale。没有原生枚举的 2.0 音色、MiMo 和支持自由指令的阿里云复刻音色，将 UI 情感预设转为自然语言表达要求；阿里云系统音色使用官方固定句式。情感预设不插入待朗读文本。样本元数据同时保存原始设置和实际供应商表达参数，去重条件包含 emotion / emotion_scale，避免不同情感误用旧音频。

共享 AnchoredComboBox 使用独立 Fusion 代理风格和明确列表样式，固定控件宽度及屏幕边界。Windows 6.8.3 在页面即时重建与反复打开时出现 private popup 未显示问题，每次打开重置为顶层窗口拥有的 Qt.Popup，再按固定位置显示；仍保留 Qt 选择和键盘处理。代理样式不能共享 QApplication.style() 的所有权。实际首／中／末选项、鼠标点选、方向键与 Enter、Escape 和即时切换后的重复打开均需回归。

音色列表使用轻量绘制委托，名称、人口信息、语言、能力分层固定坐标绘制；鼠标整行切换 checkbox 状态，禁止同时交给默认 checkbox 编辑器造成双重切换。样本表格改用 QStyledItemDelegate 保持选中前后的换行，选择列和短状态列固定宽，文本／来源列伸展，长内容按行高显示。

播放控制属于样本、录音、噪声页面，仅播放状态可见。导航停止音频并使尚在复制的试听请求过期，即使立即返回同一页也不能偷偷重启；新试听继续使用独立缓存快照，保留 Windows 文件句柄释放边界。样本双击直接播放指定行，不执行审核或分析操作。

## beta.5 样本行选择修正

beta.5 仅收窄默认 checkbox 单元格，仍使用 Qt 委托的绘制／命中和普通当前单元格焦点。实际点击复现显示：默认样式下框绘制偏左，有效点击 x 范围偏右，同时当前格出现多余空白焦点框。单击其他单元格只改变 Qt current/selection，不改变业务的跨页勾选集合。列宽检查及直接设置 checkState 不能验证这一问题。

`desktop/sample_table.py` 的 SampleTable 将 checkbox 与文本合并为第一列，保留六个信息列。SampleDelegate 明确绘制 checkbox，去掉默认 HasCheckIndicator／HasFocus 面板，文本用同一 QTextLayout 计算行高与绘制。其他列按首列 checkbox 状态着色，窗口／列宽变化重新计算行高；当前格与业务选择不混用。

鼠标 press 保存样本稳定 ID，release 在同一 ID 的行内才切换一次，不把同一事件继续传给 Qt checkbox 编辑器；双击确保 Checked 并触发已有播放入口，第二次 release 不反转。刷新可能重新排序／替换行，不能缓存行号或原 item 指针作为一次点击的身份。空格键在表格层统一切换，跨页 selected 仍由 SamplesPage 维护，后端音频／导出契约未改变。验收需要点击实际画出的框边／中心、间隙和其他列，并校验渲染像素与业务选择集合。

### 样本整行即时重绘

样本其他列的背景依赖第 0 列的 CheckStateRole，但 Qt 默认只使发生 dataChanged 的单元格失效，导致点击后第 0 列变色，其余列要等到其他事件才重绘。修复在模型 dataChanged 层识别该共享状态，向 viewport 排队更新相应可见行区域，覆盖全部六列；不使用计时器、网络刷新、同步 repaint 或整表刷新。空格、双击、程序修改以及页面暂时阻止 itemChanged 的更新都走同一入口。

QWidget.grab() 会主动完整绘制，可能把真实窗口的局部失效问题藏起来。验收新增隔离原生表格捕获实际 QPaintEvent.region，在点击、取消、空格、双击与程序更新后的首次绘制检查全部列命中，并确认相邻行未被无谓重绘。QScreen.grabWindow() 保存已呈现窗口，普通完整截图继续用于布局检查，不能替代本项首帧证据。
