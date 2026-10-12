# Windows 验证与合入门槛

## 本轮范围

本轮为克隆、分支和文档初始化，没有修改业务代码，没有实现 Windows app。检查文档路径、索引、占位符、分支基线和 diff；语法检查仅作为现有 Web 基线检查。尚未执行 Windows 10/11 应用验收或安装包验证。

2026-10-12 本机实际检查结果：

- Python 指定模块及 tests 的 compileall 检查通过；Web JavaScript 语法检查通过。
- 本轮 13 份仓内 Markdown 文件的 29 个相对链接及两级 AGENTS.md 入口检查通过，无残留模板占位符或编码异常。
- 暂存内容的 `git diff --cached --check` 和公开提交扫描通过，仅规则与文档变化。
- 本地 main、origin/main 和 Web 备份均指向原始基线提交。
- 没有安装额外依赖或执行 pytest、Web 实际运行、Docker、真实云、Windows 10/11 或安装包验收；本轮验证结论限于分支、文档与语法。

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
