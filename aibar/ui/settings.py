"""Settings dialog: providers, credentials, refresh interval."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from .. import theme
from ..providers import PROVIDER_HINTS, PROVIDERS

ZAI_REGIONS = [("global", "Global (api.z.ai)"), ("bigmodel-cn", "BigModel CN")]


class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AIBar — настройки")
        self.setMinimumWidth(430)
        self.setStyleSheet(
            f"""
            QDialog {{ background: {theme.PAGE}; }}
            QLabel, QCheckBox, QGroupBox {{
                color: {theme.TEXT_SECONDARY};
                font-family: "{theme.FONT_FAMILY}";
            }}
            QGroupBox {{
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 6px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                color: {theme.TEXT_PRIMARY};
            }}
            QLineEdit, QSpinBox, QComboBox {{
                background: {theme.SURFACE};
                color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER};
                border-radius: 6px;
                padding: 4px 8px;
            }}
            QPushButton {{
                background: {theme.SURFACE};
                color: {theme.TEXT_PRIMARY};
                border: 1px solid {theme.BORDER};
                border-radius: 6px;
                padding: 5px 14px;
            }}
            QPushButton:hover {{ border-color: {theme.TEXT_MUTED}; }}
            """
        )

        enabled = set(cfg.get("providers") or [])
        self._checks: dict[str, QCheckBox] = {}
        providers_box = QGroupBox("Провайдеры")
        providers_layout = QVBoxLayout(providers_box)
        for name in PROVIDERS:
            check = QCheckBox(f"{name} — {PROVIDER_HINTS.get(name, '')}")
            check.setChecked(name in enabled)
            self._checks[name] = check
            providers_layout.addWidget(check)

        keys_box = QGroupBox("Ключи доступа")
        keys_layout = QFormLayout(keys_box)
        self.zai_key = QLineEdit(cfg.get("zai_api_key") or "")
        self.zai_key.setEchoMode(QLineEdit.Password)
        self.zai_key.setPlaceholderText("ключ с z.ai / bigmodel.cn (coding plan)")
        self.zai_region = QComboBox()
        for value, label in ZAI_REGIONS:
            self.zai_region.addItem(label, value)
        self.zai_region.setCurrentIndex(
            max(0, [v for v, _ in ZAI_REGIONS].index(cfg.get("zai_region", "global")))
        )
        self.opencode_cookie = QLineEdit(cfg.get("opencode_cookie") or "")
        self.opencode_cookie.setEchoMode(QLineEdit.Password)
        self.opencode_cookie.setPlaceholderText("Cookie: auth=… (DevTools на opencode.ai)")
        self.opencode_workspace = QLineEdit(cfg.get("opencode_workspace") or "")
        self.opencode_workspace.setPlaceholderText("wrk_… (пусто = автоматически)")
        self.openai_key = QLineEdit(cfg.get("openai_admin_key") or "")
        self.openai_key.setEchoMode(QLineEdit.Password)
        self.openai_key.setPlaceholderText("sk-admin-… (Settings → Admin keys)")
        self.openai_budget = QSpinBox()
        self.openai_budget.setRange(0, 100000)
        self.openai_budget.setPrefix("$ ")
        self.openai_budget.setSpecialValueText("не задан (только расход)")
        self.openai_budget.setValue(int(cfg.get("openai_budget_usd") or 0))
        self.tavily_key = QLineEdit(cfg.get("tavily_api_key") or "")
        self.tavily_key.setEchoMode(QLineEdit.Password)
        self.tavily_key.setPlaceholderText("tvly-…")
        self.claude_billing_day = QSpinBox()
        self.claude_billing_day.setRange(0, 31)
        self.claude_billing_day.setSpecialValueText("не задан")
        self.claude_billing_day.setToolTip(
            "День месяца, когда списывается оплата Claude — API эту дату не отдаёт"
        )
        self.claude_billing_day.setValue(int(cfg.get("claude_billing_day") or 0))
        keys_layout.addRow("Z.ai API-ключ:", self.zai_key)
        keys_layout.addRow("Z.ai регион:", self.zai_region)
        keys_layout.addRow("OpenCode cookie:", self.opencode_cookie)
        keys_layout.addRow("OpenCode workspace:", self.opencode_workspace)
        keys_layout.addRow("OpenAI Admin-ключ:", self.openai_key)
        keys_layout.addRow("OpenAI бюджет/мес:", self.openai_budget)
        keys_layout.addRow("Tavily API-ключ:", self.tavily_key)
        keys_layout.addRow("Claude: день оплаты:", self.claude_billing_day)

        misc_box = QGroupBox("Обновление")
        misc_layout = QFormLayout(misc_box)
        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setSuffix(" мин")
        self.interval.setValue(max(1, int(cfg.get("refresh_seconds", 300)) // 60))
        misc_layout.addRow("Интервал опроса:", self.interval)

        note = QLabel(
            "Claude, Codex и Cursor используют токены установленных приложений — "
            "ключи для них не нужны."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(providers_box)
        layout.addWidget(keys_box)
        layout.addWidget(misc_box)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def apply_to(self, cfg: dict) -> dict:
        cfg["providers"] = [n for n, c in self._checks.items() if c.isChecked()]
        cfg["zai_api_key"] = self.zai_key.text().strip()
        cfg["zai_region"] = self.zai_region.currentData()
        cfg["opencode_cookie"] = self.opencode_cookie.text().strip()
        cfg["opencode_workspace"] = self.opencode_workspace.text().strip()
        cfg["openai_admin_key"] = self.openai_key.text().strip()
        cfg["openai_budget_usd"] = self.openai_budget.value()
        cfg["tavily_api_key"] = self.tavily_key.text().strip()
        cfg["claude_billing_day"] = self.claude_billing_day.value()
        cfg["refresh_seconds"] = self.interval.value() * 60
        return cfg
