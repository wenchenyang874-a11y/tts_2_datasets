"""Masked provider forms and explicit data-management actions."""
from pathlib import Path
from PySide6.QtWidgets import (QGroupBox, QVBoxLayout, QHBoxLayout, QLineEdit, QTabWidget,
                               QWidget, QFileDialog, QLabel)
from .base import Page
from .widgets import field, button, combo

FIELDS = {
    "xiaomi": [("api_key", "API Key", True), ("endpoint", "合成 Endpoint", False)],
    "volcengine": [("app_id", "App ID", False), ("access_token", "Access Token", True),
                    ("api_key", "2.0 API Key", True), ("v3_endpoint", "2.0 Endpoint", False),
                    ("cluster", "Cluster", False), ("access_key_id", "Access Key ID", False),
                    ("access_key_secret", "Access Key Secret", True), ("endpoint", "合成 Endpoint", False),
                    ("catalog_endpoint", "音色 Endpoint", False)],
    "aliyun": [("api_key", "API Key", True), ("workspace_id", "Workspace ID", False),
               ("region", "地域", False), ("endpoint", "合成 Endpoint", False),
               ("customization_endpoint", "音色 Endpoint", False)]}


class SettingsPage(Page):
    def __init__(self, shell):
        super().__init__(shell, "设置")
        self.fields = {}
        self.units = {}
        self.state = {}
        self.controls = {}
        tabs = QTabWidget()
        for provider, title in [("volcengine", "火山引擎"), ("aliyun", "阿里云"), ("xiaomi", "小米 MiMo")]:
            content = QWidget()
            layout = QVBoxLayout(content)
            controls = {}
            for key, label, secret in FIELDS[provider]:
                widget = QLineEdit()
                if secret:
                    widget.setEchoMode(QLineEdit.EchoMode.Password)
                tip = "凭据由当前 Windows 用户的 DPAPI 保护；留空保留已保存的密钥，不回显明文。" if secret else (
                    "默认仅允许供应商官方 HTTPS 主机。留空使用适配器默认值。" if "endpoint" in key else "")
                if provider == "volcengine" and key == "api_key":
                    tip += " 豆包 2.0 使用控制台 API Key 管理中的密钥，与 1.0 Access Token 和目录查询 AK/SK 不同。"
                if provider == "xiaomi" and key == "endpoint":
                    tip += " 可填写官方 https://api.xiaomimimo.com/v1 或完整 /v1/chat/completions 地址。"
                layout.addWidget(field(label, widget, tip))
                controls[key] = widget
            prices = QHBoxLayout()
            unit = combo([("未配置", ""), ("每次调用", "per_call"), ("每千字符", "per_1k_chars")])
            self.units[provider] = unit
            rate = QLineEdit()
            currency = QLineEdit()
            controls.update(pricing_rate=rate, pricing_currency=currency)
            prices.addWidget(field("计费单位", unit, "仅用于预估费用；不能替代供应商账单、配额和预算告警。"))
            prices.addWidget(field("单价", rate))
            prices.addWidget(field("币种", currency))
            layout.addLayout(prices)
            actions = QHBoxLayout()
            self.controls[provider] = [button("保存", lambda checked=False, p=provider: self.save(p), True),
                button("验证格式", lambda checked=False, p=provider: self.validate(p)),
                button("获取音色列表", lambda checked=False, p=provider: self.fetch_catalog(p)),
                button("清空配置", lambda checked=False, p=provider: self.clear(p))]
            for control in self.controls[provider]:
                actions.addWidget(control)
            actions.addStretch()
            layout.addLayout(actions)
            self.state[provider] = QLabel()
            layout.addWidget(self.state[provider])
            self.fields[provider] = controls
            tabs.addTab(content, title)
        self.layout.addWidget(tabs)
        self.location = QLabel(str(shell.runtime.data_directory))
        self.location.setWordWrap(True)
        self.layout.addWidget(field("当前数据目录", self.location, "数据库、音频和加密凭据位于用户数据目录，与安装包分离。DPAPI 凭据只能由原 Windows 用户读取，跨用户恢复需重新配置。"))
        self.data_directory = QLineEdit(str(shell.runtime.data_directory))
        self.layout.addWidget(field("下次启动目录", self.data_directory, "选择或输入目录并保存，下次启动生效。旧数据保留原位置，不自动搬迁；命令行 --data-dir 优先于此设置。"))
        self.actions(("选择目录", self.choose_directory, False), ("保存目录", self.save_directory, True),
                     ("打开当前目录", shell.open_data_directory, False))
        self.layout.addStretch()

    def refresh(self):
        self.request("/api/settings/providers", callback=self.show_settings)

    def show_settings(self, result):
        for provider in result["providers"]:
            self.show_provider(provider)

    def show_provider(self, provider):
        name = provider["id"]
        if name not in self.fields:
            return
        for key, widget in self.fields[name].items():
            value = provider.get(key, "")
            if isinstance(value, dict):
                widget.clear()
                widget.setPlaceholderText("已保存，留空保留" if value.get("configured") else "尚未配置")
            else:
                widget.setText(str(value))
        self.units[name].setCurrentIndex(max(0, self.units[name].findData(provider.get("pricing_unit"))))
        self.state[name].setText("合成配置已填写" if provider.get("synthesis_configured") else "合成配置未完整填写")

    def form_payload(self, provider):
        payload = {}
        secret_names = {key for key, _, secret in FIELDS[provider] if secret}
        for key, widget in self.fields[provider].items():
            value = widget.text().strip()
            if key in secret_names and not value:
                continue
            payload[key] = value if value else None
        payload["pricing_unit"] = self.units[provider].currentData() or None
        return payload

    def busy(self, provider, enabled):
        for control in self.controls[provider]:
            control.setEnabled(not enabled)

    def save(self, provider):
        payload = self.form_payload(provider)
        self.busy(provider, True)
        self.request(f"/api/settings/providers/{provider}", "PUT", json=payload,
                     callback=lambda result: self.saved(provider, result), error=lambda error: self.failed(provider, error))

    def saved(self, provider, result):
        # Render only the saved provider, so another tab's unsaved edits are
        # not lost. A failed catalog lookup must not roll back valid settings
        # or claim the save failed: surface those two results separately.
        self.show_provider(result)
        self.fetch_catalog(provider, saved=True)

    def failed(self, provider, error):
        self.busy(provider, False)
        self.shell.error(error)

    def validate(self, provider):
        self.busy(provider, True)
        self.request(f"/api/settings/providers/{provider}/test", "POST", json=self.form_payload(provider),
                     callback=lambda result: (self.busy(provider, False), self.notify("当前填写内容格式通过；未发起云调用")),
                     error=lambda error: self.failed(provider, error))

    def fetch_catalog(self, provider, saved=False):
        self.busy(provider, True)
        self.state[provider].setText("配置已保存，正在获取音色…" if saved else "正在获取音色…")
        self.request("/api/voices/refresh", "POST", json={"provider": provider},
                     callback=lambda result: self.catalog_ready(provider, result),
                     error=lambda error: self.catalog_failed(provider, error, saved))

    def catalog_ready(self, provider, result):
        self.busy(provider, False)
        suffix = "；" + "；".join(result["warnings"]) if result.get("warnings") else ""
        description = "预置音色" if provider == "xiaomi" else "音色"
        self.state[provider].setText(f"已获取 {result['count']} 个{description}" + suffix)
        self.notify(self.state[provider].text())
        self.shell.pages[0].refresh_voices()

    def catalog_failed(self, provider, error, saved):
        self.busy(provider, False)
        message = ("配置已保存；" if saved else "") + "音色获取失败：" + str(error)
        self.state[provider].setText(message)
        self.shell.set_status(message, True)

    def clear(self, provider):
        if self.confirm("清空该供应商配置与凭据？"):
            self.mutate(f"/api/settings/providers/{provider}", "DELETE")

    def choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择数据目录", self.data_directory.text())
        if directory:
            self.data_directory.setText(directory)

    def save_directory(self):
        value = self.data_directory.text().strip()
        if not value:
            self.shell.error("请输入或选择数据目录")
            return
        if self.shell.preferences is None:
            self.shell.error("当前启动方式未配置桌面偏好存储")
            return
        self.shell.submit(lambda: self.shell.preferences.save_data_directory(value),
                          lambda path: (self.data_directory.setText(str(path)), self.notify("数据目录已保存，下次启动生效")))
