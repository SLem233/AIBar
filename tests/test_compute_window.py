"""Агрегация за период: дневные срезы там, где они есть; итог сессии — иначе.

Тесты гоняют настоящий JS из сгенерированной страницы через node (харнесс
tests/compute_probe.js), поэтому проверяют реальные цифры дашборда.
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

pytestmark = pytest.mark.skipif(NODE is None, reason="node не установлен")

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
  PRIMARY KEY (agent, session_id));
CREATE TABLE daily_activity (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, date TEXT NOT NULL, project TEXT,
  active_seconds INTEGER NOT NULL DEFAULT 0, messages INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  time_reliable INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (agent, session_id, date));
CREATE TABLE daily_models (
  agent TEXT NOT NULL, session_id TEXT NOT NULL, date TEXT NOT NULL, model TEXT NOT NULL,
  project TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (agent, session_id, date, model));
"""

# Окно «7 дней» относительно последнего события 25.07 — с 19.07 по 25.07 (МСК).
# A  — покрыта срезами и начата ДО окна: в окно должен попасть только день 25.07
# B  — без срезов (старый сбор), последняя активность в окне: считается целиком
# A:agent-x — субагент: токены учитываются, сессией и временем — нет
# D  — целиком вне окна: не считается совсем
FIXTURE_SESSIONS = [
    # agent, sid, project, cwd, first_ts, last_ts, act, in, out, cread, ccreate, models, type
    ("claude-code", "A", "P", None, "2026-07-01T09:00:00+00:00", "2026-07-25T09:00:00+00:00",
     4200, 1000, 1100, 5000, 100, '{"m-old": 1100}', "разработка"),
    ("claude-code", "B", "P", None, "2026-07-24T09:00:00+00:00", "2026-07-24T10:00:00+00:00",
     300, 50, 50, 0, 0, '{"m-legacy": 50}', "разработка"),
    ("claude-code", "A:agent-x", "P", None, "2026-07-25T09:30:00+00:00",
     "2026-07-25T09:40:00+00:00", 0, 7, 7, 0, 0, "{}", None),
    ("codex", "D", "Q", None, "2026-07-05T09:00:00+00:00", "2026-07-05T10:00:00+00:00",
     1200, 500, 500, 0, 0, '{"m-d": 500}', "инфраструктура"),
]

FIXTURE_DAILY = [
    # agent, sid, date, act, in, out, cread, ccreate
    ("claude-code", "A", "2026-07-01", 3600, 900, 1000, 4000, 90),
    ("claude-code", "A", "2026-07-25", 600, 100, 100, 1000, 10),
    ("claude-code", "A:agent-x", "2026-07-25", 0, 7, 7, 0, 0),
    ("codex", "D", "2026-07-05", 1200, 500, 500, 0, 0),
]

FIXTURE_DAILY_MODELS = [
    ("claude-code", "A", "2026-07-01", "m-old", 1000),
    ("claude-code", "A", "2026-07-25", "m-new", 100),
]


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("window")
    db_path = tmp / "ledger.db"
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO sessions (agent, session_id, project, cwd, first_ts, last_ts,"
        " active_seconds, input_tokens, output_tokens, cache_read_tokens,"
        " cache_creation_tokens, models_json, task_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        FIXTURE_SESSIONS,
    )
    db.executemany(
        "INSERT INTO daily_activity (agent, session_id, date, active_seconds,"
        " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)"
        " VALUES (?,?,?,?,?,?,?,?)",
        FIXTURE_DAILY,
    )
    db.executemany(
        "INSERT INTO daily_models (agent, session_id, date, model, output_tokens)"
        " VALUES (?,?,?,?,?)",
        FIXTURE_DAILY_MODELS,
    )
    db.commit()
    db.close()
    out = tmp / "stats.html"
    agentstats.generate(db_path, out)
    return out


def compute(page, period):
    run = subprocess.run(
        [NODE, str(PROBE), str(page), str(period)],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


# ---- окно 7 дней: считаем только то, что реально было внутри ----------------

def test_window_bounds(page):
    c = compute(page, 7)
    assert c["todayKey"] == "2026-07-25"
    assert c["cutKey"] == "2026-07-19"


def test_covered_session_counts_only_its_in_window_days(page):
    c = compute(page, 7)
    # у A в окне только 25.07 (600 с, in 110, out 100, cache 1000), не весь итог
    assert c["tOut"] == 157  # A 100 + B 50 + субагент 7
    assert c["tIn"] == 167  # A 110 + B 50 + субагент 7
    assert c["tAct"] == 900  # A 600 + B 300
    assert c["tCache"] == 1000


def test_uncovered_session_falls_back_to_session_totals(page):
    c = compute(page, 7)
    assert c["byProj"]["P"]["sess"] == 2  # A и B
    assert c["nSess"] == 2 and c["nSub"] == 1


def test_session_outside_window_is_excluded(page):
    c = compute(page, 7)
    assert "Q" not in c["byProj"]
    assert "D" not in c["sessIds"]


def test_project_and_matrix_match_totals(page):
    c = compute(page, 7)
    assert c["byProj"]["P"] == {"act": 900, "in": 167, "out": 157, "sess": 2}
    assert c["mtx"]["P||claude-code"] == {"act": 900, "out": 157}


def test_models_use_daily_slices_in_window(page):
    c = compute(page, 7)
    # m-old (день вне окна) не считается, m-new — считается; B без срезов даёт m-legacy
    assert c["byModel"] == {"m-new": 100, "m-legacy": 50}


def test_subagent_tokens_roll_up_to_parent(page):
    c = compute(page, 7)
    assert c["subTok"]["A"] == {"in": 7, "out": 7}


# ---- всё время: ничего не теряем -------------------------------------------

def test_all_time_counts_everything(page):
    c = compute(page, 0)
    assert c["tOut"] == 1657  # 1100 + 50 + 7 + 500
    assert c["tAct"] == 5700  # 4200 + 300 + 1200
    assert c["nSess"] == 3 and c["nSub"] == 1
    assert c["byProj"]["Q"]["out"] == 500


def test_all_time_models_mix_daily_and_session(page):
    c = compute(page, 0)
    assert c["byModel"] == {"m-old": 1000, "m-new": 100, "m-legacy": 50, "m-d": 500}


def test_chart_days_stay_exact(page):
    c = compute(page, 0)
    assert c["byDayMap"]["2026-07-01"]["out"] == 1000
    assert c["byDayMap"]["2026-07-25"]["out"] == 107  # 100 сессии + 7 субагента
    assert c["byDayMap"]["2026-07-25"]["act"] == 600  # время субагента не в счёт


def test_chart_total_matches_kpi(page):
    """Сумма столбиков графика сходится с KPI по out-токенам."""
    c = compute(page, 0)
    assert sum(d["out"] for d in c["byDayMap"].values()) == c["tOut"]
