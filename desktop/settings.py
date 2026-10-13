"""Compact service setup and immediate, user-directed data relocation."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLineEdit, QTabWidget,
                               QWidget, QFileDialog, QLabel, QMessageBox)
from .base import Page
from .widgets import field, button, combo, Card, Disclosure
from .services import BASE_URLS, settings_id

FIELDS = {
    "volcengine_v1": [("app_id", "App ID", False), ("access_token", "Access Token", True), ("endpoint", "Base URL", False)],
    "volcengine_v2": [("api_key", "API Key", True), ("v3_endpoint", "Base URL", False)],
    "xiaomi": [("api_key", "API Key", True), ("endpoint", "Base URL", False)],
    "aliyun": [("api_key", "API Key", True), ("endpoint", "Base URL", False)],
}
EXTRA_FIELDS = {
    "volcengine_v1": [("cluster", "Cluster", False), ("access_key_id", "Access Key ID (AK)", False), ("access_key_secret", "Secret Access Key (SK)", True), ("catalog_endpoint", "服务地址", False)],
    "volcengine_v2": [("access_key_id", "Access Key ID (AK)", False), ("access_key_secret", "Secret Access Key (SK)", True), ("catalog_endpoint", "服务地址", False)],
    "aliyun": [("workspace_id", "Workspace ID", False), ("region", "地域", False), ("customization_endpoint", "音色 Base URL", False)],
    "xiaomi": [],
}


class SettingsPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "设置")
        self.fields, self.units, self.state, self.controls, self.dirty = {}, {}, {}, {}, set()
        storage = Card("数据目录")
        directory_row = QHBoxLayout()
        self.location = QLineEdit(str(shell.runtime.data_directory))
        self.location.setReadOnly(True)
        self.location.setToolTip(str(shell.runtime.data_directory))
        directory_row.addWidget(self.location, 1)
        directory_row.addWidget(button("更换目录", self.choose_directory, True))
        directory_row.addWidget(button("打开", shell.open_data_directory))
        storage.body.addLayout(directory_row)
        self.layout.addWidget(storage)
        self.tabs = QTabWidget()
        self.service_order = ["volcengine_v1", "volcengine_v2", "xiaomi", "aliyun"]
        for service, title in zip(self.service_order, ["豆包 1.0", "豆包 2.0", "小米 MiMo", "阿里云"]):
            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(20, 20, 20, 18)
            layout.setSpacing(14)
            controls = {}
            for key, label, secret in FIELDS[service]:
                widget = self.make_field(service, key, secret)
                controls[key] = widget
                tip = self.tip(service, key, secret)
                layout.addWidget(field(label, widget, tip))
            advanced = Disclosure("高级设置")
            for key, label, secret in EXTRA_FIELDS[service]:
                widget = self.make_field(service, key, secret)
                controls[key] = widget
                advanced.body.addWidget(field(label, widget, self.tip(service, key, secret)))
            price_row = QHBoxLayout()
            self.units[service] = combo([("不估算", ""), ("每次调用", "per_call"), ("每千字符", "per_1k_chars")])
            rate, currency = QLineEdit(), QLineEdit()
            rate.setPlaceholderText("单价")
            currency.setPlaceholderText("如 CNY")
            rate.textEdited.connect(lambda text, s=service: self.dirty.add(s))
            currency.textEdited.connect(lambda text, s=service: self.dirty.add(s))
            self.units[service].activated.connect(lambda index, s=service: self.dirty.add(s))
            controls.update(pricing_rate=rate, pricing_currency=currency)
            for label, widget in [("费用预估", self.units[service]), ("单价", rate), ("币种", currency)]:
                price_row.addWidget(field(label, widget, "仅用于预估，实际费用以厂商账单为准。" if label == "费用预估" else ""))
            advanced.body.addLayout(price_row)
            layout.addWidget(advanced)
            actions = QHBoxLayout()
            self.controls[service] = [button("保存配置", lambda checked=False, s=service: self.save(s), True),
                button("检查配置", lambda checked=False, s=service: self.validate(s)),
                button("获取音色", lambda checked=False, s=service: self.fetch_catalog(s)),
                button("清空", lambda checked=False, s=service: self.clear(s))]
            for control in self.controls[service]:
                actions.addWidget(control)
            actions.addStretch()
            layout.addLayout(actions)
            self.state[service] = QLabel()
            self.state[service].setTextFormat(Qt.TextFormat.PlainText)
            self.state[service].setWordWrap(True)
            self.state[service].setObjectName("formStatus")
            layout.addWidget(self.state[service])
            layout.addStretch()
            self.fields[service] = controls
            self.tabs.addTab(content, title)
        self.layout.addWidget(self.tabs)
        self.layout.addStretch()

    def make_field(self, service, key, secret):
        widget = QLineEdit()
        if secret:
            widget.setEchoMode(QLineEdit.EchoMode.Password)
        if key in {"endpoint", "v3_endpoint"}:
            widget.setText(BASE_URLS[service])
        if key == "catalog_endpoint":
            widget.setPlaceholderText("https://open.volcengineapi.com")
        widget.textEdited.connect(lambda text, s=service: self.dirty.add(s))
        return widget

    def tip(self, service, key, secret):
        # ListSpeakers is a signed Volcengine OpenAPI request, independent of
        # the speech synthesis Base URL and its Token/API Key. Use the vendor's
        # service-address and AK/SK names so users can map this form to its docs;
        # leaving the service address blank uses the client's official default.
        if key == "catalog_endpoint":
            return "火山引擎 OpenAPI 服务地址，默认 https://open.volcengineapi.com；用于调用大模型音色列表接口 ListSpeakers，留空使用默认地址。"
        if key in {"access_key_id", "access_key_secret"}:
            purpose = "火山引擎 API 访问密钥，用于签名调用大模型音色列表接口 ListSpeakers；在控制台“API访问密钥”中获取，与语音合成凭据独立。"
            return purpose + ("已保存的 SK 不回显，留空保留，由 Windows 当前用户加密保护。" if secret else "未填写 AK/SK 时仅加载内置音色列表。")
        if secret:
            return "密钥由 Windows 当前用户加密保护；已保存的密钥不回显，留空保留。" + ("豆包 2.0 使用 API Key，不使用 1.0 的 Access Token。" if service == "volcengine_v2" and key == "api_key" else "")
        if key in {"endpoint", "v3_endpoint", "customization_endpoint"}:
            return "基础地址，程序自动补齐接口路径；默认使用官方 HTTPS 地址。也可填写完整请求地址。"
        return ""

    def select_service(self, service):
        if service in self.service_order:
            self.tabs.setCurrentIndex(self.service_order.index(service))

    def refresh(self):
        self.request("/api/settings/providers", callback=self.show_settings)

    def show_settings(self, result):
        rows = {item["id"]: item for item in result["providers"]}
        for service in self.service_order:
            if service not in self.dirty:
                self.show_provider(service, rows[settings_id(service)])

    def show_provider(self, service, provider):
        for key, widget in self.fields[service].items():
            value = provider.get(key, "")
            if isinstance(value, dict):
                widget.clear()
                widget.setPlaceholderText("已保存 · 留空保留" if value.get("configured") else "请输入密钥")
            else:
                if key in {"endpoint", "v3_endpoint"}:
                    if not value and service == "aliyun" and provider.get("workspace_id"):
                        value = "https://" + provider["workspace_id"] + ".cn-beijing.maas.aliyuncs.com/api/v1"
                    value = value or BASE_URLS[service]
                    for route in ("/chat/completions", "/api/v3/tts/unidirectional", "/api/v1/tts", "/services/audio/tts/SpeechSynthesizer"):
                        if str(value).endswith(route):
                            value = str(value)[:-len(route)]
                widget.setText(str(value))
        self.units[service].setCurrentIndex(max(0, self.units[service].findData(provider.get("pricing_unit"))))
        self.state[service].setText("已配置" if provider.get("synthesis_configured") else "尚未配置")

    def form_payload(self, service):
        payload = {}
        secret_names = {key for key, _, secret in FIELDS[service] + EXTRA_FIELDS[service] if secret}
        for key, widget in self.fields[service].items():
            value = widget.text().strip()
            if key in secret_names and not value:
                continue
            payload[key] = value or None
        payload["pricing_unit"] = self.units[service].currentData() or None
        return payload

    def busy(self, service, enabled):
        for control in self.controls[service]:
            control.setEnabled(not enabled)

    def save(self, service):
        self.busy(service, True)
        self.request(f"/api/settings/providers/{settings_id(service)}", "PUT", json=self.form_payload(service),
                     callback=lambda result: self.saved(service, result), error=lambda error: self.failed(service, error))

    def saved(self, service, result):
        self.dirty.discard(service)
        self.show_provider(service, result)
        self.fetch_catalog(service, saved=True)

    def failed(self, service, error):
        self.busy(service, False)
        self.state[service].setText(str(error))
        self.state[service].setStyleSheet("color:#b54050;")
        self.shell.set_status(str(error), True)

    def validate(self, service):
        self.busy(service, True)
        self.request(f"/api/settings/providers/{settings_id(service)}/test", "POST", json=self.form_payload(service),
                     callback=lambda result: (self.busy(service, False), self.state[service].setText("配置格式通过 · 未发起云调用"), self.notify("配置格式通过")),
                     error=lambda error: self.failed(service, error))

    def fetch_catalog(self, service, saved=False):
        self.busy(service, True)
        self.state[service].setText("已保存，正在获取音色…" if saved else "正在获取音色…")
        self.request("/api/voices/refresh", "POST", json={"provider": service},
                     callback=lambda result: self.catalog_ready(service, result),
                     error=lambda error: self.catalog_failed(service, error, saved))

    def catalog_ready(self, service, result):
        self.busy(service, False)
        self.state[service].setStyleSheet("")
        message = f"已获取 {result['count']} 个音色"
        if result.get("warnings"):
            message += "；" + "；".join(result["warnings"])
        self.state[service].setText(message)
        self.notify(message)
        self.shell.pages[0].refresh()

    def catalog_failed(self, service, error, saved):
        self.failed(service, ("配置已保存；" if saved else "") + "音色获取失败：" + str(error))

    def clear(self, service):
        if self.confirm("清空当前服务配置与凭据？"):
            self.dirty.discard(service)
            self.mutate(f"/api/settings/providers/{settings_id(service)}", "DELETE")

    def choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择数据目录", str(self.shell.runtime.data_directory))
        if directory:
            self.use_directory(Path(directory))

    def use_directory(self, directory, mode=None):
        directory = directory.resolve()
        if directory == self.shell.runtime.data_directory:
            return
        if mode is None:
            if (directory / "tts_dataset.sqlite3").is_file():
                mode = "existing"
            elif any(directory.iterdir()):
                self.shell.error("请选择空目录或已有数据集目录，现有文件不会被覆盖")
                return
            else:
                dialog = QMessageBox(self)
                dialog.setWindowTitle("使用新目录")
                dialog.setText("新目录为空，如何使用？")
                move = dialog.addButton("迁移现有数据", QMessageBox.ButtonRole.AcceptRole)
                empty = dialog.addButton("使用空目录", QMessageBox.ButtonRole.ActionRole)
                dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                dialog.exec()
                if dialog.clickedButton() is move:
                    mode = "move"
                elif dialog.clickedButton() is empty:
                    mode = "empty"
                else:
                    return
        self.shell.switch_directory(directory, mode)
