"""Сборка статического сайта в каталог _site/.

Берёт JSON из data/, устав из rules.md, шаблоны из templates/ и ресурсы из assets/.

Запуск:  python scripts/build.py [--data ДРУГОЙ_КАТАЛОГ_ДАННЫХ] [--out КАТАЛОГ]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

ROOT = Path(__file__).resolve().parent.parent
MSK = ZoneInfo("Europe/Moscow")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]

PAGES = [
    ("index.html", "Главная", "home"),
    ("standings.html", "Таблица", "standings"),
    ("power.html", "Рейтинг", "power"),
    ("matchups.html", "Матчапы", "matchups"),
    ("teams.html", "Команды", "teams"),
    ("rules.html", "Правила", "rules"),
    ("history.html", "История", "history"),
]


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------ фильтры

def _d(value) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def ru_date(value, year: bool = False) -> str:
    d = _d(value)
    if not d:
        return "—"
    return f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def ru_range(start, end) -> str:
    a, b = _d(start), _d(end)
    if not a or not b:
        return ""
    if a.month == b.month and a.year == b.year:
        return f"{a.day}–{b.day} {MONTHS[b.month - 1]}"
    return f"{a.day} {MONTHS[a.month - 1]} – {b.day} {MONTHS[b.month - 1]}"


def ru_datetime(value) -> str:
    """ISO-время в UTC → московское время."""
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(MSK)
    except ValueError:
        return "—"
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M} МСК"


def plural(n, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def record(obj, w="wins", l="losses", t="ties") -> str:
    return f"{obj.get(w, 0)}-{obj.get(l, 0)}-{obj.get(t, 0)}"


# ------------------------------------------------------------------ устав

def render_rules(md_text: str) -> dict:
    """Markdown устава → HTML с якорями разделов и оглавлением."""
    html = markdown.markdown(md_text, extensions=["tables", "sane_lists", "smarty"], output_format="html")
    title, intro = "Устав лиги", ""
    m = re.match(r"\s*<h1>(.*?)</h1>\s*(<p>.*?</p>)?", html, flags=re.S)
    if m:
        title = re.sub(r"<.*?>", "", m.group(1))
        intro = m.group(2) or ""
        html = html[m.end():]

    toc = []

    def h2(match):
        text = match.group(1).strip()
        plain = re.sub(r"<.*?>", "", text)
        num = re.match(r"(\d+)[.)]?\s+(.*)", plain, flags=re.S)
        if num:
            n, name = num.group(1), num.group(2)
            anchor = f"razdel-{n}"
        else:
            n, name = "", plain
            anchor = "razdel-" + hashlib.md5(plain.encode()).hexdigest()[:6]
        toc.append({"id": anchor, "num": n, "title": name})
        badge = f'<span class="badge-num" aria-hidden="true">{n}</span>' if n else ""
        label = f'<span class="visually-hidden">{n}. </span>' if n else ""
        return f'<h2 id="{anchor}" class="charter-h">{badge}<span>{label}{name}</span></h2>'

    html = re.sub(r"<h2>(.*?)</h2>", h2, html, flags=re.S)
    html = html.replace("<table>", '<div class="table-scroll"><table class="plain-table">').replace("</table>", "</table></div>")
    return {"title": title, "intro": Markup(intro), "html": Markup(html), "toc": toc}


# ------------------------------------------------------------------ настройки ESPN

SCORING = {"H2H_CATEGORY": "Head to Head Each Category", "H2H_POINTS": "Head to Head Points",
           "H2H_MOST_CATEGORIES": "Head to Head Most Categories", "ROTO": "Rotisserie", "TOTAL_SEASON_POINTS": "Season Points"}
LOCK = {"INDIVIDUAL_GAME": "по началу матча каждого игрока", "FIRSTGAME_SCORINGPERIOD": "с первым матчем игрового дня"}
ACQ = {"WAIVERS_TRADITIONAL": "вейвер по приоритету", "WAIVERS_CONTINUOUS": "постоянный вейвер", "FREE_AGENTS": "свободные агенты без вейвера"}
SEEDING = {"H2H_RECORD": "личные встречи", "TOTAL_POINTS_SCORED": "набранные очки", "INTRA_DIVISION_RECORD": "результат в дивизионе"}
DRAFT = {"OFFLINE": "офлайн", "SNAKE": "змейка", "AUCTION": "аукцион", "AUTOPICK": "автодрафт"}


def espn_settings_groups(league: dict) -> list:
    s = league.get("settings") or {}
    if not s:
        return []

    def weeks(n):
        return f"{n} {plural(n, 'неделя', 'недели', 'недель')}" if n else None

    def limit(v, unit=""):
        if v is None:
            return None
        return "без ограничений" if v < 0 else f"{v}{unit}"

    slots = ", ".join(f"{x['slot']} × {x['count']}" if x["count"] > 1 else x["slot"] for x in s.get("roster_slots", []))
    rounds = "; ".join(
        f"{r['name']} — {ru_range(r['start'], r['end'])} ({weeks(r['weeks'])})" for r in s.get("playoff_rounds", []) if r.get("start")
    )
    groups = [
        ("Формат", [
            ("Тип лиги", SCORING.get(s.get("scoring_type"), s.get("scoring_type"))),
            ("Команд", s.get("team_count")),
            ("Категории", ", ".join(s.get("categories", []))),
            ("Матчапов в регулярке", s.get("regular_matchups")),
            ("Длительность матчапа регулярки", weeks(s.get("regular_matchup_weeks"))),
        ]),
        ("Состав", [
            ("Слоты", slots),
            ("Мест в ростере", s.get("roster_size")),
            ("Игроков в старте", s.get("starters")),
            ("Блокировка состава", LOCK.get(s.get("lineup_lock"), s.get("lineup_lock"))),
        ]),
        ("Плей-офф", [
            ("Команд в плей-офф", s.get("playoff_teams")),
            ("Длительность раунда", weeks(s.get("playoff_matchup_weeks"))),
            ("Раунды", rounds),
            ("Тай-брейк при посеве", SEEDING.get(s.get("playoff_seeding"), s.get("playoff_seeding"))),
            ("Пересев между раундами", None if s.get("playoff_reseed") is None else ("да" if s["playoff_reseed"] else "нет")),
        ]),
        ("Транзакции", [
            ("Способ подбора игроков", ACQ.get(s.get("acquisition_type"), s.get("acquisition_type"))),
            ("Лимит замен", f"{s['acquisitions_per_week']} в неделю" if s.get("acquisitions_per_week") else None),
            ("Лимит замен на сезон", limit(s.get("acquisitions_season_limit"))),
            ("Срок на вейвере", f"{s['waiver_hours']} ч" if s.get("waiver_hours") else None),
        ]),
        ("Обмены", [
            ("Дедлайн обменов", ru_datetime(s.get("trade_deadline")) if s.get("trade_deadline") else None),
            ("Срок рассмотрения", f"{s['trade_review_hours']} ч" if s.get("trade_review_hours") is not None else None),
            ("Лимит обменов", limit(s.get("trade_limit"))),
        ]),
        ("Киперы и драфт", [
            ("Киперов на команду", f"до {s['keepers']}" if s.get("keepers") else None),
            ("Тип драфта", DRAFT.get(s.get("draft_type"), s.get("draft_type"))),
            ("Дата драфта", ru_date(s.get("draft_date"), year=True) if s.get("draft_date") else None),
            ("Обмен драфт-пиков в ESPN", None if s.get("draft_pick_trading") is None else ("включён" if s["draft_pick_trading"] else "выключен")),
        ]),
    ]
    return [
        {"title": title, "rows": [{"k": k, "v": v} for k, v in rows if v not in (None, "")]}
        for title, rows in groups if any(v not in (None, "") for _, v in rows)
    ]


# ------------------------------------------------------------------ сборка

def build(data_dir: Path, out: Path) -> None:
    league = read_json(data_dir / "league.json", {})
    standings = read_json(data_dir / "standings.json", {"rows": []})
    matchups = read_json(data_dir / "matchups.json", {"periods": []})
    teams = read_json(data_dir / "teams.json", {"teams": []})
    history = read_json(data_dir / "history.json", {"seasons": []})
    power = read_json(data_dir / "power.json", {"editions": []})

    periods = matchups.get("periods", [])
    current = next((p for p in periods if p["state"] == "current"), None)
    played = [p for p in periods if p["state"] == "past" and p.get("matchups")]
    last_done = played[-1] if played else None
    upcoming = next((p for p in periods if p["state"] == "future"), None)
    default_period = current or last_done or upcoming
    season = league.get("season")
    seasons = sorted(history.get("seasons", []), key=lambda s: s.get("season", 0), reverse=True)

    rules_path = ROOT / "rules.md"
    charter = render_rules(rules_path.read_text(encoding="utf-8")) if rules_path.exists() else None

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "assets", out / "assets")
    if data_dir.exists():
        shutil.copytree(data_dir, out / "data")
    for extra in ("CNAME",):
        if (ROOT / extra).exists():
            shutil.copy2(ROOT / extra, out / extra)
    (out / ".nojekyll").write_text("", encoding="utf-8")

    def asset_hash(rel: str) -> str:
        return hashlib.md5((ROOT / "assets" / rel).read_bytes()).hexdigest()[:8]

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters.update(ru_date=ru_date, ru_datetime=ru_datetime, record=record)
    env.globals.update(ru_range=ru_range, plural=plural)

    ctx = {
        "league": league,
        "season_label": league.get("season_label"),
        "updated": ru_datetime(league.get("updated_at")) if league.get("updated_at") else None,
        "standings": standings,
        "rows": standings.get("rows", []),
        "playoff_spots": standings.get("playoff_spots") or league.get("playoff_spots") or 0,
        "periods": periods,
        "current": current,
        "last_done": last_done,
        "upcoming": upcoming,
        "default_period": default_period,
        "teams": teams.get("teams", []),
        "seasons": seasons,
        "power": power,
        "editions": power.get("editions", []),
        "next_champion_year": season or (seasons[0]["season"] + 1 if seasons else date.today().year + 1),
        "charter": charter,
        "espn_groups": espn_settings_groups(league),
        "categories": [c["key"] for c in league.get("categories", [])],
        "pages": PAGES,
        "css_v": asset_hash("css/site.css"),
        "js_v": asset_hash("js/site.js"),
    }
    for filename, title, key in PAGES:
        html = env.get_template(f"{key}.html").render(page=key, page_title=title, **ctx)
        (out / filename).write_text(html, encoding="utf-8")
    (out / "404.html").write_text(env.get_template("404.html").render(page="404", page_title="Страница не найдена", **ctx), encoding="utf-8")
    print(f"Сайт собран: {out}  (страниц: {len(PAGES) + 1})")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--out", default=str(ROOT / "_site"))
    args = ap.parse_args()
    build(Path(args.data), Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
