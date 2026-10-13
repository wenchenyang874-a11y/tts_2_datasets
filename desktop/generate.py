"""Native batch generation preserving provider capability and billing guards."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QPlainTextEdit, QLineEdit,
                               QListWidget, QListWidgetItem, QGroupBox, QCheckBox, QLabel, QWidget)
from .base import Page
from .widgets import field, combo, spin, button, HelpButton

PROVIDERS = [("豆包语音 · 火山引擎", "volcengine"), ("阿里云", "aliyun"), ("小米 MiMo", "xiaomi"), ("离线验证 · Mock", "mock")]
TARGETS = [("正样本", "positive"), ("负样本", "negative"), ("背景样本", "background")]
AGES = [("青年", "young"), ("中年", "middle"), ("儿童", "child"), ("老年", "senior"), ("未知", "unknown")]


class GeneratePage(Page):
    def __init__(self, shell):
        super().__init__(shell, "合成创作")
        self.selected = set()
        self.voice_rows = {}
        self.capabilities = {}
        self.audio_options = {}
        self.project = {}
        self.submitting = False
        self.display_text = QLineEdit("你好，小助手")
        project_row = QHBoxLayout()
        project_row.addWidget(field("唤醒词", self.display_text, "保存后统一生成目标标签；修改不会重写已有样本。"), 1)
        project_row.addWidget(button("保存唤醒词", self.save_project), 0, Qt.AlignmentFlag.AlignBottom)
        self.layout.addLayout(project_row)
        body = QHBoxLayout()
        left = QVBoxLayout()
        self.texts = QPlainTextEdit("你好，小助手")
        self.texts.setPlaceholderText("每行一条文本")
        self.texts.setMinimumHeight(165)
        left.addWidget(field("合成文本", self.texts, "每行创建一个子任务。Mock 只生成测试音调，不是真实人声。"))
        row = QHBoxLayout()
        self.kind = combo(TARGETS)
        self.count = spin(1, 1000, 1)
        self.concurrency = spin(1, 16, 3)
        for label, widget, help_text in [("样本目标", self.kind, "负样本仍属于当前唤醒词，训练目标为 0。"),
                                          ("每音色份数", self.count, "每条文本 × 音色 × 份数形成合成调用。"),
                                          ("并发", self.concurrency, "实际并发同时受全局线程池与供应商能力限制。")]:
            row.addWidget(field(label, widget, help_text))
        left.addLayout(row)
        body.addLayout(left, 3)
        right = QVBoxLayout()
        filters = QHBoxLayout()
        self.provider = combo(PROVIDERS)
        self.gender = combo([("全部性别", ""), ("女", "female"), ("男", "male")])
        self.model = combo([("全部模型", ""), ("豆包语音 1.0", "v1"), ("豆包语音 2.0", "seed-tts-2.0")])
        self.model.currentIndexChanged.connect(self.refresh_voices)
        self.model.currentIndexChanged.connect(self.update_capabilities)
        self.age = combo([("全部年龄", "")] + AGES)
        for widget in [self.provider, self.gender, self.age]:
            filters.addWidget(widget)
            widget.currentIndexChanged.connect(self.refresh_voices)
        self.provider.currentIndexChanged.connect(self.update_capabilities)
        right.addLayout(filters)
        catalog_row = QHBoxLayout()
        catalog_row.addWidget(self.model)
        self.catalog_button = button("获取音色列表", self.refresh_catalog, True)
        catalog_row.addWidget(self.catalog_button)
        catalog_row.addWidget(HelpButton("小米加载官方预置音色快照。豆包远程目录查询使用独立 AK/SK；合成 API Key 不等于目录密钥。显示音色不代表账号已开通对应资源。"))
        right.addLayout(catalog_row)
        self.voices = QListWidget()
        self.voices.itemChanged.connect(self.voice_changed)
        right.addWidget(self.voices)
        self.selection_count = QLabel("已选 0 个音色")
        right.addWidget(self.selection_count)
        voice_actions = QHBoxLayout()
        voice_actions.addWidget(button("清空选择", self.clear_voices))
        right.addLayout(voice_actions)
        body.addLayout(right, 4)
        self.layout.addLayout(body)
        parameters = QGroupBox("合成参数")
        p = QVBoxLayout(parameters)
        controls = QHBoxLayout()
        self.speed = spin(.5, 2, 1, True)
        self.pitch = spin(.5, 2, 1, True)
        self.volume = spin(0, 100, 50)
        self.seed = spin(0, 2**31-1, 20260804)
        for label, widget, tip in [("语速", self.speed, "供应商不支持的参数由业务层拒绝，不静默发送。"),
                                   ("音高", self.pitch, "接口支持情况依供应商和模型而定。"),
                                   ("音量", self.volume, "供应商 0–100 相对控制量，不是声学 dB。"),
                                   ("随机种子", self.seed, "用于确定性参数与可复现增强。")]:
            controls.addWidget(field(label, widget, tip))
        p.addLayout(controls)
        self.advanced = QWidget()
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        randoms = QVBoxLayout()
        self.random_fields = {}
        for name, label, minimum, maximum, low, high in [
            ("speed", "随机语速", .5, 2, .8, 1.2),
            ("pitch", "随机音高", .5, 2, .9, 1.1),
            ("volume", "随机音量", 0, 100, 40, 60)]:
            enable = QCheckBox(label)
            start = spin(minimum, maximum, low, name != "volume")
            end = spin(minimum, maximum, high, name != "volume")
            group = QHBoxLayout()
            group.addWidget(enable)
            group.addWidget(start)
            group.addWidget(QLabel("—"))
            group.addWidget(end)
            randoms.addLayout(group)
            self.random_fields[name] = enable, start, end
        advanced.addLayout(randoms)
        self.instruction = QLineEdit()
        advanced.addWidget(field("风格指令", self.instruction, "仅发送给支持该能力的接口，留空使用音色默认风格。"))
        audio_row = QHBoxLayout()
        self.audio_format = combo([])
        self.sample_rate = combo([])
        self.audio_format.currentIndexChanged.connect(self.store_audio_options)
        self.sample_rate.currentIndexChanged.connect(self.store_audio_options)
        audio_row.addWidget(field("请求格式", self.audio_format, "向供应商请求的格式；入库仍统一 PCM16 WAV。只显示该供应商支持的选项。"))
        audio_row.addWidget(field("请求采样率", self.sample_rate, "请求采样率与训练音频不同；入库和导出统一为 16 kHz mono。"))
        advanced.addLayout(audio_row)
        p.addWidget(button("高级参数", lambda: self.advanced.setVisible(not self.advanced.isVisible())))
        p.addWidget(self.advanced)
        self.advanced.hide()
        self.layout.addWidget(parameters)
        self.billing = QCheckBox("我已了解云调用费用由凭据所属账号承担")
        self.layout.addWidget(self.billing)
        # Keep the primary action in the heading: at 150% DPI Windows can
        # constrain the window to a shorter logical height. A bottom-only
        # action otherwise disappears below the first visible form viewport.
        title = self.layout.takeAt(0).widget()
        heading = QHBoxLayout()
        heading.addWidget(title)
        heading.addStretch()
        heading.addWidget(button("查看任务", lambda: shell.navigate(4)))
        self.start_button = button("开始合成", self.generate, True)
        heading.addWidget(self.start_button)
        self.layout.insertLayout(0, heading)
        self.layout.addStretch()

    def voice_changed(self, item):
        identifier = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(identifier)
        else:
            self.selected.discard(identifier)
        self.selection_count.setText(f"已选 {len(self.selected)} 个音色")
        self.update_capabilities()

    def clear_voices(self):
        self.selected.clear()
        self.selection_count.setText("已选 0 个音色")
        self.refresh_voices()

    def refresh(self):
        self.request("/api/settings/project", callback=self.set_project)
        self.request("/api/providers/capabilities", callback=self.set_capabilities)
        self.refresh_voices()

    def set_capabilities(self, result):
        self.capabilities = result
        self.update_capabilities()

    def update_capabilities(self):
        if not hasattr(self, "audio_format"):
            return
        provider = self.provider.currentData()
        capability = self.capabilities.get(provider, {})
        saved = self.audio_options.get(provider, {})
        self.audio_format.blockSignals(True)
        self.sample_rate.blockSignals(True)
        self.audio_format.clear()
        self.sample_rate.clear()
        formats = capability.get("audio_formats", [])
        if provider == "volcengine" and (self.model.currentData() == "seed-tts-2.0" or any(self.voice_rows[v].get("model") == "seed-tts-2.0" for v in self.selected)):
            formats = [value for value in formats if value != "ogg_opus"]
        for format in formats:
            self.audio_format.addItem(format.upper(), format)
        for rate in capability.get("sample_rates", []):
            self.sample_rate.addItem(f"{rate} Hz", rate)
        self.audio_format.setCurrentIndex(max(0, self.audio_format.findData(saved.get("format", "wav"))))
        self.sample_rate.setCurrentIndex(max(0, self.sample_rate.findData(saved.get("sample_rate", 16000))))
        self.audio_format.blockSignals(False)
        self.sample_rate.blockSignals(False)
        self.store_audio_options()
        providers = {self.voice_rows[v]["provider"] for v in self.selected} or {provider}
        for name, control, default in (("pitch", self.pitch, 1), ("speed", self.speed, 1), ("volume", self.volume, 50)):
            supported = all(self.capabilities.get(item, {}).get(name, True) for item in providers)
            control.setEnabled(supported)
            self.random_fields[name][0].setEnabled(supported)
            if not supported:
                control.setValue(default)
                self.random_fields[name][0].setChecked(False)
        self.model.setVisible(provider == "volcengine")

    def store_audio_options(self):
        if self.audio_format.currentData() and self.sample_rate.currentData():
            self.audio_options[self.provider.currentData()] = {"format": self.audio_format.currentData(), "sample_rate": self.sample_rate.currentData()}

    def set_project(self, result):
        self.project = result
        self.display_text.setText(result["display_text"])
        if not self.texts.document().isModified():
            self.texts.setPlainText(result["display_text"])
        recording = self.shell.pages[2]
        if not recording.text.isModified():
            recording.text.setText(result["display_text"])
        self.shell.project = result
        self.shell.project_label.setText(result["display_text"])

    def save_project(self):
        self.request("/api/settings/project", "PUT", json={"display_text": self.display_text.text()},
                     callback=lambda result: (self.set_project(result), self.notify("唤醒词已保存")))

    def refresh_voices(self):
        snapshot = (self.provider.currentData(), self.gender.currentData(), self.age.currentData(), self.model.currentData())
        self.request("/api/voices", params={"provider": self.provider.currentData(),
                     "gender": self.gender.currentData(), "age_group": self.age.currentData()},
                     callback=lambda rows: self.show_voices(rows) if snapshot == (self.provider.currentData(), self.gender.currentData(), self.age.currentData(), self.model.currentData()) else None)

    def show_voices(self, rows):
        if self.provider.currentData() == "volcengine" and self.model.currentData():
            selected_model = self.model.currentData()
            rows = [row for row in rows if (row["model"] == "seed-tts-2.0") == (selected_model == "seed-tts-2.0")]
        self.voices.blockSignals(True)
        self.voices.clear()
        for row in rows:
            self.voice_rows[row["id"]] = row
            model = "豆包 2.0" if row.get("model") == "seed-tts-2.0" else ("豆包 1.0" if row["provider"] == "volcengine" else row.get("model", ""))
            item = QListWidgetItem(f"{row['name']}  ·  {model}")
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setToolTip(f"{row['voice_id']}\n{row.get('gender', '')} · {row.get('age_group', '')}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if row["id"] in self.selected else Qt.CheckState.Unchecked)
            self.voices.addItem(item)
        self.voices.blockSignals(False)

    def refresh_catalog(self):
        self.catalog_button.setEnabled(False)
        self.notify("正在获取音色列表…")
        self.request("/api/voices/refresh", "POST", json={"provider": self.provider.currentData()},
                     callback=lambda result: (self.catalog_button.setEnabled(True), self.notify(f"已获取 {result['count']} 个音色" + ("；" + "；".join(result.get("warnings", [])) if result.get("warnings") else "")), self.refresh_voices()),
                     error=lambda error: (self.catalog_button.setEnabled(True), self.shell.error(error)))

    def payload(self):
        result = {"texts": self.texts.toPlainText().splitlines(), "voice_ids": sorted(self.selected),
                  "keyword_id": self.shell.project["keyword_id"], "label": self.shell.project["label"],
                  "target_kind": self.kind.currentData(), "count": self.count.value(),
                  "concurrency": self.concurrency.value(), "speed": self.speed.value(),
                  "pitch": self.pitch.value(), "volume": self.volume.value(), "seed": self.seed.value(),
                  "instruction": self.instruction.text(), "cost_acknowledged": self.billing.isChecked()}
        for name, (enable, start, end) in self.random_fields.items():
            result.update({"random_" + name: enable.isChecked(), name + "_min": start.value(), name + "_max": end.value()})
        providers = {self.voice_rows[v]["provider"] for v in self.selected}
        result["provider_audio"] = {name: value for name, value in self.audio_options.items() if name in providers}
        return result

    def generate(self):
        if self.submitting:
            return
        if not self.selected:
            self.shell.error("请选择音色")
            return
        cloud = any(self.voice_rows[v]["provider"] != "mock" for v in self.selected)
        if cloud and not self.billing.isChecked():
            self.shell.error("云合成前请确认费用责任")
            return
        calls = len([t for t in self.texts.toPlainText().splitlines() if t.strip()]) * len(self.selected) * self.count.value()
        if cloud and not self.confirm(f"即将发起最多 {calls} 次云合成调用。最终费用以厂商账单为准，是否继续？"):
            return
        try:
            payload = self.payload()
        except (ValueError, KeyError):
            self.shell.error("请检查合成参数或等待项目初始化")
            return
        # Disable creation until the acknowledgement returns. A double click
        # must not silently create a second potentially billable batch.
        self.submitting = True
        self.start_button.setEnabled(False)
        self.request("/api/tasks/generate-batch", "POST", json=payload,
                     callback=self.created, error=self.creation_failed)

    def created(self, result):
        self.submitting = False
        self.start_button.setEnabled(True)
        self.notify("合成任务已创建")
        self.shell.navigate(4)

    def creation_failed(self, error):
        self.submitting = False
        self.start_button.setEnabled(True)
        self.shell.error(error)
