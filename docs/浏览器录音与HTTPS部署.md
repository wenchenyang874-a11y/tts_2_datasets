# 浏览器录音与 HTTPS 部署

## 为什么局域网 HTTP 不能直接录音

网页录音依赖浏览器的 `navigator.mediaDevices.getUserMedia()`。除 `localhost` 外，主流浏览器只在受信任的 HTTPS 安全上下文中开放麦克风权限。通过 `http://局域网IP:端口/` 访问时，页面仍可生成、试听和管理已有样本，但浏览器会拒绝开始录音。

页面会主动检查安全上下文，并给出 HTTPS、localhost 或 SSH 端口转发提示，不会把浏览器权限错误误报成服务器故障。

## 推荐方式：在现有 HTTPS 反向代理后运行

生产或多人使用时，推荐由 Caddy、Nginx、Traefik 等统一终止 TLS，再代理到应用的 HTTP 端口：

```text
browser --HTTPS--> reverse proxy --HTTP--> tts-app:8080
```

证书必须满足以下条件：

- 证书的 SAN 包含用户实际访问的域名或 IP；
- 客户端信任证书签发机构；
- 私钥只保存在服务器，权限不高于 `0600`；
- 证书、私钥和反向代理私有配置不得提交 Git。

## 直接让 Uvicorn 提供 HTTPS

仓库提供 `docker-compose.https.yml.example`。先复制为被 Git 忽略的本地文件：

```bash
cp docker-compose.https.yml.example docker-compose.https.yml
```

准备受信任证书目录：

```text
/absolute/path/to/certs/
├── server.crt
└── server.key
```

在未跟踪的 `.env` 中设置：

```dotenv
TTS_TLS_CERT_DIR=/absolute/path/to/certs
```

然后同时加载两个 Compose 文件：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.https.yml \
  up -d --build
```

此方式会把原应用端口改为 HTTPS。切换前应先用候选端口和临时数据目录验证，不能在存在运行中生成任务时直接重建容器。

## 局域网没有域名时：使用本地 CA

公网证书机构通常不会为 `192.168.x.x` 等私有地址签发证书。局域网部署可以创建一套仅供本项目使用的本地 CA，再用它签发包含服务器主机名和局域网 IP 的服务器证书：

```text
certs/local/
├── ca.crt       # 分发到访问设备并加入受信任根证书
├── ca.key       # CA 私钥，只能留在服务器
├── server.crt   # 服务器证书
└── server.key   # 服务器私钥，只能留在服务器
```

安全要求：

- `ca.key` 和 `server.key` 使用 `0600` 权限，整个 `certs/` 目录不得提交 Git；
- 服务器证书的 SAN 必须包含浏览器地址栏实际使用的主机名或 IP；
- 只向受控设备安装 `ca.crt`，不要安装或复制 `ca.key`；
- 本地 CA 只用于本项目，泄露后立即撤销信任并重新签发整套证书；
- 证书到期前重新签发服务器证书，并保留原 CA 可避免客户端重复安装根证书。

Windows 上可在当前用户范围信任根证书，无需把私钥复制到 Windows。以 PowerShell 执行：

```powershell
Import-Certificate -FilePath .\ca.crt -CertStoreLocation Cert:\CurrentUser\Root
```

完成后彻底退出并重新打开 Chrome 或 Edge，再通过证书 SAN 中的 HTTPS 地址访问。Firefox 若未使用系统证书库，需要在“设置 -> 隐私与安全 -> 证书 -> 查看证书 -> 证书颁发机构”中单独导入 `ca.crt` 并允许其标识网站。

可以用以下命令验证服务端证书链；验证结果应为 `0 (ok)`：

```bash
openssl s_client \
  -connect SERVER_IP:8080 \
  -servername SERVER_HOSTNAME \
  -CAfile certs/local/ca.crt \
  -verify_return_error </dev/null
```

## 采集和数据约定

浏览器录到的 WebM、Ogg 或其他受支持容器只作为上传传输格式。服务器最终统一转为：

- 16 kHz；
- 单声道；
- signed 16-bit little-endian PCM；
- WAV 容器。

浏览器请求的采样率只是约束提示，不能代替服务端归一化。上传记录还应保存浏览器原始 MIME、录音时长、说话人和正负样本类型，方便后续审计与复现。
