"""Активность агентов на дашборде: инструменты, компакты, effort, режим одобрения.

Данные приходят из новых полей ledger AgentPulse (`daily_tools`, колонки
`compactions`/`effort`/`approval_mode`). Агрегация проверяется на настоящем JS
страницы через node — как и остальные цифры дашборда.
"""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from aibar import agentstats

NODE = shutil.which("node")
PROBE = Path(__file__).parent / "compute_probe.js"

SCHEMA = """
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
  closed INTEGER NOT NULL DEFAULT 0, parse_errors INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT, title TEXT, task_type TEXT, task_type_manual TEXT,
  compactions INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
  effort TEXT, approval_mode TEXT,
  PRIMARY KEY (agent, session_id));
CREATE TABLE daily_activity (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, date TEXT NOT NULL, project TEXT,
  active_seconds INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  time_reliable INTEGER NOT NULL DEFAULT 1,
  compactions INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent, session_id, date));
CREATE TABLE daily_models (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, date TEXT NOT NULL, model TEXT NOT NULL,
  project TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent, session_id, date, model));
CREATE TABLE daily_tools (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, date TEXT NOT NULL,
  tool TEXT NOT NULL, category TEXT NOT NULL, project TEXT,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent, session_id, date, tool));
"""

# Окно «7 дней» относительно последнего события 25.07 — с 19.07 по 25.07 (МСК).
SESSIONS = [
    # agent, sid, project, first_ts, last_ts, act, effort, approval
    ("claude-code", "A", "P", "2026-07-01T09:00:00+00:00", "2026-07-25T09:00:00+00:00",
     4200, "xhigh", "auto"),
    ("claude-code", "B", "P", "2026-07-24T09:00:00+00:00", "2026-07-24T10:00:00+00:00",
     300, None, "acceptEdits"),
    # субагент: его вызовы инструментов — работа, но сессией он не считается
    ("claude-code", "A:agent-x", "P", "2026-07-25T09:30:00+00:00",
     "2026-07-25T09:40:00+00:00", 0, "xhigh", "auto"),
    # целиком вне окна 7 дней
    ("codex", "D", "Q", "2026-07-05T09:00:00+00:00", "2026-07-05T10:00:00+00:00",
     1200, "low", "never"),
]

DAILY = [
    # agent, sid, date, act, compactions
    ("claude-code", "A", "2026-07-01", 3600, 2),
    ("claude-code", "A", "2026-07-25", 600, 1),
    ("claude-code", "A:agent-x", "2026-07-25", 0, 0),
    ("claude-code", "B", "2026-07-24", 300, 0),
    ("codex", "D", "2026-07-05", 1200, 1),
]

DAILY_TOOLS = [
    # agent, sid, date, tool, category, count
    ("claude-code", "A", "2026-07-01", "Read", "read", 50),      # вне окна 7 дней
    ("claude-code", "A", "2026-07-25", "Bash", "shell", 10),
    ("claude-code", "A", "2026-07-25", "Edit", "edit", 4),
    ("claude-code", "A:agent-x", "2026-07-25", "Read", "read", 3),
    ("codex", "D", "2026-07-05", "exec", "shell", 7),            # вне окна 7 дней
]


def build_db(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO sessions (agent, session_id, project, first_ts, last_ts,"
        " active_seconds, effort, approval_mode) VALUES (?,?,?,?,?,?,?,?)",
        SESSIONS,
    )
    db.executemany(
        "INSERT INTO daily_activity (agent, session_id, date, active_seconds, compactions)"
        " VALUES (?,?,?,?,?)",
        DAILY,
    )
    db.executemany(
        "INSERT INTO daily_tools (agent, session_id, date, tool, category, count)"
        " VALUES (?,?,?,?,?,?)",
        DAILY_TOOLS,
    )
    db.commit()
    db.close()
    return path


# ---- Python-слой: чтение новых полей ----------------------------------------

class TestLoadData:
    def test_daily_tools_loaded(self, tmp_path: Path):
        data = agentstats.load_data(build_db(tmp_path / "ledger.db"))
        assert len(data["daily_tools"]) == len(DAILY_TOOLS)
        row = next(r for r in data["daily_tools"] if r["tool"] == "Bash")
        assert row["category"] == "shell" and row["count"] == 10

    def test_session_activity_fields_loaded(self, tmp_path: Path):
        data = agentstats.load_data(build_db(tmp_path / "ledger.db"))
        a = next(s for s in data["sessions"] if s["session_id"] == "A")
        assert a["effort"] == "xhigh"
        assert a["approval_mode"] == "auto"

    def test_daily_compactions_loaded(self, tmp_path: Path):
        data = agentstats.load_data(build_db(tmp_path / "ledger.db"))
        total = sum(r.get("compactions") or 0 for r in data["daily"])
        assert total == 4

    def test_old_ledger_without_new_columns_still_works(self, tmp_path: Path):
        """Ledger до миграции: страница обязана строиться, просто без активности."""
        db_path = tmp_path / "old.db"
        db = sqlite3.connect(db_path)
        db.executescript(
            "CREATE TABLE sessions (agent TEXT, session_id TEXT, project TEXT, cwd TEXT,"
            " first_ts TEXT, last_ts TEXT, active_seconds INTEGER, input_tokens INTEGER,"
            " output_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER,"
            " models_json TEXT, time_reliable INTEGER, title TEXT, task_type TEXT,"
            " task_type_manual TEXT);"
            "CREATE TABLE daily_activity (agent TEXT, session_id TEXT, date TEXT,"
            " active_seconds INTEGER, input_tokens INTEGER, output_tokens INTEGER,"
            " cache_read_tokens INTEGER, cache_creation_tokens INTEGER, time_reliable INTEGER);"
        )
        db.execute(
            "INSERT INTO sessions VALUES ('claude-code','A','P',NULL,"
            "'2026-07-25T09:00:00+00:00','2026-07-25T10:00:00+00:00',60,1,1,0,0,'{}',1,'t',NULL,NULL)"
        )
        db.commit()
        db.close()

        data = agentstats.load_data(db_path)
        assert data["daily_tools"] == []
        assert data["sessions"][0].get("effort") is None


# ---- JS-агрегация страницы --------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node не установлен")
class TestPageAggregation:
    @pytest.fixture(scope="class")
    def page(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("activity")
        out = tmp / "stats.html"
        agentstats.generate(build_db(tmp / "ledger.db"), out)
        return out

    def compute(self, page, period):
        run = subprocess.run(
            [NODE, str(PROBE), str(page), str(period)],
            capture_output=True, text=True, encoding="utf-8",
        )
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)

    def test_categories_respect_window(self, page):
        c = self.compute(page, 7)
        # 01.07 (Read×50) и 05.07 (exec×7) вне окна
        assert c["byCat"] == {"shell": 10, "edit": 4, "read": 3}
        assert c["tCalls"] == 17

    def test_subagent_tool_calls_are_counted(self, page):
        """Время субагента не считается, а его работа инструментами — считается."""
        c = self.compute(page, 7)
        assert c["byCat"]["read"] == 3

    def test_categories_all_time(self, page):
        c = self.compute(page, 0)
        assert c["byCat"] == {"shell": 17, "edit": 4, "read": 53}
        assert c["tCalls"] == 74

    def test_raw_tools_breakdown(self, page):
        c = self.compute(page, 0)
        assert c["byTool"] == {"Read": 53, "Bash": 10, "Edit": 4, "exec": 7}

    def test_compactions_respect_window(self, page):
        assert self.compute(page, 7)["tCompact"] == 1
        assert self.compute(page, 0)["tCompact"] == 4

    def test_effort_counts_sessions_not_subagents(self, page):
        c = self.compute(page, 7)
        assert c["byEffort"] == {"xhigh": 1}  # A; субагент A:agent-x не в счёт

    def test_effort_all_time(self, page):
        c = self.compute(page, 0)
        assert c["byEffort"] == {"xhigh": 1, "low": 1}

    def test_approval_modes(self, page):
        c = self.compute(page, 7)
        assert c["byApproval"] == {"auto": 1, "acceptEdits": 1}

    def test_calls_sum_matches_categories(self, page):
        """Сумма по категориям обязана сходиться с суммой по сырым именам."""
        c = self.compute(page, 0)
        assert sum(c["byCat"].values()) == sum(c["byTool"].values()) == c["tCalls"]

    def test_mcp_tool_names_are_shortened(self, page):
        """`mcp__сервер__инструмент` — 40+ символов, в узкой колонке нечитаемо."""
        got = self.compute(page, 0)["toolLabels"]
        assert got[0] == "chrome-devtools: get_network_request"
        assert got[1] == "claude_ai_Gmail: search_threads"
        assert got[2] == "Bash"          # обычные имена не трогаем
        assert got[3] == "mcp__weird"    # непарсимое — оставляем как есть
