"""Masked provider forms and explicit data-management actions."""
from pathlib import Path
from PySide6.QtWidgets import (QGroupBox, QVBoxLayout, QHBoxLayout, QLineEdit, QTabWidget,
                               QWidget, QFileDialog, QLabel)
from .base import Page
from .widgets import field, button, combo
from .data_transfer import copy_dataset

FIELDS = {
    "volcengine": [("app_id", "App ID", False), ("access_token", "Access Token", True),
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
        tabs = QTabWidget()
        for provider, title in [("volcengine", "火山引擎"), ("aliyun", "阿里云")]:
            content = QWidget()
            layout = QVBoxLayout(content)
            controls = {}
            for key, label, secret in FIELDS[provider]:
                widget = QLineEdit()
                if secret:
                    widget.setEchoMode(QLineEdit.EchoMode.Password)
                tip = "凭据由当前 Windows 用户的 DPAPI 保护；留空保留已保存的密钥，不回显明文。" if secret else (
                    "默认仅允许供应商官方 HTTPS 主机。留空使用适配器默认值。" if "endpoint" in key else "")
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
            actions.addWidget(button("保存", lambda checked=False, p=provider: self.save(p), True))
            actions.addWidget(button("验证格式", lambda checked=False, p=provider: self.validate(p)))
            actions.addWidget(button("清空配置", lambda checked=False, p=provider: self.clear(p)))
            actions.addStretch()
            layout.addLayout(actions)
            self.state[provider] = QLabel()
            layout.addWidget(self.state[provider])
            self.fields[provider] = controls
            tabs.addTab(content, title)
        self.layout.addWidget(tabs)
        self.location = QLabel(str(shell.runtime.data_directory))
        self.location.setWordWrap(True)
        self.layout.addWidget(field("数据目录", self.location, "数据库、音频和加密凭据位于用户数据目录，与安装包分离。DPAPI 凭据只能由原 Windows 用户读取，跨用户恢复需重新配置。"))
        self.actions(("打开数据目录", shell.open_data_directory, False),
                     ("导入 Web 备份到新目录", self.import_data, False))
        self.layout.addStretch()

    def refresh(self):
        self.request("/api/settings/providers", callback=self.show_settings)

    def show_settings(self, result):
        for provider in result["providers"]:
            name = provider["id"]
            for key, widget in self.fields[name].items():
                value = provider.get(key, "")
                if isinstance(value, dict):
                    widget.clear()
                    widget.setPlaceholderText("已保存，留空保留" if value.get("configured") else "尚未配置")
                else:
                    widget.setText(str(value))
            self.units[name].setCurrentIndex(max(0, self.units[name].findData(provider.get("pricing_unit"))))
            self.state[name].setText("合成配置已填写" if provider.get("synthesis_configured") else "合成配置未完整填写")

    def save(self, provider):
        payload = {}
        secret_names = {key for key, _, secret in FIELDS[provider] if secret}
        for key, widget in self.fields[provider].items():
            value = widget.text().strip()
            if key in secret_names and not value:
                continue
            payload[key] = value if value else None
        payload["pricing_unit"] = self.units[provider].currentData() or None
        self.request(f"/api/settings/providers/{provider}", "PUT", json=payload,
                     callback=lambda result: (self.notify("配置已保存"), self.refresh()))

    def validate(self, provider):
        self.request(f"/api/settings/providers/{provider}/test", "POST",
                     callback=lambda result: self.notify("配置格式通过；尚未验证真实云调用"))

    def clear(self, provider):
        if self.confirm("清空该供应商配置与凭据？"):
            self.mutate(f"/api/settings/providers/{provider}", "DELETE")

    def import_data(self):
        if not self.confirm("请先停止原 Web 服务并备份完整数据目录。将导入到一个空目录，原数据保持不变。继续？"):
            return
        source = QFileDialog.getExistingDirectory(self, "选择停止服务后的 Web 数据备份")
        if not source:
            return
        destination = QFileDialog.getExistingDirectory(self, "选择空的导入目标目录")
        if not destination:
            return
        if Path(destination).resolve() == self.shell.runtime.data_directory:
            self.shell.error("不能导入到正在使用的数据目录")
            return
        self.shell.submit(lambda: copy_dataset(Path(source), Path(destination)),
                          lambda result: self.notify("数据已导入；关闭应用后以 --data-dir 指定新目录启动。跨 Windows 用户的凭据需重新配置。"))
