# 自定义音色：beta.8 设计与接口依据

本版本目标：声音复刻、文本音色设计、自定义音色管理；验证结论另记测试文档。

## 供应商接口

核对日期 2026-10-15。所有创建请求仅发送一次；超时或结果未知不自动重试。

| 供应商 | 复刻 | 文本设计 | 持久化与限制 |
| --- | --- | --- | --- |
| 豆包 1.0 / 2.0 | V3 `tts/voice_clone`，本地音频 Base64 | V3 `tts/voice_design`，控制台购买的 Speaker ID | V3 `tts/get_voice` 查询状态；合成分别绑定 `seed-icl-1.0`、`seed-icl-2.0`，对应版本 API Key。本版本仅使用已购买槽位，不自动购买、转正或覆盖本地已有音色。 |
| 小米 MiMo | `mimo-v2.5-tts-voiceclone`，参考音频 Data URL | `mimo-v2.5-tts-voicedesign`，user 描述 + assistant 预览文本 | 无持久云音色 ID；参考音频保存在数据目录。设计结果保存为参考音频，后续通过 clone 模型复用，不每次重新设计。 |
| 阿里云 CosyVoice | `voice-enrollment/create_voice`，公网音频 URL | 同接口，`voice_prompt` + `preview_text` | 绑定 CosyVoice 模型、地域和 Workspace；`query_voice` 确认 OK 后合成。设计仅北京地域。不自动上传本地文件到第三方存储。 |

依据：[豆包 V3 复刻](https://docs.volcengine.com/docs/DoubaoVoice/SoundReplicaAPI-V3?lang=zh)、[豆包设计](https://docs.volcengine.com/docs/DoubaoVoice/SoundDesignAPI?lang=zh)、[小米合成、设计与复刻](https://mimo.mi.com/docs/en-US/quick-start/usage-guide/audio/speech-synthesis-v2.5)、[阿里复刻](https://help.aliyun.com/zh/model-studio/voice-clone-design-http-api)、[阿里设计](https://help.aliyun.com/zh/model-studio/voice-design-api-references)、[CosyVoice HTTP 合成](https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api)。

## 生命周期和资源

- 新建前完成参数校验、参考检查和明确云操作确认。先保存本地记录与操作 ID，再在业务任务池执行，避免 UI 与串行 IPC 被训练阻塞。
- 区分创建中、待审核、可用、失败、结果未知、已移除。重启将执行中标记结果未知；保留远端 ID，主动查询不重新创建。
- 名称、标签、启用状态不被云刷新覆盖。移除仅隐藏本地音色，保留历史样本和云槽位。
- `custom_voices/` 属于数据目录，参考/预览 WAV 仅存相对路径。纳入目录迁移与回滚。预览不是训练样本。
- 小米参考音频只在业务进程按需编码；目录、任务详情、异常日志不返回 Base64、文件路径或签名 URL。
- 同一参考跨供应商默认共享声源标识；可指定与真人录音相同的说话人标识。生成样本继承标识，训练划分防止真人/复刻及不同模型泄漏。
- 参考文件与样本库文件 SHA 相同时自动继承其声源；不同片段需显式填写同一说话人 ID。阿里云公网 URL 的内容不在本地抓取，不能自动判断不同链接是否来自同一人。
- 阿里云音色记录创建时地域 / Workspace，设置切换后在合成前校验，禁止把原地域音色误发到新地域。密钥不写入音色记录。
- 豆包复刻 2.0 填写表达指令时使用 `req_params.model=seed-tts-2.0-expressive`；标准版默认过滤指令，不能仅发送 `context_texts` 后宣称生效。依据：[HTTP V3 字段说明](https://docs.volcengine.com/docs/DoubaoVoice/HTTPChunkedSSEUnidirectionalStreaming-V3?lang=zh)。
- 创建、查询和生成任务执行时禁止目录切换；播放通过受限资源复制入口，退出释放音频句柄。

## 原生界面

目录 API `/api/voices` 新增独立 `kind` 参数（空／preset／clone／design／custom），返回 `voice_kind`。本地 `custom_voices.mode` 优先于合成模型分类，避免小米设计结果因后续走 clone 模型被误判；豆包账号 clone 模型可明确归为复刻，来源不明的账号自定义音色不细分。所有筛选按交集生效，异步回调快照包括类型，避免迟到结果覆盖新选择。筛选控件按真实文字宽度换行，五项优先采用均衡的 3＋2 行列，而非裁切或 4＋1。

录音文稿是本地朗读辅助，与生成用的预览文本分离；按创建语言初始化，创建窗口按语言保留草稿，包括取消录音后的编辑。录制期间只读，停止后可改；不声称文稿与录音已自动对齐或转写，也不增加供应商请求字段。

参考录音窗口独立拥有 QAudioSource、播放器和临时目录，按设备真实格式采集后保存为单声道 PCM16 WAV（保留设备采样率，不伪装采样率）。录音格式协商与 PCM 转换复用真人录音入口的共同函数。确认使用后复制到创建表单拥有的临时目录，上传接口复制到数据目录后表单才释放临时文件；取消保留原选择。现场录制不写样本库、不直接调用云服务。创建表单结束、录音窗口关闭、设备断开都必须关闭 WAV、释放输入输出资源后再删除临时文件；不能依赖 QObject 析构结束捕获。

“我的音色”提供新建复刻、文本设计、刷新状态、名称/标签编辑、启停、本地移除。表单按供应商显示必要字段，说明置于带圈问号。列表分列显示供应商、模型、来源、状态，有本地预览时直接播放。可用且启用的音色自动进入现有跨供应商合成目录。
