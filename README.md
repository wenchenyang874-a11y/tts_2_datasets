# 语音数据集工坊

面向中文关键词唤醒（KWS）的 Windows 原生桌面工具，支持云 TTS 批量合成、真人录音导入、样本试听审核、噪声增强和训练集导出。

目标环境：x64 Windows 10 1809 及以上、Windows 11。桌面版在 `feature/windows-native` 开发；实际系统验证与待验收项见 [验证记录](docs/testing/windows-validation.md)，验收完成前不合入 main。

## 快速启动

从 [GitHub Releases](https://github.com/wenchenyang874-a11y/tts_2_datasets/releases) 下载安装包或便携包；安装包默认安装到当前用户目录，无需管理员权限。便携包中双击 `TTS Dataset Studio.exe`，保留整个程序文件夹。包中包含运行依赖，无需部署 Docker 或启动浏览器。默认数据目录在当前用户 LocalAppData，与程序目录分离。

从源码运行（仓库根目录，PowerShell）：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-desktop.txt
.venv/Scripts/python.exe desktop_main.py
```

开发、构建、指定数据目录及旧 Web 数据导入见 [Windows 桌面版使用](docs/product/windows-quickstart.md)。

## 功能

- 火山引擎、阿里云配置与音色查询，多文本、多音色、真实并发批量合成。
- 麦克风录音、试听、重录和连续导入，音频统一 16 kHz mono PCM16 WAV。
- 波形/频谱与音频指标、跨页选择、审核、元数据编辑和软删除恢复。
- 可复现噪声增强，按说话人或供应商音色分组划分 Train/Dev/Test。
- 通用 JSONL、分类目录和 WeKWS 导出；任务中心显示父子任务、实际状态与预估费用。
- 简约原生界面，必要解释通过相关控件旁的带圈问号悬停提示。

本项目不内置云凭据、音频或运行数据库。云调用需使用者显式确认费用，由凭据所属账号承担，最终金额以厂商账单为准。详见 [使用、计费与责任说明](DISCLAIMER.md)、[安全策略](SECURITY.md) 和 [第三方说明](THIRD_PARTY_NOTICES.md)。Mock 为离线测试音调，不是真实人声；TTS 数据不能替代真人 Dev/Test 或实机 KWS 评估。

## 文档与 Web 备份

[项目文档索引](docs/README.md)提供需求、架构、开发状态、验收和分支规则。原 Web 版本完整保存在 `backup/web-2026-10-12`；现有 Web 文档保留为兼容与迁移参考。

本项目为独立社区工具，与云厂商无官方隶属、授权或背书关系。采用 [Apache License 2.0](LICENSE)。
