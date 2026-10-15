# beta.8 自定义音色验证

## 已完成

- 254 项离线测试通过；含 24 项新增测试，覆盖八种供应商/创建方式的请求契约、查询、幂等操作 ID、参考检查、失败不重试、异常脱敏、云 ID 保留、地域限制、名称保留、启停、历史样本及目录迁移。
- Windows 11 build 26200：源码 100% 与 150% 原生流程验收；共 38 项主流程检查，新功能单独保留创建表单、可用列表、合成定位和播放按钮全区域证据。最终打包后继续核对冻结版与安装版，不能用源码结论替代。
- 阶段性源码证据：`artifacts/beta8-source-verified-100/`、`artifacts/beta8-source-final-150/`；最后安装验收绑定 `c5f9f15` 构建，见下文。
- 新增管理页的预览按钮在初始截图发现单元格 8px padding 与 host margin 叠加裁切；改为固定列宽、host 零边距并居中，在实际控件 visibleRegion 与底部点击验证完整可用。
- 验收脚本主动查询与管理页自动轮询曾互相覆盖旧快照；已停止该页定时轮询后由验收脚本单独等待完整状态，保留失败证据，不改业务状态假装通过。

## 仅授权的真实云请求

用户本轮明确授权小米 2 次请求，无自动重试：

1. `mimo-v2.5-tts-voicedesign`，温暖清晰的青年女声，简短中文预览。
2. `mimo-v2.5-tts-voiceclone`，使用上一步落盘的参考 WAV，另一段中文文本。

两次均 HTTP 200，创建状态 ready，有可解析的本地预览；原供应商设置文件 SHA-256 前后相同。未构造生产设置 Store、不写真实样本库、不存明文密钥。证据 `artifacts/beta8-mimo-two-authorized/result.json` 与其隔离数据目录，音频不提交公开仓库。此结果证明 API 与本地参考复用链路成功，不代表用户对音色相似度、主观听感或 ASMR 效果已验收。

豆包创建/设计和阿里云创建/设计尚未进行真实账号调用；已按当日官方接口文档实现并完成 Mock 请求契约验证，账号权限、槽位、审核和区域实际可用性待验证。Windows 10、真实麦克风仍未实测。

## 最终冻结与安装验收

- 构建源 `c5f9f157a71b18a6277141842922f71704ba27e3`，安装器与程序 PE 均为 `2.0.0.8`；实际产物和哈希见[版本清单](../releases/v2.0.0-beta.8.md)。
- `artifacts/beta8-pytest-complete.log`：254 项通过；其后声源分组变化的 63 项回归通过，记录 `artifacts/beta8-provenance-regression.log`。
- `artifacts/beta8-portable-launch/evidence/result.json`：便携实际 exe 在隔离数据目录正常启动、锁目录并关闭业务进程。
- `artifacts/beta8-installer-verified/result.json`：Windows 11 build 26200，9 项安装检查通过，包括相同版本修复重装、旧版登记升级、运行标记拦截升级与卸载、降级拒绝、数据保留、正常启动及卸载清理。升级测试为模拟旧注册版本和损坏的受控程序，不声称真实跨版本用户数据迁移实测。
- 同目录 `installed-smoke/result.json`：150% 缩放，38 项完整原生流程通过；`custom-voices-evidence.json` 另含 7 项新功能子检查，覆盖云供应商字段显示、Mock 复刻/设计、播放按钮全部可见与底部点击/停止、预览复制、名称标签编辑、创作页定位、移除恢复且不新发云请求。
- 已查看安装后管理列表、复刻/设计弹窗与加入创作页的截图；播放按钮不裁切、选中行文字完整。测试安装使用独立 AppId/mutex，程序字节与正式便携包逐文件一致；用户原安装未替换。
- 源码 `beta8-source-complete-150` 曾停在首轮导航断言，未记为通过；后续安装后的相同构建完成整套真实鼠标/悬停验证，不删除失败证据。
- 外层 Windows PowerShell 把 PyInstaller 的 INFO stderr 包装成 NativeCommandError，构建命令外层返回 1；日志中 PyInstaller 与 Inno 均实际完成、清单已生成。未将退出码当作成功依据，随后独立核验全部产物字节/哈希、PE、实际 exe 和 9 项安装流程成功。
