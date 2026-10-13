# Windows 验证与合入门槛

## 初始化检查（历史）

本轮为克隆、分支和文档初始化，没有修改业务代码，没有实现 Windows app。检查文档路径、索引、占位符、分支基线和 diff；语法检查仅作为现有 Web 基线检查。尚未执行 Windows 10/11 应用验收或安装包验证。

2026-10-12 本机实际检查结果：

- Python 指定模块及 tests 的 compileall 检查通过；Web JavaScript 语法检查通过。
- 本轮 13 份仓内 Markdown 文件的 29 个相对链接及两级 AGENTS.md 入口检查通过，无残留模板占位符或编码异常。
- 暂存内容的 `git diff --cached --check` 和公开提交扫描通过，仅规则与文档变化。
- 本地 main、origin/main 和 Web 备份均指向原始基线提交。
- 没有安装额外依赖或执行 pytest、Web 实际运行、Docker、真实云、Windows 10/11 或安装包验收；本轮验证结论限于分支、文档与语法。

## 2026-10-12 Windows 开发阶段实测

环境：Windows 11 Enterprise x64，10.0.26200；Python 3.13.14、PySide6/Qt 6.8.3、PyInstaller 6.16.0、FFmpeg 7.1（imageio-ffmpeg 0.6.0 的 Windows 二进制）。这些为本次环境记录，不作为未经实测的其他系统结论。

- `python -m pytest -q`：119 项通过，包含原有离线业务回归、业务子进程启停、凭据保护、路径保护、NTFS Junction 拒绝、数据复制失败清理、生成中途退出与重新启动中断标记，以及真实 Windows 进程树退出检查。
- 测试有 1 条上游 Starlette TestClient/HTTPX 兼容弃用提示，来自原有接口测试入口；桌面运行时已改用公开 HTTPX ASGITransport，不依赖 TestClient。
- 原生窗口源码自检通过：七页面、实际 Tooltip 事件、供应商表单保存与 DPAPI 密文/不回显、2 文本 × 2 音色 × 2 份的 8 个 WAV、审核/分析、重复试听资源加载、跨页选择、48 kHz 真人文件归一化与重复导入、8 个未审核增强样本、四种 ZIP 导出、分组隔离、软删除/恢复、旧数据重定位、再次冷启动与关闭。
- 验证直接运行普通入口的 `QApplication.exec`、数据目录锁和自动退出后的业务子进程释放；并非仅验证自检入口。
- 便携 exe 的完整流程与缩放检查结果、产物路径记录于下方交付项；所有测试均使用应用自有临时目录、Mock 和合成的本地测试音频，未打开默认真实数据目录。

### 本地产物与最终验证

| 入口/产物 | 实际结果 |
| --- | --- |
| `artifacts/windows-final/source/result.json` | 源码原生流程 13 项检查通过，含 Tooltip 行为与热试听 |
| `artifacts/windows-verified/package/result.json` | 最终 exe 流程 13 项检查通过，退出码 0 |
| `artifacts/windows-verified/dpi150/result.json` | 最终 exe 在 `QT_SCALE_FACTOR=1.5` 下同样 13 项检查通过，退出码 0；合成主操作放在标题行 |
| `artifacts/windows-verified/normal/result.json` | 普通加锁入口/Qt 主事件循环启动、截图、关闭验证通过，无业务子进程残留 |
| `artifacts/windows-verified/relocated/result.json` | 解压到独立中文/空格目录，工作目录移出仓库，PATH 仅保留 System32、去除 Python/Qt/FFmpeg 开发环境覆盖后，同样 13 项通过 |
| `dist/TTS Dataset Studio/TTS Dataset Studio.exe` | 便携程序入口，必须保留同目录依赖 |
| `dist/TTS-Dataset-Studio-windows-x64-preview.zip`、同名 `.zip.sha256` | ZIP CRC 检查、解压实际运行通过；107646138 bytes，SHA-256 `d2bbdd1127c2e246c7f4469df7a22e4704d5fc2a0b3c79a7e95386c8fc53efc5` |
| `dist/TTS Dataset Studio/licenses/`、`dependencies.json` | 实际依赖版本、许可文件、FFmpeg version/buildconf/license 输出与上游源码入口 |

`artifacts/`、`build/`、`dist/` 为本轮本地产物，已忽略，不提交运行数据、截图或二进制；上方结论和复现命令自包含于仓内文档，读者不需依赖这些本地文件才能理解验证范围。

### 实测发现与修复

1. `os.fchmod` 在 Windows 不存在：保存采用可用的文件标志操作，云凭据保密由 DPAPI 提供，不假装 chmod 是 Windows ACL。
2. SQLite Connection 的 `with` 只结束事务，不关闭句柄：导入显式 closing 后才移动 staging 目录，失败路径同样释放连接，Windows 文件占用回归通过。
3. 生成样本旧状态 pass/reject 与交互审核 accepted/rejected 不同：查询按同义状态分组，既有记录不重写；两种状态的筛选回归通过。
4. 自检嵌套 `QTest.qWait` 在反复切页时停滞：验收驱动使用普通事件队列及短时间 Python yield，使超时可检查。UI 后台不操作 Qt 对象，主线程处理已完成 Future。
5. 热试听可能与媒体后端仍持有的缓存文件冲突：每次使用新快照，重复播放加载及退出清理通过。声音效果仍需人实际听辨。

### 验证边界

Windows 10、真实麦克风录音/拒绝权限/设备拔插、真实云账号与人耳播放质量未实测；检测到设备不等于录音成功。用户确认暂无 Windows 10 环境，先保留开发分支，不合入 main。不宣称公开安装包发布、代码签名、其他 CPU 架构、长期压力运行或完整发布许可审计已完成。

## 2026-10-13 安装与发布阶段

新增版本编码及 Win32 安装保护回归后，`python -m pytest -q` 为 128 项通过，1 条原有上游弃用提示。安装器以 Inno Setup 6.7.3 编译，后续使用实际发布产物按 [安装发布流程](../releases/windows-installer-release.md) 验证；具体安装与上传结果另行记录，不将编译成功视为安装成功。

实际安装发现 Inno Setup 在处理 [Registry] 后重建卸载登记，先写入的版本字段丢失；已改为 ssPostInstall 写入，并完善验证器对缺失字段的报告。应用在加载 Qt 和启动后端之前持有安装保护 mutex，覆盖启动期间的文件替换竞态。降级边界检查还发现 Inno 6.7.3 ComparePackedVersion 按有符号 Int64 比较，65535 主版本会排到 2.x 前；已先比较符号组再比较同组 packed 值，普通较高版本和 65535 边界的诊断安装器均实际拒绝降级。此前失败产物与证据留在忽略目录，仅卸载核对路径属于本轮的测试安装，不触及真实用户数据。

## 现有回归入口

工作目录为代码仓库根目录。以下入口已核对 `CONTRIBUTING.md` 和现有 CI，不意味着本机已经全部执行通过：

| 检查 | Shell 与命令 | 条件和范围 |
| --- | --- | --- |
| Python 语法 | PowerShell：`python -m compileall -q app.py audio_utils.py db.py project_settings.py providers.py settings_store.py voice_catalog.py tests` | 只做语法检查，不导入应用 |
| Web JavaScript 语法 | PowerShell：`node --check static/app.js` | 需要 Node.js，仅 Web 前端 |
| 业务测试 | PowerShell：`python -m pytest -q` | 先在隔离环境安装 `requirements-dev.txt` 和 ffmpeg；使用临时数据与 Mock |
| 公开内容扫描 | Git Bash：`bash scripts/check_public_release.sh` | 扫描暂存内容，暂存前后的范围要明确 |

现有 CI 运行于 Ubuntu，不能用 Linux CI 成功证明 Windows 10/11 支持。Windows 构建、安装包命令在框架和打包方式确定后补入，不执行模板命令。

## 合入 main 的必要证据

- 现有业务回归及新增桌面入口检查通过：Mock 批量生成、任务并发/失败、导入、审核、软删除/恢复、增强和全部导出格式。
- 核对实际 WAV 参数、JSONL 字段、WeKWS 文件、ZIP 内容与分组隔离；不能只看命令退出码。
- 在 Windows 10 和 Windows 11 分别记录系统版本/build、架构、安装包或产物、启动入口、操作与结果。只覆盖一个系统时，另一个标记待验证，不提前合入。
- 验证原生窗口启动、长任务期间 UI 响应、重复打开/关闭、任务中途退出、录音/播放设备释放、无残留进程或锁定文件。
- 验证录音停止/试听/重录/连续导入、拒绝权限、缺少设备、依赖缺失、云配置不可用和任务失败，不伪造可用状态。
- 验证用户目录、中文/空格路径、只读/无权限目录、文件占用、配置持久化、日志和错误脱敏；不操作用户真实数据。
- 验证旧 Web 数据的备份、导入、兼容和恢复；升级与卸载按最终分发方式验证，不能误删用户数据。
- 以实际截图和交互检查简约现代视觉、中文长文本、窗口缩放、DPI；相关内容旁有带圈问号，悬停显示必要解释/要求，多余说明不常驻。
- 明确记录真实云验证是否执行。未授权时用 Mock 验证流程并列出真实供应商待验证项，不把 Mock 结果描述为真实云成功；真实云验证是否为当前验收必要项按迁移影响核定。

受设备或环境阻塞时保留证据和待办；不能用“编译通过”替代实际运行。只清理本轮创建、身份与边界已确认的测试资源。
