"""Settings dialog: tabbed (fits laptop screens) — providers, keys, billing."""

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTabWidget,
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
        self.setMinimumWidth(440)
        self.setStyleSheet(
            f"""
            QDialog {{ background: {theme.PAGE}; }}
            QLabel, QCheckBox {{
                color: {theme.TEXT_SECONDARY};
                font-family: "{theme.FONT_FAMILY}";
            }}
            QTabWidget::pane {{
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                top: -1px;
            }}
            QTabBar::tab {{
                background: {theme.PAGE};
                color: {theme.TEXT_SECONDARY};
                border: 1px solid {theme.BORDER};
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 5px 12px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {theme.SURFACE};
                color: {theme.TEXT_PRIMARY};
            }}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
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

        tabs = QTabWidget()
        tabs.addTab(self._providers_tab(cfg), "Провайдеры")
        tabs.addTab(self._keys_tab(cfg), "Ключи")
        tabs.addTab(self._billing_tab(cfg), "Подписки и бюджет")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # ---- tabs -----------------------------------------------------------
    def _providers_tab(self, cfg: dict) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        enabled = set(cfg.get("providers") or [])
        self._checks: dict[str, QCheckBox] = {}
        for name in PROVIDERS:
            check = QCheckBox(f"{name} — {PROVIDER_HINTS.get(name, '')}")
            check.setChecked(name in enabled)
            self._checks[name] = check
            layout.addWidget(check)
        note = QLabel(
            "Claude, Codex, Cursor и Google используют токены установленных "
            "приложений — ключи для них не нужны."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        layout.addStretch()
        layout.addWidget(note)
        return page

    def _keys_tab(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
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
        self.tavily_key = QLineEdit(cfg.get("tavily_api_key") or "")
        self.tavily_key.setEchoMode(QLineEdit.Password)
        self.tavily_key.setPlaceholderText("tvly-…")
        form.addRow("Z.ai API-ключ:", self.zai_key)
        form.addRow("Z.ai регион:", self.zai_region)
        form.addRow("OpenCode cookie:", self.opencode_cookie)
        form.addRow("OpenCode workspace:", self.opencode_workspace)
        form.addRow("OpenAI Admin-ключ:", self.openai_key)
        form.addRow("Tavily API-ключ:", self.tavily_key)
        return page

    def _billing_tab(self, cfg: dict) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        def renewal_row(prefix: str) -> QWidget:
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
            if current in ("month", "quarter", "year"):
                period.setCurrentIndex(["month", "quarter", "year"].index(current))
            setattr(self, f"{prefix}_renewal_date", date_edit)
            setattr(self, f"{prefix}_renewal_period", period)
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(date_edit, stretch=1)
            row.addWidget(period)
            return box

        form.addRow("Claude: продление:", renewal_row("claude"))
        form.addRow("Cursor: продление:", renewal_row("cursor"))
        form.addRow("Z.ai: продление:", renewal_row("zai"))
        form.addRow("Tavily: продление:", renewal_row("tavily"))

        self.openai_budget = QSpinBox()
        self.openai_budget.setRange(0, 100000)
        self.openai_budget.setPrefix("$ ")
        self.openai_budget.setSpecialValueText("не задан (только расход)")
        self.openai_budget.setValue(int(cfg.get("openai_budget_usd") or 0))
        form.addRow("OpenAI бюджет/мес:", self.openai_budget)

        self.openai_balance = QDoubleSpinBox()
        self.openai_balance.setRange(0, 1000000)
        self.openai_balance.setDecimals(2)
        self.openai_balance.setPrefix("$ ")
        self.openai_balance.setSpecialValueText("не задан")
        self.openai_balance.setValue(float(cfg.get("openai_balance_usd") or 0))
        self.openai_balance_date_edit = QLineEdit(cfg.get("openai_balance_date") or "")
        self.openai_balance_date_edit.setPlaceholderText("дд.мм.гггг")
        balance_box = QWidget()
        balance_row = QHBoxLayout(balance_box)
        balance_row.setContentsMargins(0, 0, 0, 0)
        balance_row.addWidget(self.openai_balance, stretch=1)
        balance_row.addWidget(QLabel("на дату:"))
        balance_row.addWidget(self.openai_balance_date_edit)
        balance_box.setToolTip(
            "Остаток со страницы platform.openai.com/settings/organization/billing "
            "на указанную дату; дальше приложение само вычитает расходы Costs API."
        )
        form.addRow("OpenAI остаток:", balance_box)

        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setSuffix(" мин")
        self.interval.setValue(max(1, int(cfg.get("refresh_seconds", 300)) // 60))
        form.addRow("Интервал опроса:", self.interval)
        return page

    # ---- result ---------------------------------------------------------
    def apply_to(self, cfg: dict) -> dict:
        cfg["providers"] = [n for n, c in self._checks.items() if c.isChecked()]
        cfg["zai_api_key"] = self.zai_key.text().strip()
        cfg["zai_region"] = self.zai_region.currentData()
        cfg["opencode_cookie"] = self.opencode_cookie.text().strip()
        cfg["opencode_workspace"] = self.opencode_workspace.text().strip()
        cfg["openai_admin_key"] = self.openai_key.text().strip()
        cfg["openai_budget_usd"] = self.openai_budget.value()
        cfg["openai_balance_usd"] = self.openai_balance.value()
        cfg["openai_balance_date"] = self.openai_balance_date_edit.text().strip()
        cfg["tavily_api_key"] = self.tavily_key.text().strip()
        for prefix in ("claude", "cursor", "zai", "tavily"):
            cfg[f"{prefix}_renewal_date"] = getattr(self, f"{prefix}_renewal_date").text().strip()
            cfg[f"{prefix}_renewal_period"] = getattr(self, f"{prefix}_renewal_period").currentData()
        cfg["refresh_seconds"] = self.interval.value() * 60
        return cfg
