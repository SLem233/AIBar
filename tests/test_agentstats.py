"""AgentPulse stats page: data export from ledger.db and HTML generation."""

import json
import sqlite3

import pytest

from aibar import agentstats

SESSIONS_DDL = """
CREATE TABLE sessions (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, project TEXT, cwd TEXT,
  git_branch TEXT, first_ts TEXT, last_ts TEXT,
  active_seconds INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  models_json TEXT NOT NULL DEFAULT '{}',
  source_file TEXT, source_size INTEGER, source_mtime REAL,
  time_reliable INTEGER NOT NULL DEFAULT 1,
  closed INTEGER NOT NULL DEFAULT 0,
  parse_errors INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT, title TEXT, task_type TEXT, task_type_manual TEXT,
  PRIMARY KEY (agent, session_id))
"""

DAILY_DDL = """
CREATE TABLE daily_activity (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, date TEXT NOT NULL,
  project TEXT,
  active_seconds INTEGER NOT NULL DEFAULT 0,
  messages INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  time_reliable INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (agent, session_id, date))
"""


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    db = sqlite3.connect(path)
    db.execute(SESSIONS_DDL)
    db.execute(DAILY_DDL)
    db.execute(
        "INSERT INTO sessions (agent, session_id, project, first_ts, last_ts,"
        " active_seconds, input_tokens, output_tokens, models_json, title, task_type)"
        " VALUES ('claude-code', 's1', 'AIBar', '2026-07-20T10:00:00+00:00',"
        " '2026-07-20T11:00:00+00:00', 3600, 100, 200, '{\"claude-opus\": 200}',"
        " 'Правка виджета', 'фича')"
    )
    db.execute(
        "INSERT INTO sessions (agent, session_id, project, first_ts, last_ts,"
        " active_seconds, output_tokens, task_type, task_type_manual, title)"
        " VALUES ('codex', 's2', NULL, '2026-07-21T09:00:00+00:00',"
        " '2026-07-21T09:30:00+00:00', 1800, 50, 'авто', 'ручной',"
        " '</script><b>инъекция</b>')"
    )
    db.execute(
        "INSERT INTO sessions (agent, session_id, first_ts, last_ts, output_tokens)"
        " VALUES ('claude-code', 's1:agent-a1', '2026-07-20T10:05:00+00:00',"
        " '2026-07-20T10:15:00+00:00', 500)"
    )
    db.execute(
        "INSERT INTO daily_activity (agent, session_id, date, active_seconds,"
        " output_tokens) VALUES ('claude-code', 's1', '2026-07-20', 3600, 200)"
    )
    db.commit()
    db.close()
    return path


CARD_MD = """---
project: AIBar
active_time: "4 ч 12 мин"
---

# AIBar

<!-- AUTO-BEGIN: обновляется AgentPulse, не править вручную -->
| Метрика | Значение |
|---|---|
| Период разработки | 15.07.2026 |
<!-- AUTO-END -->

## Описание

Виджет лимитов, см. [[release-process]] и проект [[AgentPulse|пульс]].

| Метрика | Значение |
|---|---|
| Сессий | 5 |
"""


@pytest.fixture
def vault(tmp_path):
    cards = tmp_path / "SL_Wiki" / "sl_projects"
    cards.mkdir(parents=True)
    (cards / "AIBar.md").write_text(CARD_MD, encoding="utf-8")
    return cards


def test_load_data_links_projects_to_existing_cards(ledger, vault):
    data = agentstats.load_data(ledger, cards_dir=vault)
    assert data["cards"] == {"AIBar": "cards/AIBar.html"}


def test_load_data_skips_projects_without_cards(ledger, vault):
    (vault / "AIBar.md").unlink()
    data = agentstats.load_data(ledger, cards_dir=vault)
    assert data["cards"] == {}


def test_load_data_without_cards_dir(ledger, tmp_path):
    data = agentstats.load_data(ledger, cards_dir=tmp_path / "no_vault")
    assert data["cards"] == {}


def test_stats_page_uses_card_links(ledger, vault, tmp_path):
    out = tmp_path / "stats.html"
    agentstats.generate(ledger, out, cards_dir=vault)
    text = out.read_text(encoding="utf-8")
    assert "cards\\/AIBar.html" in text or "cards/AIBar.html" in text
    assert "RAW.cards" in text  # template uses the mapping
    assert "obsidian" not in text


def test_generate_renders_card_pages(ledger, vault, tmp_path):
    out = tmp_path / "stats.html"
    agentstats.generate(ledger, out, cards_dir=vault)
    card = tmp_path / "cards" / "AIBar.html"
    assert card.is_file()
    html = card.read_text(encoding="utf-8")
    assert "<h2" in html and "Описание" in html  # markdown rendered
    assert "<table" in html  # tables extension on
    assert "4 ч 12 мин" in html  # frontmatter shown as meta
    assert "---" not in html.split("<body")[1].split("</body")[0][:200]
    # wiki-links degrade to plain text (alias wins)
    assert "release-process" in html and "[[" not in html
    assert "пульс" in html and "AgentPulse|" not in html
    # back to the dashboard, same-window navigation
    assert 'href="../stats.html"' in html
    # the AUTO block duplicates the frontmatter meta — it must be dropped
    assert "Период разработки" not in html
    assert "AUTO-BEGIN" not in html
    assert "Сессий" in html  # while regular tables stay


def test_card_pages_removed_for_deleted_cards(ledger, vault, tmp_path):
    out = tmp_path / "stats.html"
    agentstats.generate(ledger, out, cards_dir=vault)
    (vault / "AIBar.md").unlink()
    agentstats.generate(ledger, out, cards_dir=vault)
    assert not (tmp_path / "cards" / "AIBar.html").exists()


def test_load_data_exports_sessions_and_daily(ledger):
    data = agentstats.load_data(ledger)
    assert len(data["sessions"]) == 3
    assert len(data["daily"]) == 1
    assert data["source"].endswith("ledger.db")
    s1 = next(s for s in data["sessions"] if s["session_id"] == "s1")
    assert s1["task_type"] == "фича"
    assert s1["models_json"] == '{"claude-opus": 200}'


def test_load_data_prefers_manual_task_type(ledger):
    data = agentstats.load_data(ledger)
    s2 = next(s for s in data["sessions"] if s["session_id"] == "s2")
    assert s2["task_type"] == "ручной"


def test_load_data_missing_db_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        agentstats.load_data(tmp_path / "nope.db")


def test_generate_writes_self_contained_page(ledger, tmp_path):
    out = tmp_path / "stats.html"
    result = agentstats.generate(ledger, out)
    assert result == out
    text = out.read_text(encoding="utf-8")
    assert "__AGENTPULSE_DATA__" not in text  # placeholder replaced
    assert '"sessions"' in text
    # fully offline: no CDN scripts, no runtime data fetching
    assert "cdnjs" not in text and "sql-wasm" not in text
    assert "fetch(" not in text
    # new controls requested for AIBar
    assert "12 месяцев" in text
    assert "по неделям" in text and "по месяцам" in text


def test_generate_escapes_script_breakers(ledger, tmp_path):
    out = tmp_path / "stats.html"
    agentstats.generate(ledger, out)
    text = out.read_text(encoding="utf-8")
    # a </script> inside a session title must not terminate the data script
    assert "</script><b>" not in text


def test_generated_data_roundtrips(ledger, tmp_path):
    out = tmp_path / "stats.html"
    agentstats.generate(ledger, out)
    text = out.read_text(encoding="utf-8")
    start = text.index("const RAW = ") + len("const RAW = ")
    end = text.index(";\n", start)
    data = json.loads(text[start:end].replace("<\\/", "</"))
    assert {s["session_id"] for s in data["sessions"]} == {"s1", "s2", "s1:agent-a1"}
