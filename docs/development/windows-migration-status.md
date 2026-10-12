# Windows 迁移状态

记录日期：2026-10-12。

## 初始化基线

- 仓库已克隆为工作区下的 `tts_2_datasets/`，远程为 `wenchenyang874-a11y/tts_2_datasets`。
- 原主分支为 `main`，用户已确认保留该名称。
- Web 备份分支为 `backup/web-2026-10-12`，基线为 `fcbcbb22f84797dfe9b17b81da0c9eea444fe38d`，已推送远程。
- Windows 开发分支为 `feature/windows-native`，从相同 Web 基线开始；初始化阶段仅增加项目规则和文档，当前阶段已开发原生版本。
- 已确认原生 UI、Windows 10/11、简约现代美观界面及带圈问号悬停说明要求。
- 仓外工作区 `AGENTS.md` 与仓内 `AGENTS.md` 都作为各自作用域的规则入口；仓内文件和文档可独立读取，不依赖仓外资料。

当前不合入 main、不提高版本号、不创建 Tag、不发布 Release。分支与合入规则见 [分支工作流](../releases/windows-branch-workflow.md)。验证范围见 [测试文档](../testing/windows-validation.md)。

## 已实现

- 已确认 Qt/PySide6 6.8.3、x64 Windows 10 1809 及以上/Windows 11 和包含依赖的便携包。
- `desktop_main.py` 为默认桌面入口，原生七页面覆盖合成、样本、录音导入、噪声增强、任务、导出和设置；高级参数默认收起，解释使用带圈问号与提示。
- `desktop/runtime.py` 显式拥有业务子进程，HTTPX ASGITransport 在内存复用接口，不监听网页端口。UI 后台队列执行请求，主线程处理结果。
- 数据目录独立、每数据目录互斥锁、DPAPI 用户绑定凭据、旧 Web 数据复制与路径重定位、退出时进程树清理已实现。
- 保留音频与导出契约；修复 Windows fchmod 不可用、SQLite 导入句柄未关闭，以及旧/新审核状态筛选兼容问题。
- 便携构建入口为 `scripts/build_windows.ps1`，包含 Python、Qt、FFmpeg，附实际依赖与许可清单。

## 当前验证与后续待办

业务回归与原生离线流程已在 Windows 11 x64 验证，实际数字、产物入口和边界见测试文档。录音采集与设备释放代码已实现，但本轮未录制用户真实麦克风；真实云调用亦未执行。

用户已说明暂无 Windows 10 测试环境，要求先完成 Windows 11 验证并保留开发分支。后续安排 Windows 10 实测、真实麦克风与听感确认；真实云调用仅在取得授权后以小规模执行。全部必要验收满足后再合入 main。

运行真实云调用前需取得用户授权。不要因开发机工具已安装就假定目标机已有 Python、ffmpeg 或桌面运行时。
