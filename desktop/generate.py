"""Native batch generation preserving provider capability and billing guards."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (QHBoxLayout, QVBoxLayout, QPlainTextEdit, QLineEdit,
                               QListWidgetItem, QGroupBox, QCheckBox, QLabel, QWidget, QMessageBox, QBoxLayout)
from .base import Page
from .widgets import field, combo, spin, button, HelpButton, Card, Disclosure, CheckableVoiceList
from .services import SERVICES, audio_provider
from voice_features import expression_features, language_groups, resolve_expression

PROVIDERS = SERVICES
TARGETS = [("正样本", "positive"), ("负样本", "negative"), ("背景样本", "background")]
AGES = [("青年", "young"), ("中年", "middle"), ("儿童", "child"), ("老年", "senior"), ("未知", "unknown")]


class GeneratePage(Page):
    def __init__(self, shell):
        super().__init__(shell, "合成创作")
        self.selected, self.voice_rows, self.capabilities, self.audio_options = set(), {}, {}, {}
        self.project = {}
        self.submitting = False
        title = self.layout.takeAt(0).widget()
        heading = QHBoxLayout()
        heading.addWidget(title)
        heading.addStretch()
        heading.addWidget(button("任务中心", lambda: shell.navigate(4)))
        self.start_button = button("开始合成", self.generate, True)
        heading.addWidget(self.start_button)
        self.layout.addLayout(heading)
        project_row = QHBoxLayout()
        self.display_text = QLineEdit("你好，小助手")
        project_row.addWidget(field("项目唤醒词", self.display_text, "保存后统一标签；已有样本不重写。"), 1)
        project_row.addWidget(button("保存唤醒词", self.save_project), 0, Qt.AlignmentFlag.AlignBottom)
        self.layout.addLayout(project_row)
        body = QHBoxLayout()
        self.body_layout = body
        self.shell.installEventFilter(self)
        body.setSpacing(16)
        text_card, voice_card = Card("01  合成文本"), Card("02  选择音色")
        self.texts = QPlainTextEdit("你好，小助手")
        self.texts.setPlaceholderText("每行一条文本")
        self.texts.setMinimumHeight(220)
        self.texts.textChanged.connect(self.update_summary)
        text_card.body.addWidget(self.texts, 1)
        row = QHBoxLayout()
        self.kind = combo(TARGETS)
        self.count = spin(1, 1000, 1)
        self.concurrency = spin(1, 16, 3)
        self.count.valueChanged.connect(self.update_summary)
        for label, widget, tip in [("样本类型", self.kind, "负样本的训练目标为 0。"), ("份数", self.count, "每条文本 × 每个音色的合成份数。"), ("并发", self.concurrency, "同时执行的文本任务数量，仍受业务线程池限制。")]:
            row.addWidget(field(label, widget, tip))
        text_card.body.addLayout(row)
        self.provider = combo(PROVIDERS)
        self.provider.setMinimumHeight(40)
        voice_card.body.addWidget(self.provider)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索音色")
        self.gender = combo([("全部性别", ""), ("女", "female"), ("男", "male")])
        self.age = combo([("全部年龄", "")] + AGES)
        self.language = combo([("全部语言", ""), ("中文", "chinese"), ("外语", "foreign"), ("语言未知", "unknown")])
        for control in (self.language, self.gender, self.age):
            control.setMinimumContentsLength(4)
        self.capability_filter = combo([("全部能力", ""), ("支持指令", "instruction"), ("支持情感", "emotion"), ("无指令控制", "no_instruction")])
        self.capability_filter.setMaximumWidth(160)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.capability_filter)
        voice_card.body.addLayout(search_row)
        for widget in [self.language, HelpButton("按音色支持的语言筛选。多语种音色会同时出现在中文和外语中；供应商未提供语言信息时列为语言未知。切换语言分类会清空已选音色。"), self.gender, self.age]:
            filters.addWidget(widget)
        voice_card.body.addLayout(filters)
        self.provider.currentIndexChanged.connect(self.provider_changed)
        self.gender.currentIndexChanged.connect(self.refresh_voices)
        self.age.currentIndexChanged.connect(self.refresh_voices)
        self.language.currentIndexChanged.connect(self.selection_filter_changed)
        self.capability_filter.currentIndexChanged.connect(self.selection_filter_changed)
        self.search.textChanged.connect(self.refresh_voices)
        self.voices = CheckableVoiceList()
        self.voices.setMinimumHeight(210)
        self.voices.itemChanged.connect(self.voice_changed)
        voice_card.body.addWidget(self.voices, 1)
        voice_actions = QHBoxLayout()
        self.catalog_button = button("获取音色列表", self.refresh_catalog, True)
        voice_actions.addWidget(self.catalog_button)
        voice_actions.addWidget(HelpButton("预置列表不代表账号权限。远程目录查询使用可选 AK/SK；合成前会检查所选服务的凭据。"))
        voice_actions.addStretch()
        voice_actions.addWidget(button("全选", self.select_all))
        voice_actions.addWidget(button("清空", self.clear_voices))
        voice_card.body.addLayout(voice_actions)
        body.addWidget(text_card, 1)
        body.addWidget(voice_card, 1)
        self.layout.addLayout(body)
        self.selection_count = QLabel("0 条文本 · 0 个音色 · 0 次合成")
        self.selection_count.setObjectName("batchSummary")
        self.layout.addWidget(self.selection_count)
        expression = Card()
        expression.body.setContentsMargins(16, 12, 16, 12)
        expression_row = QHBoxLayout()
        self.emotion = combo([("默认情感", "")])
        self.emotion.currentIndexChanged.connect(self.update_expression_scale)
        self.emotion_scale = spin(1, 5, 4, True)
        self.instruction = QLineEdit()
        self.instruction.setMaxLength(500)
        self.instruction.setPlaceholderText("例如：轻快、自然，重点强调唤醒词")
        expression_row.addWidget(field("情感", self.emotion, "只列出已选音色共同支持的情感；豆包多情感音色使用官方参数，其余支持指令的音色使用自然语言控制。"), 1)
        self.scale_field = field("强度", self.emotion_scale, "仅用于音色支持的豆包情感参数，范围 1–5；不是所有音色都支持。")
        expression_row.addWidget(self.scale_field, 0)
        expression_row.addWidget(field("表达指令", self.instruction, "豆包 2.0 与小米可填写自然语言要求。阿里云部分系统音色须用固定格式，如：你说话的情感是happy。选择多个音色时，指令须被全部已选音色支持。"), 3)
        expression.body.addLayout(expression_row)
        self.expression_panel = expression
        self.layout.addWidget(expression)
        parameters = Disclosure("合成参数")
        controls = QHBoxLayout()
        self.speed, self.pitch, self.volume = spin(.5, 2, 1, True), spin(.5, 2, 1, True), spin(0, 100, 50)
        for label, widget in [("语速", self.speed), ("音高", self.pitch), ("音量", self.volume)]:
            controls.addWidget(field(label, widget, "只允许当前服务支持的参数。"))
        parameters.body.addLayout(controls)
        self.advanced = parameters.content
        randoms = Disclosure("随机变化与音频格式")
        self.random_fields = {}
        for name, label, minimum, maximum, low, high in [("speed", "随机语速", .5, 2, .8, 1.2), ("pitch", "随机音高", .5, 2, .9, 1.1), ("volume", "随机音量", 0, 100, 40, 60)]:
            enable = QCheckBox(label)
            start, end = spin(minimum, maximum, low, name != "volume"), spin(minimum, maximum, high, name != "volume")
            start.setEnabled(False)
            end.setEnabled(False)
            enable.toggled.connect(start.setEnabled)
            enable.toggled.connect(end.setEnabled)
            group = QHBoxLayout()
            for widget in [enable, start, QLabel("—"), end]:
                group.addWidget(widget)
            randoms.body.addLayout(group)
            self.random_fields[name] = enable, start, end
        self.seed = spin(0, 2**31-1, 20260804)
        randoms.body.addWidget(field("随机种子", self.seed, "用于确定性参数与可复现增强。"))
        audio_row = QHBoxLayout()
        self.audio_format, self.sample_rate = combo([]), combo([])
        self.audio_format.currentIndexChanged.connect(self.store_audio_options)
        self.sample_rate.currentIndexChanged.connect(self.store_audio_options)
        audio_row.addWidget(field("请求格式", self.audio_format, "入库统一为 PCM16 WAV。"))
        audio_row.addWidget(field("请求采样率", self.sample_rate, "入库与导出统一为 16 kHz mono。"))
        randoms.body.addLayout(audio_row)
        parameters.body.addWidget(randoms)
        self.layout.addWidget(parameters)
        self.billing = QCheckBox("我已了解云合成费用由当前账号承担")
        self.layout.addWidget(self.billing)
        issue = QHBoxLayout()
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.problem.setObjectName("errorBanner")
        self.configure_button = button("去配置", self.open_settings)
        self.configure_button.hide()
        issue.addWidget(self.problem, 1)
        issue.addWidget(self.configure_button)
        self.layout.addLayout(issue)
        self.layout.addStretch()

    def eventFilter(self, watched, event):
        if watched is self.shell and event.type() == QEvent.Type.Resize:
            # Three parameter columns and voice filters need more room than a
            # 980px window can offer side by side. Stack the cards before the
            # page's minimum width clips the right controls; vertical scrolling
            # already belongs to the shell's QScrollArea.
            direction = QBoxLayout.Direction.TopToBottom if event.size().width() < 1120 else QBoxLayout.Direction.LeftToRight
            self.body_layout.setDirection(direction)
        return super().eventFilter(watched, event)

    def update_summary(self):
        if not hasattr(self, "selection_count"):
            return
        texts = len([line for line in self.texts.toPlainText().splitlines() if line.strip()])
        voices = len(self.selected)
        self.selection_count.setText(f"{texts} 条文本 · {voices} 个音色 · {texts * voices * self.count.value()} 次合成")

    def provider_changed(self):
        # Selections never cross service boundaries invisibly. beta.2 could
        # retain a hidden V2 voice when the user switched back to V1.
        self.selected.clear()
        self.voice_rows.clear()
        self.problem.clear()
        self.configure_button.hide()
        self.update_summary()
        self.update_capabilities()
        self.refresh_voices()

    def selection_filter_changed(self):
        # Switching language categories must not keep invisible voices selected
        # and unexpectedly send them in a foreign-language batch.
        self.selected.clear()
        self.update_summary()
        self.update_expression()
        self.refresh_voices()

    def select_all(self):
        for index in range(self.voices.count()):
            self.voices.item(index).setCheckState(Qt.CheckState.Checked)

    def open_settings(self):
        self.shell.navigate(6)
        self.shell.pages[6].select_service(self.provider.currentData())

    def voice_changed(self, item):
        identifier = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.selected.add(identifier)
        else:
            self.selected.discard(identifier)
        self.update_summary()
        self.update_capabilities()

    def clear_voices(self):
        self.selected.clear()
        self.update_summary()
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
        capability = self.capabilities.get(audio_provider(provider), {})
        saved = self.audio_options.get(provider, {})
        self.audio_format.blockSignals(True)
        self.sample_rate.blockSignals(True)
        self.audio_format.clear()
        self.sample_rate.clear()
        formats = capability.get("audio_formats", [])
        for format in formats:
            self.audio_format.addItem(format.upper(), format)
        for rate in capability.get("sample_rates", []):
            self.sample_rate.addItem(f"{rate} Hz", rate)
        self.audio_format.setCurrentIndex(max(0, self.audio_format.findData(saved.get("format", "wav"))))
        self.sample_rate.setCurrentIndex(max(0, self.sample_rate.findData(saved.get("sample_rate", 16000))))
        self.audio_format.blockSignals(False)
        self.sample_rate.blockSignals(False)
        self.store_audio_options()
        providers = {audio_provider(provider)}
        for name, control, default in (("pitch", self.pitch, 1), ("speed", self.speed, 1), ("volume", self.volume, 50)):
            supported = all(self.capabilities.get(item, {}).get(name, True) for item in providers)
            control.setEnabled(supported)
            self.random_fields[name][0].setEnabled(supported)
            if not supported:
                control.setValue(default)
                self.random_fields[name][0].setChecked(False)
        self.billing.setVisible(provider != "mock")
        self.update_expression()

    def update_expression(self):
        if not hasattr(self, "expression_panel"):
            return
        selected = [self.voice_rows[identifier] for identifier in self.selected if identifier in self.voice_rows]
        features = [row.get("expression_features", expression_features(row)) for row in selected]
        # Keep the draft editable across selection changes. Clearing/disabling
        # it when an unsupported voice is checked destroys the user's work and
        # hides the conflict. Validate the exact batch on Start instead.
        self.instruction.setEnabled(True)
        self.instruction.setPlaceholderText("例如：你说话的情感是happy。" if any(item["instruction"] == "fixed" for item in features) else "例如：轻快、自然，重点强调唤醒词")
        # A batch uses one expression setting. Intersect exact voice capabilities
        # instead of advertising every provider's global emotions for each voice.
        common = set(item["value"] for item in features[0]["emotions"]) if features else set()
        for item in features[1:]:
            common &= {option["value"] for option in item["emotions"]}
        previous = self.emotion.currentData()
        self.emotion.blockSignals(True)
        self.emotion.clear()
        self.emotion.addItem("默认情感", "")
        for option in features[0]["emotions"] if features else []:
            if option["value"] in common:
                self.emotion.addItem(option["label"], option["value"])
        self.emotion.setCurrentIndex(max(0, self.emotion.findData(previous)))
        self.emotion.blockSignals(False)
        self.emotion.setEnabled(bool(common))
        self.supports_emotion_scale = bool(features) and all(item["emotion_scale"] for item in features)
        self.scale_field.setVisible(self.supports_emotion_scale)
        self.update_expression_scale()
        self.expression_panel.setVisible(True)

    def update_expression_scale(self):
        supported = getattr(self, "supports_emotion_scale", False) and bool(self.emotion.currentData())
        if hasattr(self, "emotion_scale"):
            self.emotion_scale.setEnabled(supported)
            if not supported:
                self.emotion_scale.setValue(4)

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
        snapshot = (self.provider.currentData(), self.gender.currentData(), self.age.currentData(), self.search.text(), self.language.currentData(), self.capability_filter.currentData())
        self.request("/api/voices", params={"provider": self.provider.currentData(), "gender": self.gender.currentData(), "age_group": self.age.currentData(), "language_group": self.language.currentData(), "capability": self.capability_filter.currentData()},
                     callback=lambda rows: self.show_voices(rows) if snapshot == (self.provider.currentData(), self.gender.currentData(), self.age.currentData(), self.search.text(), self.language.currentData(), self.capability_filter.currentData()) else None)

    def show_voices(self, rows):
        self.voices.blockSignals(True)
        self.voices.clear()
        query = self.search.text().strip().casefold()
        for row in rows:
            self.voice_rows[row["id"]] = row
            if query and query not in (row["name"] + " " + row["voice_id"]).casefold():
                continue
            gender = {"female": "女", "male": "男", "unknown": ""}.get(row.get("gender"), "")
            groups = language_groups(row)
            language = "中/外语" if len(groups) == 2 else "中文" if "chinese" in groups else "外语" if "foreign" in groups else "语言未知"
            features = row["expression_features"]
            instruction = {"free": "指令", "fixed": "固定指令", "none": "无指令"}[features["instruction"]]
            traits = instruction + ("/情感" if features["emotions"] else "")
            item = QListWidgetItem(row['name'])
            age = dict((value, label) for label, value in AGES).get(row.get("age_group"), "")
            item.setData(Qt.ItemDataRole.UserRole + 1, {"demographics": " · ".join(value for value in (gender, age) if value), "language": language,
                         "capability": traits, "supported": features["instruction"] != "none" or bool(features["emotions"])})
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setToolTip(f"{row['voice_id']}\n{row.get('model', '')}\n语言：{', '.join(row.get('languages', [])) or '未知'}\n表达能力：{traits}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if row["id"] in self.selected else Qt.CheckState.Unchecked)
            self.voices.addItem(item)
        self.voices.blockSignals(False)
        self.update_summary()
        self.update_expression()

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
                  "instruction": self.instruction.text(), "emotion": self.emotion.currentData() or "",
                  "emotion_scale": self.emotion_scale.value(), "cost_acknowledged": self.billing.isChecked()}
        for name, (enable, start, end) in self.random_fields.items():
            result.update({"random_" + name: enable.isChecked(), name + "_min": start.value(), name + "_max": end.value()})
        service = self.provider.currentData()
        result["provider_audio"] = {audio_provider(service): self.audio_options[service]} if service in self.audio_options else {}
        return result

    def generate(self):
        if self.submitting:
            return
        if not self.selected:
            self.creation_failed("请选择音色")
            return
        if not self.texts.toPlainText().strip():
            self.creation_failed("请输入至少一条合成文本")
            return
        # Reject incompatible expression controls synchronously, before the
        # credentials preflight, billing confirmation or any task submission.
        # The backend repeats this check against authoritative catalog rows;
        # this UI check preserves the form and explains all conflicting voices.
        conflicts = []
        for identifier in sorted(self.selected):
            voice = self.voice_rows.get(identifier)
            if voice is None:
                conflicts.append("音色信息尚未载入，请获取音色列表后重试")
                continue
            try:
                resolve_expression(voice, self.instruction.text(), self.emotion.currentData() or "", self.emotion_scale.value())
            except ValueError as error:
                reason = str(error).removeprefix(f"音色 {voice['name']} ")
                conflicts.append(f"{voice['name']}：{reason}")
        if conflicts:
            message = "\n".join(conflicts) + "\n\n请取消这些音色，或调整表达指令／情感。已填写内容和勾选音色均已保留。"
            self.creation_failed("所选音色与表达设置不匹配")
            QMessageBox.warning(self, "请调整音色或表达设置", message)
            return
        cloud = self.provider.currentData() != "mock"
        if cloud and not self.billing.isChecked():
            self.creation_failed("请确认云合成费用")
            return
        try:
            payload = self.payload()
        except (ValueError, KeyError):
            self.creation_failed("请检查参数或等待项目初始化")
            return
        self.problem.clear()
        self.configure_button.hide()
        self.submitting = True
        self.start_button.setEnabled(False)
        self.request("/api/generation/preflight", "POST", json={key: payload[key] for key in ("voice_ids", "instruction", "emotion", "emotion_scale")},
                     callback=lambda result: self.preflight_ready(payload, cloud), error=self.preflight_failed)

    def preflight_failed(self, error):
        self.creation_failed(error)
        if "指令" in str(error) or "情感" in str(error):
            QMessageBox.warning(self, "请调整音色或表达设置", str(error) + "\n\n已填写内容和勾选音色均已保留。")

    def preflight_ready(self, payload, cloud):
        calls = len([t for t in payload["texts"] if t.strip()]) * len(payload["voice_ids"]) * payload["count"]
        if cloud and not self.confirm(f"即将发起最多 {calls} 次云合成调用。最终费用以厂商账单为准，是否继续？"):
            self.submitting = False
            self.start_button.setEnabled(True)
            return
        # Snapshot the form before preflight. Editing while a response is in
        # flight must not silently change the billable batch being confirmed.
        self.request("/api/tasks/generate-batch", "POST", json=payload, callback=self.created, error=self.creation_failed)

    def created(self, result):
        self.submitting = False
        self.start_button.setEnabled(True)
        self.notify("合成任务已创建")
        self.shell.navigate(4)

    def creation_failed(self, error):
        self.submitting = False
        self.start_button.setEnabled(True)
        self.problem.setText(str(error))
        self.configure_button.setVisible("配置" in str(error) or "API Key" in str(error))
        self.shell.set_status(str(error), True)
