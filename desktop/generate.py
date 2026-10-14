"""Native batch generation preserving provider capability and billing guards."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QListWidgetItem, QMessageBox
from .base import Page
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
        self.pending_locate = None
        from .composer_workspace import build_workspace
        build_workspace(self, self.layout.takeAt(0).widget(), TARGETS, AGES)

    def eventFilter(self, watched, event):
        if watched is getattr(self, "voice_pane", None) and event.type() == QEvent.Type.Resize:
            columns = 4 if event.size().width() >= 445 else 2
            if columns != getattr(self, "filter_columns", None):
                self.filter_columns = columns
                for index, control in enumerate(self.filter_controls):
                    self.filter_grid.removeWidget(control)
                    self.filter_grid.addWidget(control, index//columns, index%columns)
                for index in range(4):
                    self.filter_grid.setColumnStretch(index, int(index < columns))
        return super().eventFilter(watched, event)

    def viewport(self):
        return self

    def ensureWidgetVisible(self, widget):
        # There is no page scroll area. Reveal a control's tab instead of moving
        # the submit action out of view; list/text scrolling stays local.
        ancestor = widget
        while ancestor is not None:
            index = self.editor_tabs.indexOf(ancestor)
            if index >= 0:
                self.editor_tabs.setCurrentIndex(index)
                break
            ancestor = ancestor.parentWidget()

    def render_selected(self):
        from .services import service_for_voice
        labels = dict((key, label) for label, key in SERVICES)
        self.chosen_voices.clear()
        for identifier in sorted(self.selected):
            voice = self.voice_rows.get(identifier)
            if voice is None:
                continue
            item = QListWidgetItem(voice['name'])
            item.setData(Qt.ItemDataRole.UserRole, identifier)
            item.setData(Qt.ItemDataRole.UserRole+1, {'service': labels.get(service_for_voice(voice), voice['provider'])})
            item.setToolTip(voice['name']+'\n'+voice['voice_id'])
            self.chosen_voices.addItem(item)
        self.selected_count.setText(f"{len(self.selected)} 个音色")

    def remove_voice(self, identifier):
        self.selected.discard(identifier)
        self.voices.blockSignals(True)
        for index in range(self.voices.count()):
            item = self.voices.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == identifier:
                item.setCheckState(Qt.CheckState.Unchecked)
        self.voices.blockSignals(False)
        self.update_summary()
        self.update_capabilities()

    def locate_voice(self, identifier):
        from .services import service_for_voice
        voice = self.voice_rows.get(identifier)
        if voice is None:
            return
        self.pending_locate = identifier
        self.provider.setCurrentIndex(self.provider.findData(service_for_voice(voice)))
        for control in self.filter_controls:
            control.setCurrentIndex(0)
        self.search.clear()
        self.refresh_voices()

    def update_summary(self):
        if not hasattr(self, "selection_count"):
            return
        texts = len([line for line in self.texts.toPlainText().splitlines() if line.strip()])
        voices = len(self.selected)
        self.selection_count.setText(f"{texts} 条文本 · {voices} 个音色 · {texts * voices * self.count.value()} 次合成")
        if hasattr(self, "start_button"):
            self.start_button.setEnabled(not self.submitting and texts > 0 and voices > 0)
        if hasattr(self, "selection_timer"):
            # Do not delete a basket QListWidgetItem inside its own mouse/key
            # signal delivery. Coalesce rendering on an owned timer after the
            # current native event; dataset switches delete the timer as well.
            self.selection_timer.start(0)

    def provider_changed(self):
        # Browsing is not batch selection. Keep global selected IDs and their
        # catalog metadata; the persistent basket makes other suppliers visible.
        self.problem.clear()
        self.problem.hide()
        self.configure_button.hide()
        self.update_capabilities()
        self.refresh_voices()

    def selection_filter_changed(self):
        self.refresh_voices()

    def select_all(self):
        # Updating hundreds of checks one by one with live signals repeatedly
        # rebuilds capability controls. Batch the state change to keep UI usable.
        self.voices.blockSignals(True)
        for index in range(self.voices.count()):
            item = self.voices.item(index)
            item.setCheckState(Qt.CheckState.Checked)
            self.selected.add(item.data(Qt.ItemDataRole.UserRole))
        self.voices.blockSignals(False)
        self.update_summary()
        self.update_capabilities()

    def open_settings(self):
        from .services import service_for_voice
        service = self.provider.currentData()
        labels = dict((key, label) for label, key in SERVICES)
        for identifier in sorted(self.selected):
            voice = self.voice_rows.get(identifier)
            if voice and labels.get(service_for_voice(voice), '') in self.problem.text():
                service = service_for_voice(voice)
                break
        self.shell.navigate(6)
        if service:
            self.shell.pages[6].select_service(service)

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
        self.update_capabilities()
        self.refresh_voices()

    def refresh(self):
        self.request("/api/settings/project", callback=self.set_project)
        self.request("/api/providers/capabilities", callback=self.set_capabilities)
        self.refresh_voices()

    def set_capabilities(self, result):
        self.capabilities = result
        self.update_capabilities()

    def update_capabilities(self):
        if not hasattr(self, "audio_supplier"):
            return
        providers = {self.voice_rows[identifier]['provider'] for identifier in self.selected if identifier in self.voice_rows}
        if not providers:
            providers = {audio_provider(self.provider.currentData()) or 'volcengine'}
        previous = self.audio_supplier.currentData()
        self.audio_supplier.blockSignals(True)
        self.audio_supplier.clear()
        labels = {'volcengine': '豆包', 'xiaomi': '小米 MiMo', 'aliyun': '阿里云', 'mock': '离线测试'}
        for provider in sorted(providers):
            self.audio_supplier.addItem(labels.get(provider, provider), provider)
            capability = self.capabilities.get(provider, {})
            formats, rates = capability.get('audio_formats', []), capability.get('sample_rates', [])
            if formats and rates:
                self.audio_options.setdefault(provider, {'format': 'wav' if 'wav' in formats else formats[0], 'sample_rate': 16000 if 16000 in rates else rates[0]})
        self.audio_supplier.setCurrentIndex(max(0, self.audio_supplier.findData(previous)))
        self.audio_supplier.blockSignals(False)
        self.update_audio_controls()
        for name, control, default in (("pitch", self.pitch, 1), ("speed", self.speed, 1), ("volume", self.volume, 50)):
            supported = all(self.capabilities.get(item, {}).get(name, True) for item in providers)
            control.setEnabled(supported)
            self.random_fields[name][0].setEnabled(supported)
            if not supported:
                control.setValue(default)
                self.random_fields[name][0].setChecked(False)
        self.update_expression()

    def update_audio_controls(self):
        if not hasattr(self, 'audio_format'):
            return
        provider = self.audio_supplier.currentData()
        capability = self.capabilities.get(provider, {})
        saved = self.audio_options.get(provider, {})
        self.audio_format.blockSignals(True)
        self.sample_rate.blockSignals(True)
        self.audio_format.clear()
        self.sample_rate.clear()
        for value in capability.get('audio_formats', []):
            self.audio_format.addItem(value.upper(), value)
        for value in capability.get('sample_rates', []):
            self.sample_rate.addItem(f'{value} Hz', value)
        self.audio_format.setCurrentIndex(max(0, self.audio_format.findData(saved.get('format', 'wav'))))
        self.sample_rate.setCurrentIndex(max(0, self.sample_rate.findData(saved.get('sample_rate', 16000))))
        self.audio_format.blockSignals(False)
        self.sample_rate.blockSignals(False)
        self.store_audio_options()

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
        # QTabWidget owns tab visibility. Calling setVisible(True) on a hidden
        # tab bypasses its stack and can overlay the active text/parameter tab.

    def update_expression_scale(self):
        supported = getattr(self, "supports_emotion_scale", False) and bool(self.emotion.currentData())
        if hasattr(self, "emotion_scale"):
            self.emotion_scale.setEnabled(supported)
            if not supported:
                self.emotion_scale.setValue(4)

    def store_audio_options(self):
        if self.audio_format.currentData() and self.sample_rate.currentData():
            self.audio_options[self.audio_supplier.currentData()] = {"format": self.audio_format.currentData(), "sample_rate": self.sample_rate.currentData()}

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
            if features.get("instruction_unknown"):
                instruction = "未标注指令"
            traits = instruction + ("/情感" if features["emotions"] else "")
            item = QListWidgetItem(row['name'])
            age = dict((value, label) for label, value in AGES).get(row.get("age_group"), "")
            from .services import service_for_voice
            supplier = dict((value, label) for label, value in SERVICES).get(service_for_voice(row), row['provider'])
            item.setData(Qt.ItemDataRole.UserRole + 1, {"demographics": " · ".join(value for value in (supplier, gender, age) if value), "language": language,
                         "capability": traits, "supported": features["instruction"] != "none" or bool(features["emotions"])})
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setToolTip(f"{row['voice_id']}\n{row.get('model', '')}\n语言：{', '.join(row.get('languages', [])) or '未知'}\n表达能力：{traits}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if row["id"] in self.selected else Qt.CheckState.Unchecked)
            self.voices.addItem(item)
        self.voices.blockSignals(False)
        self.visible_count.setText(f"{self.voices.count()} 个音色")
        if self.pending_locate is not None:
            for index in range(self.voices.count()):
                item = self.voices.item(index)
                if item.data(Qt.ItemDataRole.UserRole) == self.pending_locate:
                    self.voices.scrollToItem(item)
                    self.voices.setCurrentItem(item)
                    self.pending_locate = None
                    break
        self.update_summary()
        self.update_expression()

    def refresh_catalog(self):
        self.catalog_button.setEnabled(False)
        self.notify("正在获取音色列表…")
        self.request("/api/voices/refresh", "POST", json={"provider": self.provider.currentData()} if self.provider.currentData() else {},
                     callback=lambda result: (self.catalog_button.setEnabled(True), self.notify(f"已获取 {result['count']} 个音色" + ("；" + "；".join(result.get("warnings", [])) if result.get("warnings") else "")), self.refresh_voices()),
                     error=lambda error: (self.catalog_button.setEnabled(True), self.shell.error(error)))

    def payload(self):
        result = {"texts": self.texts.toPlainText().splitlines(), "voice_ids": sorted(self.selected),
                  "keyword_id": self.shell.project["keyword_id"], "label": self.shell.project["label"],
                  "target_kind": self.kind.currentData(), "count": self.count.value(),
                  "concurrency": self.concurrency.value(), "speed": self.speed.value(),
                  "pitch": self.pitch.value(), "volume": self.volume.value(), "seed": self.seed.value(),
                  "instruction": self.instruction.text(), "emotion": self.emotion.currentData() or "",
                  "emotion_scale": self.emotion_scale.value(), "cost_acknowledged": False}
        for name, (enable, start, end) in self.random_fields.items():
            result.update({"random_" + name: enable.isChecked(), name + "_min": start.value(), name + "_max": end.value()})
        providers = {self.voice_rows[identifier]['provider'] for identifier in self.selected if identifier in self.voice_rows}
        result["provider_audio"] = {provider: dict(self.audio_options[provider]) for provider in providers if provider in self.audio_options}
        return result

    def generate(self):
        from .diagnostics import event
        event('generation.start-click')
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
        cloud = any(self.voice_rows[identifier]["provider"] != "mock" for identifier in self.selected)
        try:
            payload = self.payload()
        except (ValueError, KeyError):
            self.creation_failed("请检查参数或等待项目初始化")
            return
        self.problem.clear()
        self.problem.hide()
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
        from .diagnostics import event
        event('generation.preflight-ready')
        calls = len([t for t in payload["texts"] if t.strip()]) * len(payload["voice_ids"]) * payload["count"]
        if cloud and not self.confirm(f"即将发起最多 {calls} 次云合成调用。最终费用以厂商账单为准，是否继续？"):
            self.submitting = False
            self.update_summary()
            return
        # The redundant page checkbox is gone. Acknowledgement belongs to this
        # exact preflighted batch, and is set only after its cloud confirmation;
        # cancellation never submits a task or silently authorizes a later one.
        payload["cost_acknowledged"] = cloud
        event('generation.submit')
        # Snapshot the form before preflight. Editing while a response is in
        # flight must not silently change the billable batch being confirmed.
        self.request("/api/tasks/generate-batch", "POST", json=payload, callback=self.created, error=self.creation_failed)

    def created(self, result):
        from .diagnostics import event
        event('generation.created')
        self.submitting = False
        self.start_button.setEnabled(True)
        self.notify("合成任务已创建")
        self.update_summary()

    def creation_failed(self, error):
        from .diagnostics import event
        event('generation.failed')
        self.submitting = False
        self.problem.setText(str(error))
        self.problem.show()
        self.configure_button.setVisible("配置" in str(error) or "API Key" in str(error))
        self.shell.set_status(str(error), True)
        self.update_summary()
