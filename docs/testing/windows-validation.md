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

新增版本编码及 Win32 安装保护回归后，`python -m pytest -q` 为 128 项通过，1 条原有上游弃用提示。安装器以 Inno Setup 6.7.3 编译，使用实际发布产物按 [安装发布流程](../releases/windows-installer-release.md) 验证。安装包结果为 7 项检查通过，安装后原生流程 13 项通过；草稿查找修复新增 3 项针对性测试通过，并在实际 GitHub 草稿上验证。

实际安装发现 Inno Setup 在处理 [Registry] 后重建卸载登记，先写入的版本字段丢失；已改为 ssPostInstall 写入，并完善验证器对缺失字段的报告。应用在加载 Qt 和启动后端之前持有安装保护 mutex，覆盖启动期间的文件替换竞态。降级边界检查还发现 Inno 6.7.3 ComparePackedVersion 按有符号 Int64 比较，65535 主版本会排到 2.x 前；已先比较符号组再比较同组 packed 值，普通较高版本和 65535 边界的诊断安装器均实际拒绝降级。此前失败产物与证据留在忽略目录，仅卸载核对路径属于本轮的测试安装，不触及真实用户数据。

### 安装包实际结果

环境仍为 Windows 11 x64 / 10.0.26200。版本 `2.0.0-beta.1`，构建与 Tag 源提交 `2d712c2207f3a4cc33d2bf58899b29c3c6499944`。

- `artifacts/installer-beta1-final/result.json`：ok=true，Windows 11 实测标志为 true；7 项安装检查、13 项安装后原生流程通过。
- 使用未修改的生产安装器，中文/空格隔离目录、无管理员权限、无快捷方式污染；全部安装文件与便携 ZIP 逐文件哈希一致，PE/卸载登记版本匹配。
- 运行保护拒绝升级和卸载；模拟较旧安装登记与损坏程序，升级成功修复且保留数据。普通较高版本与 65535 主版本边界均实际拒绝降级。
- PATH 仅 System32、移除 Python/Qt/FFmpeg 环境覆盖后运行安装后的 app，原生 Mock、音频、审核、增强、四种导出与重定位流程通过；普通数据目录加锁启动/退出通过，无后台进程残留。
- 卸载删除程序、运行依赖和本轮 HKCU 登记，保留独立数据与安装目录内的未知用户文件。未触及用户真实安装、数据或凭据。
- 并行回读工具不记录认证令牌或签名 URL；认证请求与资产请求分离，逐段核对 Content-Range 与长度，拼接后核对完整字节数与 SHA-256。

`artifacts/release-beta1-range-download/verification.json`：ok=true，安装包、便携包和 SHA256SUMS.txt 从 GitHub 完整回读，字节数与完整 SHA-256 全部匹配，Release 已公开为预发布、未设置为 Latest。版本文件、哈希和实际发布状态统一记录于 [2.0.0-beta.1](../releases/v2.0.0-beta.1.md)。Windows 10、真实麦克风、真实云与人耳播放质量继续待验收，main 不合入。

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

## 2026-10-13 / 2.0.0-beta.2

- Windows 11 10.0.26200 x64，新增小米、豆包 2.0、当前表单验证、目录偏好回归后，全套 154 项通过；修正一个测试断言的空正则提示后，相关 22 项专项再次通过，仅余原有上游弃用提示。
- `artifacts/beta2-source-final/result.json`：源码原生流程 16 项通过，包含实际鼠标命中帮助控件、悬停窗口文本、移出隐藏、键盘聚焦显示/失焦隐藏；未直接注入 ToolTip 事件。当前帮助弹层截图为 hover-help.png。
- 小米请求的消息角色、Base64 音频、固定采样率、失败/截断、费用确认、密钥脱敏与禁止计费 POST 重试均为模拟 HTTP 测试；豆包 2.0 的 API Key / resource、语速音量映射、JSON 分段汇总、分段失败丢弃及 1.0 回归已覆盖。
- 当前表单校验不落盘、空密钥保留、错误 Endpoint 拒绝；保存后自动载入官方预置音色、明显的手动按钮、2.0 模型筛选在真实原生窗口检查。
- 自定义中文/空格目录偏好保存与原数据不搬移已验证；安装后的程序使用保存偏好、无 --data-dir 冷启动，实际在所选目录建立数据库，正常加锁启动/退出通过。
- 检测到用户安装的 beta.1 正在运行，采用不同 AppId / AppMutexName 的隔离测试安装副本，不替换旧应用、不关闭用户进程。`artifacts/installer-beta2-final/result.json` 为 ok=true，8 项安装检查与安装后的 16 项原生流程通过；其运行文件与生产便携包逐文件哈希一致，生产安装器、副本与程序 PE 版本一致。
- 运行保护拒绝升级/卸载、模拟旧登记与损坏文件后的升级修复、两个较高版本边界拒绝降级、卸载删除程序和本轮登记且保留数据均通过。此结果限于隔离身份与模拟升级夹具，不代表真实用户原位升级。
- 冻结程序实际鼠标悬停、移出收起、键盘聚焦提示均通过，截图为 `artifacts/installer-beta2-final/installed-smoke/hover-help.png`。保存配置自动载入音色、当前未保存表单校验、明显获取按钮与豆包 2.0 筛选通过。
- `artifacts/release-beta2-download/verification.json` 为 ok=true，三项资产完整回读字节数及 SHA-256 一致。已公开非 Latest 预发布；产物、构建提交和哈希见 [2.0.0-beta.2](../releases/v2.0.0-beta.2.md)。

Windows 10、真实麦克风和真实云合成继续待验证；保留开发分支，不合入 main。


## 2026-10-13 / 2.0.0-beta.3 本地验收

- Windows 11 x64 / 10.0.26200，168 项离线回归通过，1 条原有上游弃用提示。新增检查包括即时切换、实际音频迁移与路径重定位、目录占用、活动任务拒绝、偏好写入失败和新服务启动失败的回滚，以及模型凭据预检和独立配置。
- `artifacts/beta3-source-final-3/result.json`：源码原生流程 18 项通过。目录按钮调用已验证，系统选目录返回由隔离路径替代，空目录模式选择在真实原生弹窗点击；切换期间窗口保留，新后端立即读取所选目录，偏好自动保存，无保存目录按钮。
- 实际迁移 17 条样本及导出包后旧数据库/音频目录已移除，新目录样本数一致。失败原因展示使用隔离任务显示夹具；缺失 2.0 API Key 的实际预检请求在创建任务前拒绝，不发起云调用。切换供应商清除隐藏选择。
- 主窗口、合成、设置和任务截图已核对；悬停验收要求 popup 出现时实际鼠标仍命中控件，遇到外部鼠标移动时只做有界重新定位，不注入 ToolTip 事件。最终真实悬停、移出、键盘提示通过。
- `artifacts/beta3-cross-volume/result.json` 为 ok=true，实际从 Z: 迁移到 C:，旧数据库/音频移除，从新位置读取音频通过；仅使用自建数据集，结束后关闭服务再清理本轮临时目标。
- `artifacts/beta3-frozen-150-final/result.json`：修复任务详情高度后的冻结流程 18 项通过，150% 截图中文字完整。其源提交为 c67ce0a，之后补齐样本来源筛选并重新构建；不能把此证据冒充最终源提交 a8da6b4 的完整鼠标验收。
- 最终 `artifacts/installer-beta3-locked-subset/result.json`：installer_checks_passed=true，8 项安装/普通启动/冷启动检查通过；同一运行文件和安装源、独立 AppId/mutex，逐文件与生产便携 ZIP 匹配，生产安装器/副本/app PE 版本一致。用户 beta.2 安装保留原状态。
- 此前鼠标命中被 Windows LockApp 锁屏窗口遮挡，已通过前台窗口与 WindowFromPoint 的进程身份查证，不靠 Qt 的 isActiveWindow 推断实际前台。子集证据保留 ok=false / visual_acceptance_pending=true，不作为完整验收证据。
- 用户解锁后，最终源 a8da6b4 的 `artifacts/beta3-unlocked-150/result.json` 为 ok=true，18 项冻结程序原生流程通过；150% 缩放截图中的任务结果两行完整，真实悬停、移出、键盘提示通过。即时目录切换、17 条样本实际迁移且移除旧数据、独立服务配置、缺失凭据预检与完整 Mock 音频/导出流程通过。
- `artifacts/installer-beta3-unlocked/result.json` 为 ok=true，8 项隔离安装检查与安装后的 18 项原生流程通过，源提交 `a8da6b4d083c3a2dc8cc1097e31f2eac4fa14309`，manifest SHA-256 `2c425470839cbdff2df578372c41b98d18e14fcbb6b48f31a53d77098259a00f`。生产运行文件逐项匹配；安装器保护、修复、两个边界的降级拒绝、普通/冷启动和卸载保留数据通过。升级仍仅为模拟夹具，测试身份登记已卸载，无测试 app 进程残留，用户 beta.2 安装保持原路径与版本。
- 补测后未修改运行代码或重建安装包，三项资产字节数及 SHA-256 与 manifest 再次核对一致。
- 最终产物、源提交和完整哈希见 beta.3 版本记录；未执行远程推送、Tag 或 Release。

当前不做旧配置/旧版本兼容；真实云、真实麦克风和 Win10 仍待实测。只交付本地安装包，未打 Tag、未创建或上传 Release，不合入 main。

## 2026-10-13 / 2.0.0-beta.4

Windows 11 x64 / 10.0.26200，175 项离线回归通过，1 条原有上游弃用提示。覆盖两个豆包模型的 V3 HTTP 请求、各自 API Key/资源绑定、不得借用另一模型密钥、禁止计费 POST 重试、错误分段丢弃、完成标志/用量与 PCM 单文件 WAV，以及北京/新加坡、共享/工作空间地址的合成和查询一致性。

`artifacts/beta4-source-150/result.json` 为 ok=true，19 项源码原生流程通过，新增地域下拉框未保存格式检查、保存后显示新加坡且密钥遮蔽。源码界面不再存在任何供应商 URL、Access Token 或 Cluster 输入框。原生验证未调用真实云账号，阿里云格式校验后通过内部设置 API 持久化夹具，避免保存按钮自动查询云端音色。

`artifacts/beta4-frozen-150/result.json` 为 ok=true，最终冻结程序 19 项原生流程通过，150% 缩放截图已核对。`artifacts/installer-beta4-final/result.json` 为 ok=true，8 项隔离安装检查和安装后的 19 项原生流程通过，源提交 `6ec3373e032641c5620affdef6d5989fd8bc9a5d` 与生产清单一致，manifest SHA-256 为 `009f483d301cb8c089e8c0049dc9ac4294bb16d98183d033ca7e4205f6e6ab33`。

隔离安装副本仅更换 AppId/mutex，运行文件与生产便携 ZIP 逐文件一致，生产安装器、副本与 app PE 版本均为 2.0.0.4。安装运行保护、模拟升级修复、两个降级边界、正常/保存目录冷启动和卸载保留数据通过；测试登记已卸载，无测试 app 进程残留。此升级仅是模拟夹具，未替换用户现有生产安装；验收结束时只读核对生产登记为 beta.3、使用现有安装路径。新产物及保留的 beta.3 资产字节数和哈希均与各自清单匹配，见 [beta.4 记录](../releases/v2.0.0-beta.4.md)。Win10、真实云账号权限及合成、真实麦克风仍待验证，不提前合入 main。收到用户明确发布通知后，`artifacts/release-beta4-download/verification.json` 为 ok=true，安装包、便携包与 SHA256SUMS.txt 全部从 GitHub 完整回读，字节数和 SHA-256 匹配；随后才公开为非 Latest 预发布，Tag 保持构建提交，未重新构建用户验收包。
