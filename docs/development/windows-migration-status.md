# Windows 迁移状态

记录日期：2026-10-12。

## 初始化状态

- 仓库已克隆为工作区下的 `tts_2_datasets/`，远程为 `wenchenyang874-a11y/tts_2_datasets`。
- 原主分支为 `main`，用户已确认保留该名称。
- Web 备份分支为 `backup/web-2026-10-12`，基线为 `fcbcbb22f84797dfe9b17b81da0c9eea444fe38d`，已推送远程。
- Windows 开发分支为 `feature/windows-native`，本轮仅新增项目规则、需求、迁移与验收文档。源码仍为 Web 版本，尚无 Windows UI、exe 或安装包。
- 已确认原生 UI、Windows 10/11、简约现代美观界面及带圈问号悬停说明要求。
- 仓外工作区 `AGENTS.md` 与仓内 `AGENTS.md` 都作为各自作用域的规则入口；仓内文件和文档可独立读取，不依赖仓外资料。

本轮不合入 main、不提高版本号、不创建 Tag、不发布 Release。分支与合入规则见 [分支工作流](../releases/windows-branch-workflow.md)。验证范围见 [测试文档](../testing/windows-validation.md)。

## 后续开发顺序

1. 查证最低 Windows 10 构建号、CPU 架构与候选桌面框架的兼容性，核对影响交付范围的选择。
2. 建立现有功能、数据与测试对照，设计原生界面与悬停帮助；记录业务复用、进程、数据目录、凭据、ffmpeg 和安装包方案。
3. 分离 UI 与应用用例，按功能逐步迁移并回归 Mock 生成、录音导入、审核、增强和导出，保持数据契约。
4. 实际打包并分别运行 Windows 10/11 验收；满足全部合入门槛后正常合入 main。

运行真实云调用前需取得用户授权。不要因开发机工具已安装就假定目标机已有 Python、ffmpeg 或桌面运行时。
