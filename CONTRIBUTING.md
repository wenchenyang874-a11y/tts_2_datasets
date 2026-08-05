# Contributing

感谢参与改进。提交变更前请遵守以下约定。

## 开发环境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

需要系统安装 `ffmpeg`，前端 JavaScript 语法检查需要 Node.js。

## 验证

```bash
pytest -q
python -m compileall -q .
node --check static/app.js
bash scripts/check_public_release.sh
docker build -t tts-2-datasets:check .
```

自动化测试必须使用 Mock provider，不要让 CI 调用真实云 API 或产生费用。

## 变更要求

- 保持默认音频契约：16 kHz、mono、signed 16-bit little-endian PCM WAV。
- 新增厂商能力时，明确凭据用途、地域、模型、参数和错误脱敏边界。
- 修改接口、数据格式、导出结构或部署方式时，同步更新文档和测试。
- 对文件移动、软删除和恢复路径增加失败与路径越界测试。
- 不在前端、日志、测试夹具或错误响应中暴露真实凭据。
- 不提交生成数据、数据库、个人路径和仅适用于某台服务器的配置。

## Pull Request

PR 描述应说明目的、行为变化、验证命令和仍需真实云账号验证的部分。涉及 UI 时可以附脱敏截图，但不得包含账号、Token、内网 IP、绝对路径或私有音频信息。
