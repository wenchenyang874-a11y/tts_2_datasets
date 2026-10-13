# Windows 安装与版本发布

面向维护者。用户于 2026-10-13 调整发布规则：Agent 可自主本地提交、测试和构建安装包，先提供本地产物供用户验证；用户验证通过并明确通知发布后，才打 Tag 并上传 GitHub Release。此前的自动发布授权已撤回，Agent 自测通过不等于用户验收。普通开发提交不必单独发版。

## 安装契约

- Inno Setup 6.7.3，x64 Windows 10 1809 / build 17763 及以上与 Windows 11；拒绝 x86、ARM64 和较老系统。
- 当前用户安装，无需提权；默认 `%LOCALAPPDATA%/Programs/TTS Dataset Studio`，提供开始菜单入口与可选桌面快捷方式。
- 数据默认 `%LOCALAPPDATA%/TTS Dataset Studio/data`，与程序目录分离。安装、升级与卸载不删数据/凭据，不添加 wildcard UninstallDelete。
- AppId 固定为 `{C8EF7556-B9EA-44AF-96C4-5982E4212033}`；升级使用同一卸载登记与原路径。不能随版本更换 AppId。
- 应用持有 `Local\TTS_Dataset_Studio_Installer_Guard` Win32 mutex，安装与卸载检测其存在；关闭 UI 和业务进程后才允许操作。禁止强制终止写数据的进程。
- 检测到较高版本时拒绝降级；需手动卸载后安装旧程序，数据是否向后兼容另行核对。升级覆盖已登记文件，不任意清理未知文件。
- 当前产物未代码签名，不宣称已通过 SmartScreen 信任或每种安全软件检查。

## 唯一版本来源

只修改 `app_version.py` 的 `VERSION`：`major.minor.patch` 或 `major.minor.patch-beta.N`。应用/API、Windows PE、安装器、文件名和 `v<版本>` 标签从同一来源生成。前三段上限 65535，beta 序号 1–65534；稳定版 Windows 第四段为 65535，使其高于同版本 beta。

未发布期间继续沿用当前版本号，只有发布后开始下一版才递增；本地修改或构建失败不是递增理由。重建前按源提交将旧的未发布产物目录归档到 `dist/releases/archive/<版本>/<源提交前缀>/`，完整保留资产、清单及旧验证证据，确认身份与目录边界后才移动。当前待验收包继续使用 `dist/releases/<版本>/`。已发布资产及 Tag 不按此流程覆盖或移动。

同一版本号可能有多个未发布构建，用户验收／发布授权必须对应源提交、安装包 SHA-256 和验证清单。上一构建验收不自动覆盖新运行代码；最终发布使用最新获用户验收的具体文件，不以文件名相同判断资产相同。安装验证额外检查同版本重新安装实际替换运行文件并保留数据。

首个 Windows 原生版本为 `2.0.0-beta.1`，与 Web `v1.2.3` 区分。用户于 2026-10-13 已合入 main，并要求后续直接在 main 开发；Win10 尚未实测时获准发布的版本仍仅为非 Latest 预发布。稳定版必须在 main，满足 Windows 10 和 Windows 11 验收；合入不代表两套系统已验收，直接开发也不构成自动发布授权。

## 本地构建与验证

仓库根目录，PowerShell；先安装 `requirements-build.txt` 和 Inno Setup 6.7.3。编译器路径通过 `-Iscc` 或 `INNO_SETUP_ISCC` 指定；脚本不自动安装工具、不修改个人配置。

```powershell
.venv/Scripts/python.exe -m pytest -q
# 更新版本、说明和规则后，核对 Git 身份并提交。构建要求干净工作区。
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_release.ps1 -Iscc '<ISCC.exe 路径>'
.venv/Scripts/python.exe scripts/verify_installer.py dist/releases/2.0.0-beta.1 artifacts/installer-beta1-final
```

每版输出 `dist/releases/<版本>/`：安装器 `*-setup.exe`、便携 `*-portable.zip`、`SHA256SUMS.txt`、`release-manifest.json`。清单记录构建提交、版本、字节数和 SHA-256。版本输出目录已存在则停止，避免覆盖已验证产物。

`verify_installer.py` 使用实际生产安装器，以新建中文/空格路径、隔离数据和 `/NOICONS` 测试。真实 AppId 已安装时停止，要求干净测试账号，绝不覆盖用户安装。创建的 HKCU 登记必须属于本轮安装目录才允许更新/卸载。检查完整安装内容与便携包逐文件一致、运行 mutex 阻止升级/卸载、模拟较旧登记且损坏程序的覆盖修复、拒绝降级、安装后的原生 Mock 流程与普通加锁启动/退出、卸载登记/程序清理和用户数据保留。升级夹具只验证安装器路径，不声称真实历史桌面版数据迁移已验证。

## Tag 与 Release

仅在用户验证本版本安装包通过并明确通知发布后执行。未获通知时，不创建本地/远程 Tag，不创建 Release 草稿，不上传资产，也不运行 publish_release.py。通知对应具体版本、构建提交和安装包；验收后若改变运行代码或重新构建，应重新交付用户验证。

仓库根目录，已登录 `gh` 的 PowerShell：

```powershell
.venv/Scripts/python.exe scripts/publish_release.py dist/releases/2.0.0-beta.1 --evidence artifacts/installer-beta1-final/result.json --notes docs/releases/v2.0.0-beta.1.md --download-dir artifacts/release-beta1-download
```

发布脚本验证干净工作区、origin、分支、构建提交、版本与安装验证清单一致后，创建 annotated Tag 并推送分支和 Tag。随后建立 Release 草稿，上传安装包、便携包与校验文件；通过 scripts/download_release_assets.py 从 GitHub 分段并行下载，核对各段范围与长度、完整文件字节数和 SHA-256，再公开 Release。失败保留草稿和证据，不伪造成功、不移动 Tag、不覆盖已有发布资产；网络失败按项目代理规则处理。公开版本修复另发新版本。

稳定版额外传入 `--windows10-evidence <Win10 result.json>`；两套系统的证据都必须对应相同提交与产物清单，脚本从实际 Windows build 分类系统。

新增版本必须同步 CHANGELOG、版本说明和验证记录。发布后记录实际链接/提交与校验结果；记录文档可后续提交，不改变已发布 Tag 指向的构建提交。维护脚本是当前的正式发布入口，不声称任意 push 都自动通过验收并发版。

## 版本结尾的交付回复

每个版本的最终交付回复必须列出本地已打好的安装包：版本、完整文件名和可点击的绝对路径。已获通知并实际发布时同时提供 GitHub Release 链接；待用户验收时明确写明未打 Tag、未发布。Release 链接不能替代本地安装包链接。回复前检查实际文件存在，并核对其字节数和 SHA-256 与该版本 `release-manifest.json` 一致；如有多个安装包，逐一列出。

仓内文档仍使用相对产物路径，例如 `dist/releases/2.0.0-beta.1/TTS-Dataset-Studio-2.0.0-beta.1-windows-x64-setup.exe`；本机绝对路径只在当前会话的交付回复中按实际工作区生成，不能写死到公开仓库。

## 工具与第三方来源

Inno Setup [官方 6.7.3](https://github.com/jrsoftware/issrc/releases/tag/is-6_7_3)，安装器 SHA-256 `9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732`。简体中文译文取自同一标签 `Files/Languages/Unofficial/ChineseSimplified.isl`，保留上游署名，仅规范化尾随空格，随项目存于 `installer/`；属于社区译文。安装器 API 根据[官方文档](https://jrsoftware.org/ishelp/)实现。

包内保留项目与实际依赖许可、版本清单、FFmpeg `-L/-version/-buildconf` 和对应上游源码入口。Qt DLL 可替换，FFmpeg 为独立可执行文件；项目 Apache-2.0 许可不替代第三方许可。改变依赖或分发方式时重新核对实际内容。

## 发布排障

GitHub 按标签查询 Release 的接口在草稿阶段返回 404，即使当前用户是创建者；草稿 URL 中的 untagged-* 也不是正式 Tag。publish_release.py 改为分页列出已认证可访问的 Release，按 tag_name 定位并使用数字 ID；重复草稿时停止检查。首版运行中发现此问题，已保留原 Tag 并通过草稿 ID 继续后续上传与回读校验，相关工具修复随开发分支后续提交。

单连接下载过慢时，回读使用最多 8 个 HTTPS Range 请求；原慢下载停止，未完成文件保留在本轮忽略目录。仅在完整回读哈希匹配后公开草稿。下载认证只在内存使用，不将 Authorization 传给重定向后的资产主机，也不记录签名 URL。失败保留草稿，不自动重试或降低 TLS 校验。

## 已安装旧版本时的验证隔离

发现用户正在使用已安装版本时，不覆盖其安装、不关闭其进程、不清理其卸载登记。以同一 installer/windows.iss 和同一运行目录编译仅 AppId/AppMutexName 不同的测试副本，再给 verify_installer.py 传入 --isolated-setup、--app-id、--mutex-name。副本与生产程序运行文件逐一对齐，生产安装器和副本的 PE 版本同时核对；报告明确写入隔离身份与副本哈希，不称为真实用户安装原位升级。正式产物保持固定 AppId；用户自行关闭旧应用后正常升级。

回读采用最多 8 个并行连接，每段至多约 4 MiB，检查完整字节数和 SHA-256 后发布。
