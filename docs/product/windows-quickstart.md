# Windows 桌面版使用

目标为 x64 Windows 10 1809 及以上、Windows 11；当前实际验证系统与结果见 [验收记录](../testing/windows-validation.md)。

## 安装与启动

从 GitHub Release 下载 `*-setup.exe` 安装到当前用户目录，无需管理员权限；开始菜单中启动。也可使用便携包。安装程序要求 x64 Windows 10 1809 或更新系统；ARM64 尚不支持。

便携包中的整个 `TTS Dataset Studio` 文件夹需保留完整，双击 `TTS Dataset Studio.exe`。无需启动 Docker、浏览器、Python 或手动安装 FFmpeg。该包不包含用户云凭据、音频和数据库。

默认数据目录为当前用户 `%LOCALAPPDATA%/TTS Dataset Studio/data`。在“设置”的“数据目录”点击“更换目录”，选择后立即切换并自动保存，不需要重启或再点保存。

- 目标为空时选择“迁移现有数据”或“使用空目录”。迁移成功后旧位置不保留重复的应用数据，原目录中的其他文件不动；使用空目录则保留旧数据集原位。
- 目标为已有有效数据集时直接打开它；其他非空目录不覆盖。录音中、任务执行/排队中或目录被另一窗口占用时不能切换，失败恢复原数据集。
- 偏好存于 `%LOCALAPPDATA%/TTS Dataset Studio/desktop_preferences.json`。CLI --data-dir 只决定本次初始目录，不妨碍在设置中即时切换；再次带该参数启动时仍以参数为准。

同一数据目录只允许一个实例，数据目录与安装目录分离。命令行指定启动目录：

```powershell
& '.\TTS Dataset Studio.exe' --data-dir 'D:\datasets\my-kws'
```

## 主要操作

1. 在“设置”填写供应商配置并保存，随后自动获取音色列表。“检查配置”直接检查当前填写内容，不必先保存；它不保存配置、不调用云端，也不代表账号权限或真实合成成功。合成创作页紫色“获取音色列表”可手动获取。小米 MiMo 的目录为官方预置音色快照。
2. 在“合成创作”设置唤醒词、文本、音色和数量。云合成需确认费用；“离线测试”生成测试音调，不是真实人声。
3. 在“样本库”试听、分析、审核或跨页勾选样本。双击审核列可循环切换状态；删除后可在本次会话撤销最近删除。
4. “真人录音”支持设备选择、录音、停止、试听、重录和导入已有音频。每次录音最多 30 秒，最短 0.2 秒；连续导入保留说话人和标签等设置。
5. 在“噪声增强”导入并选择噪声，使用样本库已选原音创建增强；也可把噪声素材加入背景样本。新增增强样本需重新审核。
6. 在“训练集导出”明确选择“样本库所选”或“当前唤醒词全部”，创建导出后从列表另存为 ZIP。关闭音频复制时，训练程序必须能访问清单中的本机数据路径。

必要参数说明位于相关控件旁的带圈问号，悬停或键盘聚焦查看。操作错误与任务状态直接显示。云费用为预估值，实际金额以供应商账单为准；失败或超时后不要盲目重试。

## 录音与数据恢复

Windows 系统设置中需允许桌面应用使用麦克风。设备缺失、权限关闭或设备被独占时应用会报告不可用。录音数据按设备支持的格式采集，导入后统一为 16 kHz mono PCM16 WAV。

设置中的“导入 Web 备份到新目录”入口已移除。备份和恢复应在退出应用、确认后台进程结束后整体处理数据库、音频和配置；切换空目录时由用户决定迁移现有数据或直接使用空数据集。

桌面云凭据用当前 Windows 用户的 DPAPI 保护。跨机器或跨用户恢复时可能无法解密，需使用原用户环境或移走备份中的 `provider_settings.json` 后重新配置，不能把加密文件误当成通用可迁移密钥。数据库和音频需整体备份；仅复制数据库会丢失实际音频。关闭应用并确认后台进程退出后再备份整个数据目录。

关闭应用后运行新版本安装包可原位升级；卸载通过 Windows“已安装的应用”，数据与凭据保留。安装器拒绝运行中升级/卸载及直接降级。便携包升级时替换程序文件夹，保留独立数据目录；删除便携程序目录也不删除独立数据。当前包未代码签名。下载与版本状态见仓库 Releases；预发布版不代表 Windows 10 已完成实测。

## 开发与离线验收

仓库根目录、PowerShell：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.txt
.venv/Scripts/python.exe desktop_main.py
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe desktop_main.py --smoke-test artifacts/source-smoke
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows.ps1
& 'dist/TTS Dataset Studio/TTS Dataset Studio.exe' --smoke-test artifacts/package-smoke
```

自检目录必须不存在，自检仅使用临时数据和 Mock，并保存结果 JSON、页面截图、音频与导出包；不会读取默认用户数据或调用真实云服务。重复运行选择新目录。自检不等于真实麦克风、人声或另一套 Windows 系统验证。

普通锁定入口与事件循环可用 `--data-dir artifacts/launch-data --verify-launch artifacts/launch-evidence` 做限时启动验收；该模式要求显式指定隔离数据目录，证据目录必须不存在，完成后关闭窗口并保存结果。

## 小米与豆包模型

小米 MiMo 只需填写开放平台 API Key，官方地址由程序内置。当前支持 mimo-v2.5-tts 与 9 款官方预置音色，可用高级参数中的风格指令控制表达；不发送不支持的数值语速、音高或音量字段。请求使用 24 kHz WAV/MP3，入库仍归一化为 16 kHz mono PCM16 WAV。未配置报价不显示为零费用，真实合成仍需确认费用责任。

设置与音色选择分别提供“豆包 1.0”和“豆包 2.0”。两者均填写新版语音控制台 API Key，独立保存、清空和估价，不再使用 Access Token 或 Cluster。按用户指定的 [单向流式语音合成 HTTP 文档](https://docs.volcengine.com/docs/DoubaoVoice/unidirectional-streaming-text-to-speech-http?lang=zh)，统一调用 V3，程序自动设置对应资源 ID；1.0 使用 seed-tts-1.0 字符版，2.0 使用 seed-tts-2.0 字符版。地址内置，不提供 URL 输入框。切换服务会清除原音色选择，创建任务前检查所选版本凭据；缺失时直接显示原因，可点击“去配置”。

高级设置使用火山官方名称：`Access Key ID (AK)` 和 `Secret Access Key (SK)`。AK/SK 从控制台 [API 访问密钥](https://www.volcengine.com/docs/6257/64983?lang=zh) 获取，用于签名调用 [大模型音色列表接口 ListSpeakers](https://api.volcengine.com/api-docs/view?action=ListSpeakers&serviceCode=speech_saas_prod&version=2025-05-20)，服务地址内置。未配置 AK/SK 时仅加载维护的官方预置音色列表，不代表账号已开通资源。“复刻应用 ID”仅在查询账号复刻音色时需要，合成不需要。两版本提供 WAV/MP3；WAV 由 PCM 分段生成完整文件。当前开发阶段不迁移旧配置，需要在各独立页填写 API Key。

阿里云选择 API Key 所属地域：华北2（北京）或新加坡，地址由程序自动生成。高级设置可填写该地域的 Workspace ID，自动使用业务空间专属域名，留空使用对应 DashScope 官方域名。不同地域的 API Key 与模型权限不能混用；地域选项不证明账号已开通某模型。参考 [官方地域说明](https://www.alibabacloud.com/help/zh/model-studio/regions/) 和 [HTTP 合成文档](https://www.alibabacloud.com/help/zh/model-studio/non-realtime-tts-user-guide)。

任务中心直接显示失败原因，选中任务可看文本明细，无需先打开 JSON。再次合成需确认可能重复计费；配置检查和预检只检查本地格式与必填项，不能证明真实账号可调用。
