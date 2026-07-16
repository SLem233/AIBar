"""Settings dialog: providers, credentials, refresh interval."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
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
        def renewal_row(prefix: str) -> tuple[QWidget, QLineEdit, QComboBox]:
            date_edit = QLineEdit(cfg.get(f"{prefix}_renewal_date") or "")
            date_edit.setPlaceholderText("дд.мм.гггг")
            date_edit.setToolTip(
                "Дата ближайшего продления — API её не отдаёт. "
                "Прошедшая дата сдвигается на период автоматически."
            )
            period = QComboBox()
            for value, label in (("month", "месяц"), ("quarter", "квартал"), ("year", "год")):
                period.addItem(label, value)
            current = cfg.get(f"{prefix}_renewal_period") or "month"
            period.setCurrentIndex(max(0, ["month", "quarter", "year"].index(current) if current in ("month", "quarter", "year") else 0))
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(date_edit, stretch=1)
            row.addWidget(period)
            return box, date_edit, period

        claude_box, self.claude_renewal_date, self.claude_renewal_period = renewal_row("claude")
        zai_box, self.zai_renewal_date, self.zai_renewal_period = renewal_row("zai")
        tavily_box, self.tavily_renewal_date, self.tavily_renewal_period = renewal_row("tavily")
        self._renewal_rows = {"claude": claude_box, "zai": zai_box, "tavily": tavily_box}
        keys_layout.addRow("Z.ai API-ключ:", self.zai_key)
        keys_layout.addRow("Z.ai регион:", self.zai_region)
        keys_layout.addRow("OpenCode cookie:", self.opencode_cookie)
        keys_layout.addRow("OpenCode workspace:", self.opencode_workspace)
        keys_layout.addRow("OpenAI Admin-ключ:", self.openai_key)
        keys_layout.addRow("OpenAI бюджет/мес:", self.openai_budget)
        keys_layout.addRow("Tavily API-ключ:", self.tavily_key)
        keys_layout.addRow("Claude: продление:", self._renewal_rows["claude"])
        keys_layout.addRow("Z.ai: продление:", self._renewal_rows["zai"])
        keys_layout.addRow("Tavily: продление:", self._renewal_rows["tavily"])

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
        cfg["claude_renewal_date"] = self.claude_renewal_date.text().strip()
        cfg["claude_renewal_period"] = self.claude_renewal_period.currentData()
        cfg["zai_renewal_date"] = self.zai_renewal_date.text().strip()
        cfg["zai_renewal_period"] = self.zai_renewal_period.currentData()
        cfg["tavily_renewal_date"] = self.tavily_renewal_date.text().strip()
        cfg["tavily_renewal_period"] = self.tavily_renewal_period.currentData()
        cfg["refresh_seconds"] = self.interval.value() * 60
        return cfg
