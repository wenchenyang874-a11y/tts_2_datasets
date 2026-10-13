# Windows 桌面版使用

目标为 x64 Windows 10 1809 及以上、Windows 11；当前实际验证系统与结果见 [验收记录](../testing/windows-validation.md)。

## 安装与启动

从 GitHub Release 下载 `*-setup.exe` 安装到当前用户目录，无需管理员权限；开始菜单中启动。也可使用便携包。安装程序要求 x64 Windows 10 1809 或更新系统；ARM64 尚不支持。

便携包中的整个 `TTS Dataset Studio` 文件夹需保留完整，双击 `TTS Dataset Studio.exe`。无需启动 Docker、浏览器、Python 或手动安装 FFmpeg。该包不包含用户云凭据、音频和数据库。

默认数据目录为当前用户 `%LOCALAPPDATA%/TTS Dataset Studio/data`。在“设置”选择或输入“下次启动目录”，点击“保存目录”，退出后重新启动应用即使用新目录；旧目录数据保留原位，不自动搬迁。偏好保存在当前用户 `%LOCALAPPDATA%/TTS Dataset Studio/desktop_preferences.json`，命令行 --data-dir 优先于保存值。同一数据目录只允许一个应用实例；安装目录可与数据目录分离。指定其他数据目录：

```powershell
& '.\TTS Dataset Studio.exe' --data-dir 'D:\datasets\my-kws'
```

## 主要操作

1. 在“设置”填写供应商配置并保存，随后自动获取音色列表。“验证格式”直接检查当前填写内容，不必先保存；它不保存配置、不调用云端，也不代表账号权限或真实合成成功。合成创作页紫色“获取音色列表”可手动获取。小米 MiMo 的目录为官方预置音色快照。
2. 在“合成创作”设置唤醒词、文本、音色和数量。云合成需确认费用；“离线验证 · Mock”生成测试音调，不是真实人声。
3. 在“样本库”试听、分析、审核或跨页勾选样本。双击审核列可循环切换状态；删除后可在本次会话撤销最近删除。
4. “真人录音”支持设备选择、录音、停止、试听、重录和导入已有音频。每次录音最多 30 秒，最短 0.2 秒；连续导入保留说话人和标签等设置。
5. 在“噪声增强”导入并选择噪声，使用样本库已选原音创建增强；也可把噪声素材加入背景样本。新增增强样本需重新审核。
6. 在“训练集导出”明确选择“样本库所选”或“当前唤醒词全部”，创建导出后从列表另存为 ZIP。关闭音频复制时，训练程序必须能访问清单中的本机数据路径。

必要参数说明位于相关控件旁的带圈问号，悬停或键盘聚焦查看。操作错误与任务状态直接显示。云费用为预估值，实际金额以供应商账单为准；失败或超时后不要盲目重试。

## 录音与数据恢复

Windows 系统设置中需允许桌面应用使用麦克风。设备缺失、权限关闭或设备被独占时应用会报告不可用。录音数据按设备支持的格式采集，导入后统一为 16 kHz mono PCM16 WAV。

设置中的“导入 Web 备份到新目录”入口已移除。备份和恢复应在退出应用、确认后台进程结束后整体处理数据库、音频和配置；选择新的空目录不会自动带入原数据。

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

小米 MiMo 填写开放平台 API Key，Endpoint 可留空或填 https://api.xiaomimimo.com/v1。当前支持 mimo-v2.5-tts 与 9 款官方预置音色，可用高级参数中的风格指令控制表达；不发送不支持的数值语速、音高或音量字段。请求使用 24 kHz WAV/MP3，入库仍归一化为 16 kHz mono PCM16 WAV。未配置报价不显示为零费用，真实合成仍需确认费用责任。

豆包 1.0 使用原 App ID / Access Token；豆包 2.0 使用控制台 API Key，填在“2.0 API Key”，Endpoint 留空使用官方 V3 默认地址。音色列表可筛选模型，音色行也标明 1.0/2.0；音色、模型和已开通资源须匹配。AK/SK 用于远程音色目录，与合成凭据分开；未配置 AK/SK 时只加载已维护的官方预置目录，不宣称获取账号授权。2.0 当前支持 WAV/MP3 及既有 8/16/24 kHz 选项，不显示不兼容的 Ogg Opus 选项。
