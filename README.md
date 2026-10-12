# 语音数据集工坊

[![CI](https://github.com/wenchenyang874-a11y/tts_2_datasets/actions/workflows/ci.yml/badge.svg)](https://github.com/wenchenyang874-a11y/tts_2_datasets/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

一个面向中文关键词唤醒（KWS）的局域网 Web 工具：使用火山引擎或阿里云 TTS 批量合成语音，完成试听、审核、噪声增强和训练集导出。

默认唤醒词是 `你好，小助手`，可在网页中修改。仓库不内置或包含任何云厂商凭据、生成音频和运行数据库。

Windows 10/11 原生桌面版正在准备迁移，当前源码仍为 Web 版。需求、UI 规范、开发状态和验收入口见[项目文档索引](docs/README.md)。

> 本项目是独立社区工具，与所接入的云厂商均无官方隶属、授权或背书关系。云服务能力、价格、音色和权限以厂商最新规则及用户账号实际授权为准。

> **费用责任：** 本项目是自托管软件，不提供或代付云服务。使用者自行填写凭据并直接调用云厂商 API；凭据所属账号产生的全部费用由账号持有人和实际使用者承担。请在批量生成前核对厂商价格、设置预算告警并先做最小样本测试。详见[使用、计费与责任说明](DISCLAIMER.md)。

## 功能

- 网页配置火山引擎和阿里云账号，密钥只保存在服务端数据目录。
- 查询公共音色及账号可见音色，支持供应商、性别和年龄筛选。
- 多文本、多音色批量生成，可设置数量、语速、音量及供应商支持的音高参数。
- 一次批量操作对应一个可追踪的总任务，每条文本形成子任务；服务端执行真实并发调度并记录排队、执行和总耗时。
- 云端生成需要显式确认计费责任，后端也会拒绝未确认的云端任务。
- 云端任务可显示管理员配置的预估费用；最终金额始终以云厂商账单为准。
- 浏览器试听，查看波形、频谱、时长、峰值和 RMS。
- HTTPS 安全上下文下支持浏览器麦克风录音、试听、重录和上传。
- 真人录音导入、正负样本与背景样本标注、样本审核、元数据修改和软删除恢复。
- 上传背景噪声，按可复现随机种子和 SNR 范围批量增强。
- 按说话人或 TTS 音色分组划分 Train/Dev/Test，避免同组数据泄漏。
- 导出通用 JSONL、分类目录或 WeKWS 单关键词数据包；WeKWS 包含 `<FILLER>`/关键词字典、`words.txt`、`wav.scp` 和 `wav.dur`。
- 样本库支持分页、页码跳转、跨页选择以及按唤醒词、目标类型和来源任务筛选。
- Docker 部署；回环访问可不设令牌，开启局域网监听前必须设置应用访问令牌。

## 快速启动

需要 Docker Engine 和 Docker Compose。首次启动：

```bash
git clone https://github.com/wenchenyang874-a11y/tts_2_datasets.git
cd tts_2_datasets
cp .env.example .env
chmod 600 .env
mkdir -p var
id -u
id -g
```

把 `id -u` 和 `id -g` 的结果填入 `.env` 的 `TTS_APP_UID`、`TTS_APP_GID`。默认只监听本机回环地址，可以直接启动：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8080/healthz
```

上面使用 Docker Compose v2。若 `docker compose version` 不可用、但已安装 Compose v1，请把所有 `docker compose` 命令替换为 `docker-compose`；本项目的配置兼容这两种命令。

默认仅能在服务器本机访问：

```text
http://127.0.0.1:8080/
```

需要局域网访问时，先生成令牌：

```bash
openssl rand -hex 32
```

将输出填入 `.env` 的 `TTS_APP_API_TOKEN`，再把 `TTS_APP_BIND_ADDRESS` 改为 `0.0.0.0` 并重新启动。然后访问：

```text
http://<服务器局域网IP>:8080/
```

点击页面右上角“访问令牌”并填入同一令牌。应用令牌不提供传输加密；跨网段或多人使用时必须配置 HTTPS 和正式认证。云厂商账号信息通过“供应商设置”保存，不要写入 `.env`。

浏览器通过局域网地址录音时必须使用客户端信任的 HTTPS 证书，部署方法见[浏览器录音与 HTTPS 部署](docs/浏览器录音与HTTPS部署.md)。

真人录音上传校验后，导入窗口和填写参数会保留，可继续录音或选择下一批文件。样本“类型”只分为原音与增强样本；真人录音属于来源。噪声增强按“总任务 → 每条源音频子任务”展示，所有增强输出都重新进入未审核状态。

真人样本导入区提供明确的已有 ID 下拉框和新 ID 输入框；选择已有项会自动填入输入框，新 ID 上传成功后会自动进入候选列表。前端应用资源使用版本化 URL 并禁用缓存，防止升级后浏览器继续执行旧版导入逻辑。

样本库审核状态可直接点击切换，顺序为“未审核 → 已通过 → 已拒绝 → 未审核”；切换后立即保存到服务端，并遵循当前审核筛选条件刷新列表。

## 云厂商配置

### 火山引擎

当前适配将凭据分为两组：

- `App ID + Access Token + Cluster`：用于 V1 短文本语音合成。
- `Access Key ID + Access Key Secret`：用于查询公共音色；同时配置 App ID 后，也会查询账号可见的复刻音色。

能够查询某个音色不等于当前语音应用已经获得该音色的合成权限，最终以云厂商返回结果为准。

### 阿里云

当前适配使用百炼语音合成接口：

- `API Key`：合成必填。
- `Workspace ID`：查询和使用账号自定义音色时建议填写。
- 当前非实时合成路径按北京地域适配。

云 API 可能按调用量产生费用，免费额度也可能过期或不适用于当前模型。费用由凭据所属账号承担；请先在厂商控制台设置预算、告警与配额，再用单音色、单条文本验证，最后逐步扩大批量任务。价格入口见[使用、计费与责任说明](DISCLAIMER.md)。

## 数据目录

默认将运行数据保存到仓库下的 `var/`，并挂载为容器内的 `/data`：

```text
var/
├── tts_dataset.sqlite3
├── provider_settings.json
├── project_settings.json
├── samples/
├── noises/
├── exports/
├── trash/
└── tmp/
```

`var/` 和 `.env` 已被 Git 忽略。备份时应同时备份 SQLite 和整个数据目录，避免数据库记录与音频文件不一致。

如果关闭导出页的“复制音频文件”，清单会引用服务端源文件。Docker Compose 默认写入容器路径 `/data`；只有训练程序也能读取同一挂载时该路径才有效。其他部署方式可显式设置 `TTS_MANIFEST_DATA_ROOT` 为训练程序可见的数据根目录；直接 Python 运行在未设置时会使用解析后的 `TTS_APP_DATA_DIR`。

## 导出格式

| 格式 | 主要内容 |
|---|---|
| `generic` / `jsonl` | `audio/{split}` 与三份 JSONL 清单 |
| `folders` | `folders/{split}/{keyword_id}` 分类目录及 JSONL |
| `wekws` | 通用结构外加 `<FILLER>`/关键词字典、`words.txt`，以及每个 split 的 `text`、`wav.scp` 和 `wav.dur` |

导出记录支持下载和软删除。删除后的 ZIP 会进入 `var/trash/exports/`，网页提供短时撤销；原始样本和任务记录不受影响。

## 本地开发

需要 Python 3.10 或更高版本、Node.js（用于 JavaScript 语法检查）和 `ffmpeg`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
pytest -q
node --check static/app.js
```

直接运行：

```bash
cp .env.example .env
uvicorn app:app --env-file .env --host 127.0.0.1 --port 8080 --workers 1
```

直接运行时如需局域网访问，在确认 `.env` 已设置应用令牌后，将 `--host` 改为 `0.0.0.0`。应用使用 SQLite 和进程内任务池，生产运行保持一个 Uvicorn worker；`TTS_APP_WORKERS` 控制的是后台批处理线程数。

## 安全与隐私

- 绑定 `0.0.0.0` 前必须设置高强度应用令牌；令牌不提供传输加密。跨网段或多人使用时必须配置 HTTPS 反向代理和正式认证。
- 服务端保存的云密钥不会通过设置查询接口回传明文，但 `var/provider_settings.json` 仍属于敏感文件。
- 携带云凭据的高级 Endpoint 默认仅允许当前适配器使用的供应商官方主机；使用自管 HTTPS 代理必须由管理员显式开启。
- 不要提交 `.env`、`var/`、数据库、音频、导出包或包含真人身份信息的清单。
- 如果凭据曾进入 Git 历史，仅删除文件不够；应立即在云厂商控制台轮换凭据，并清理仓库历史。
- TTS 样本适合扩充训练集，不能替代真人录音的最终 FRR、误唤醒和远场评估。

更多说明见 [产品与部署说明](docs/产品与部署说明.md)、[任务、样本与导出设计](docs/任务样本与导出设计.md)、[浏览器录音与 HTTPS 部署](docs/浏览器录音与HTTPS部署.md)、[使用、计费与责任说明](DISCLAIMER.md)、[安全策略](SECURITY.md) 和 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 项目状态

这是面向 KWS 数据制作的工程工具，不是完整的 KWS 训练框架。当前没有完整回收站管理页、多人权限系统或内置证书签发服务。

## License

本项目采用 [Apache License 2.0](LICENSE) 开源许可证。
