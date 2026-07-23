"""AgentPulse statistics page (личная фича ветки slem).

Reads the AgentPulse ledger (SQLite) with the stdlib and renders a fully
self-contained HTML dashboard: the data is embedded into the page at
generation time, so the browser needs no server, no CDN and no network.
Aggregation formulas live in the template's JS and mirror AgentPulse 1:1.
"""

import html
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import markdown

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from .config import CONFIG_DIR

DEFAULT_DB = r"C:\SL\CursosWorkspace\AgentPulse\data\ledger.db"
# Карточки проектов, которые AgentPulse ведёт в vault (<vault>/sl_projects/<Имя>.md)
DEFAULT_CARDS_DIR = r"C:\SL\Vaults\SL_Wiki\sl_projects"

# Effective task type follows AgentPulse itself: manual override wins.
_SESSIONS_SQL = """
SELECT agent, session_id, project, cwd, first_ts, last_ts, active_seconds,
       input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
       models_json, time_reliable, title,
       COALESCE(task_type_manual, task_type) AS task_type
FROM sessions
"""

_DAILY_SQL = """
SELECT session_id, date, active_seconds, output_tokens, time_reliable
FROM daily_activity
"""


def _resource(name: str) -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller: unpacked next to the module tree
        return Path(sys._MEIPASS) / "aibar" / "resources" / name
    return Path(__file__).parent / "resources" / name


def _card_links(projects: set[str], cards_dir: Path | None) -> dict[str, str]:
    """Относительные ссылки на HTML-карточки проектов (кладём рядом в cards/)."""
    if cards_dir is None or not cards_dir.is_dir():
        return {}
    return {
        name: f"cards/{quote(name, safe='')}.html"
        for name in projects
        if name and (cards_dir / f"{name}.md").is_file()
    }


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Плоский YAML-frontmatter карточки -> (метаданные, тело)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        return {}, text
    meta = {}
    for line in parts[0].splitlines()[1:]:
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip('"')
    return meta, parts[1].lstrip("\n")


_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
# AUTO-блок AgentPulse дублирует метаданные frontmatter — на карточке не нужен
_AUTO_BLOCK = re.compile(r"<!--\s*AUTO-BEGIN.*?AUTO-END\s*-->", re.DOTALL)


def _render_card(name: str, md_path: Path, back: str, generated_at: str) -> str:
    """HTML-страница карточки проекта в стиле дашборда."""
    meta, body = _split_frontmatter(md_path.read_text(encoding="utf-8-sig"))
    body = _AUTO_BLOCK.sub("", body)
    # Obsidian-ссылки [[цель|алиас]] в вебе некликабельны — оставляем текст
    body = _WIKILINK.sub(lambda m: m.group(2) or m.group(1), body)
    doc = markdown.markdown(body, extensions=["tables", "fenced_code"])
    meta_html = ""
    if meta:
        rows = "".join(
            f'<div class="k">{html.escape(k)}</div><div class="v">{html.escape(v)}</div>'
            for k, v in meta.items()
            if v and v != "—"
        )
        meta_html = f'<div class="meta">{rows}</div>'
    template = _resource("card_template.html").read_text(encoding="utf-8")
    return (
        template.replace("__TITLE__", html.escape(name))
        .replace("__BACK__", html.escape(back))
        .replace("__META__", meta_html)
        .replace("__BODY__", doc)
        .replace("__SOURCE__", html.escape(str(md_path)))
        .replace("__GENERATED__", html.escape(generated_at))
    )


def _write_cards(data: dict, out_path: Path, cards_dir: Path | None) -> None:
    """Перегенерировать cards/ рядом со страницей статистики (старые — удалить)."""
    cards_out = out_path.parent / "cards"
    if cards_out.is_dir():
        for stale in cards_out.glob("*.html"):
            stale.unlink()
    if not data["cards"]:
        return
    cards_out.mkdir(parents=True, exist_ok=True)
    back = f"../{out_path.name}"
    for name in data["cards"]:
        page = _render_card(
            name, Path(cards_dir) / f"{name}.md", back, data["generated_at"]
        )
        (cards_out / f"{name}.html").write_text(page, encoding="utf-8")


def load_data(
    db_path: Path | str,
    cards_dir: Path | str | None = None,
    outliers_since: str = "",
) -> dict:
    """Sessions and daily slices from ledger.db as plain JSON-ready dicts."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        sessions = [dict(r) for r in con.execute(_SESSIONS_SQL)]
        daily = [dict(r) for r in con.execute(_DAILY_SQL)]
    finally:
        con.close()
    projects = {s["project"] for s in sessions if s["project"]}
    return {
        "sessions": sessions,
        "daily": daily,
        "cards": _card_links(projects, Path(cards_dir) if cards_dir else None),
        # Сессии «вне реестра» старше этой отметки помечены как «не для
        # анализа» и в одноимённый список не попадают (обнуление 23.07.2026)
        "outliers_since": outliers_since or "",
        "source": str(db_path),
        "generated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }


def generate(
    db_path: Path | str,
    out_path: Path | str,
    cards_dir: Path | str | None = None,
    outliers_since: str = "",
) -> Path:
    """Render the standalone stats page; returns the written path."""
    data = load_data(db_path, cards_dir=cards_dir, outliers_since=outliers_since)
    template = _resource("stats_template.html").read_text(encoding="utf-8")
    # "</" must not appear raw inside the inline <script> (a session title
    # containing </script> would terminate it); JSON stays equivalent.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = template.replace("__AGENTPULSE_DATA__", payload)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    _write_cards(data, out_path, Path(cards_dir) if cards_dir else None)
    return out_path


def open_stats(cfg: dict | None = None) -> None:
    """Generate a fresh page from the ledger and open it in the browser."""
    db_path = (cfg or {}).get("agentpulse_db") or DEFAULT_DB
    cards_dir = (cfg or {}).get("agentpulse_cards") or DEFAULT_CARDS_DIR
    outliers_since = (cfg or {}).get("agentpulse_outliers_since") or ""
    try:
        target = generate(
            db_path,
            CONFIG_DIR / "agentpulse_stats.html",
            cards_dir=cards_dir,
            outliers_since=outliers_since,
        )
    except FileNotFoundError:
        QMessageBox.warning(
            None,
            "AIBar — статистика",
            f"Не найден ledger AgentPulse:\n{db_path}\n\n"
            "Проверьте путь в config.json (ключ agentpulse_db) и что "
            "agentpulse collect уже запускался.",
        )
        return
    except sqlite3.Error as exc:
        QMessageBox.warning(
            None, "AIBar — статистика", f"Не удалось прочитать ledger: {exc}"
        )
        return
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
