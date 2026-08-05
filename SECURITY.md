# Security Policy

## Supported version

安全修复只针对 `main` 分支的最新版本。项目处于早期阶段，暂不维护多个长期支持分支。

## Reporting a vulnerability

请优先使用 GitHub 仓库的 Private vulnerability reporting 或 Security Advisory 私下报告。不要在公开 Issue 中附带：

- 云厂商 Access Key、Secret、Token、App ID 或 Workspace ID；
- 应用访问令牌；
- `provider_settings.json`、`.env` 或数据库；
- 内网地址、个人身份信息或真人录音；
- 可直接利用的完整攻击步骤。

报告中可以提供受影响版本、无秘密的复现步骤、预期影响和建议修复方向。

## Deployment boundary

本项目默认是单管理员、可信局域网工具，不是面向公网的多租户 SaaS：

- 示例配置默认只绑定 `127.0.0.1`。
- 开启局域网监听前，应设置高强度 `TTS_APP_API_TOKEN`。
- 应用令牌是轻量共享令牌，不等同于用户账号系统或 RBAC。
- 局域网 HTTP 不加密传输；跨网段或多人使用时必须部署 HTTPS 反向代理。
- 高级云 Endpoint 配置只应开放给可信管理员。
- 在云厂商侧设置预算告警、调用配额和最小权限凭据；应用内确认框不能替代厂商侧控制。

## Secrets and runtime data

云凭据保存在运行数据目录的 `provider_settings.json`，查询 API 不回传完整密钥。文件权限控制不能替代主机安全和加密备份。

以下内容禁止提交：

- `.env`
- `var/`、`data/` 或其他运行数据目录
- SQLite 数据库、WAL 和 SHM 文件
- 云凭据、应用令牌和包含它们的日志
- 生成音频、真人录音、噪声和导出数据包

如果凭据进入了 Git 历史，应立即在云厂商控制台撤销或轮换。之后再清理 Git 历史；仅提交一次“删除文件”无法消除历史泄漏。

## Dependency and provider risk

项目依赖 Python 包、`ffmpeg` 和外部云 API。部署者应定期更新镜像与依赖、关注云厂商接口变更，并在升级后运行离线测试。云端生成内容和音色的授权、合规、商业使用及再分发责任由使用者自行确认。

云端调用费用由凭据所属账号承担。部署者还应限制应用访问范围、监控任务和账单，并阅读 [使用、计费与责任说明](DISCLAIMER.md)。
