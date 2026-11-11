"""Выгрузка данных лиги из ESPN в JSON-файлы каталога data/.

Секреты читаются из переменных окружения LEAGUE_ID, ESPN_S2, SWID
(локально — из файла .env). Значения секретов никуда не печатаются.

Запуск:  python scripts/fetch_data.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from power import compute_power  # noqa: E402

DATA = ROOT / "data"
ET = ZoneInfo("America/New_York")

STAT_NAMES = {
    0: "PTS", 1: "BLK", 2: "STL", 3: "AST", 4: "OREB", 5: "DREB", 6: "REB",
    11: "TO", 13: "FGM", 14: "FGA", 15: "FTM", 16: "FTA", 17: "3PM", 18: "3PA",
    19: "FG%", 20: "FT%", 21: "3P%", 37: "DD", 38: "TD", 40: "MIN", 41: "GS", 42: "GP",
}
# Порядок категорий как в уставе; незнакомые категории добавляются в конец.
CAT_ORDER = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TO"]
SLOT_NAMES = {
    0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 7: "SG/SF",
    8: "G/F", 9: "PF/C", 10: "F/C", 11: "UTIL", 12: "Скамейка", 13: "IR",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ms_to_et_date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ET).date()


def ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- календарь

def build_calendar(pro_schedule: dict, final_sp: int):
    """Возвращает (date_of(sp), множество игровых дней НБА)."""
    offsets = Counter()
    game_days = set()
    for team_games in pro_schedule.values():
        for sp, games in team_games.items():
            for g in games:
                d = ms_to_et_date(g["date"])
                game_days.add(d)
                offsets[d - timedelta(days=int(sp) - 1)] += 1
    if not offsets:
        return None, set()
    day0 = offsets.most_common(1)[0][0]
    return (lambda sp: day0 + timedelta(days=int(sp) - 1)), game_days


def no_game_runs(game_days: set, min_len: int = 4) -> list:
    """Паузы в календаре НБА (подряд идущие дни без матчей).

    Первой идёт пауза, ближайшая к середине февраля, — перерыв на Матч всех звёзд.
    Декабрьская «пауза» появляется из-за ещё не назначенных матчей Кубка НБА.
    """
    if not game_days:
        return []
    days, runs, run = sorted(game_days), [], []
    d = days[0]
    while d <= days[-1]:
        if d in game_days:
            if len(run) >= min_len:
                runs.append(run)
            run = []
        else:
            run.append(d)
        d += timedelta(days=1)
    all_star = date(days[-1].year, 2, 15)
    return sorted(runs, key=lambda r: abs((r[len(r) // 2] - all_star).days))


def week_ranges(first_day: date, n_weeks: int, game_days: set, last_day: date | None):
    """Игровые недели ESPN: понедельник–воскресенье.

    Неделя с перерывом на Матч всех звёзд у ESPN длится две календарные недели.
    Какая именно пауза считается таким перерывом, проверяем по дате конца сезона
    из ESPN (finalScoringPeriod): берём вариант, при котором календарь сходится.
    """
    def build(breaks: set):
        weeks, start = [], first_day
        for _ in range(n_weeks):
            end = start + timedelta(days=6 - start.weekday())
            if any(start <= b <= end for b in breaks):
                end += timedelta(days=7)
            weeks.append((start, end))
            start = end + timedelta(days=1)
        return weeks

    variants = [build(set(run)) for run in no_game_runs(game_days)] + [build(set())]
    for weeks in variants:
        if not last_day or not weeks or weeks[-1][1] == last_day:
            return weeks
    weeks = variants[0]
    weeks[-1] = (weeks[-1][0], max(last_day, weeks[-1][0]))
    return weeks


def playoff_round_name(teams_left: int) -> str:
    if teams_left <= 2:
        return "Финал"
    return f"1/{teams_left // 2} финала"


# ---------------------------------------------------------------- разбор ESPN

def fmt_stat(key: str, value) -> str:
    if value is None:
        return "—"
    if key.endswith("%"):
        s = f"{value:.3f}"
        return s[1:] if s.startswith("0") else s
    return str(int(round(value)))


def side_payload(side: dict | None, teams: dict) -> dict | None:
    if not side:
        return None
    cs = side.get("cumulativeScore") or {}
    t = teams.get(side.get("teamId"), {})
    return {
        "team_id": side.get("teamId"),
        "name": t.get("name", "—"),
        "abbrev": t.get("abbrev", ""),
        "wins": int(cs.get("wins") or 0),
        "losses": int(cs.get("losses") or 0),
        "ties": int(cs.get("ties") or 0),
    }


def cats_payload(game: dict, categories: list) -> list:
    home = ((game.get("home") or {}).get("cumulativeScore") or {}).get("scoreByStat") or {}
    away = ((game.get("away") or {}).get("cumulativeScore") or {}).get("scoreByStat") or {}
    out = []
    for c in categories:
        h, a = home.get(str(c["id"])), away.get(str(c["id"]))
        if h is None and a is None:
            continue
        res = (h or {}).get("result")
        winner = {"WIN": "home", "LOSS": "away", "TIE": "tie"}.get(res)
        out.append({
            "key": c["key"],
            "home": fmt_stat(c["key"], (h or {}).get("score")),
            "away": fmt_stat(c["key"], (a or {}).get("score")),
            "home_raw": (h or {}).get("score"),
            "away_raw": (a or {}).get("score"),
            "winner": winner,
        })
    return out


def member_name(member: dict) -> str:
    return (member.get("displayName") or "").strip() or " ".join(
        x for x in [member.get("firstName"), member.get("lastName")] if x
    ).strip() or "—"


def settings_payload(s: dict, categories: list, trade_deadline: str | None, playoff_rounds: list) -> dict:
    roster, acq = s.get("rosterSettings", {}), s.get("acquisitionSettings", {})
    sched, draft, trade = s.get("scheduleSettings", {}), s.get("draftSettings", {}), s.get("tradeSettings", {})
    slots = [
        {"slot": SLOT_NAMES.get(int(k), f"Слот {k}"), "count": v}
        for k, v in sorted(roster.get("lineupSlotCounts", {}).items(), key=lambda kv: int(kv[0]))
        if v
    ]
    per_day = acq.get("matchupAcquisitionLimit") or 0
    per_week = None
    if per_day and per_day > 0:
        per_week = round(per_day * 7) if acq.get("matchupLimitPerScoringPeriod") else round(per_day)
    draft_date = draft.get("date")
    return {
        "scoring_type": s.get("scoringSettings", {}).get("scoringType"),
        "categories": [c["key"] for c in categories],
        "team_count": s.get("size"),
        "roster_slots": slots,
        "roster_size": sum(x["count"] for x in slots),
        "starters": sum(x["count"] for x in slots if x["slot"] not in ("Скамейка", "IR")),
        "lineup_lock": roster.get("lineupLocktimeType"),
        "regular_matchups": sched.get("matchupPeriodCount"),
        "regular_matchup_weeks": sched.get("matchupPeriodLength"),
        "playoff_teams": sched.get("playoffTeamCount"),
        "playoff_matchup_weeks": sched.get("playoffMatchupPeriodLength"),
        "playoff_rounds": playoff_rounds,
        "playoff_seeding": sched.get("playoffSeedingRule"),
        "playoff_reseed": sched.get("playoffReseed"),
        "matchup_tie_rule": s.get("scoringSettings", {}).get("matchupTieRule"),
        "acquisition_type": acq.get("acquisitionType"),
        "acquisitions_per_week": per_week,
        "acquisitions_season_limit": acq.get("acquisitionLimit"),
        "waiver_hours": acq.get("waiverHours"),
        "waiver_order_reset": acq.get("waiverOrderReset"),
        "trade_deadline": trade_deadline,
        "trade_review_hours": trade.get("revisionHours"),
        "trade_veto_votes": trade.get("vetoVotesRequired"),
        "trade_limit": trade.get("max"),
        "keepers": draft.get("keeperCount"),
        "draft_type": draft.get("type"),
        "draft_date": ms_to_iso(draft_date),
        "draft_pick_trading": draft.get("isTradingEnabled"),
    }


def collect(league) -> dict:
    raw = league.espn_request.league_get(
        params={"view": ["mSettings", "mTeam", "mStandings", "mMatchup", "mMatchupScore", "mStatus"]}
    )
    s, status = raw["settings"], raw.get("status", {})
    sched = s.get("scheduleSettings", {})
    season = int(raw.get("seasonId") or league.year)
    overrides = read_json(DATA / "managers_override.json", {})

    # категории
    items = s.get("scoringSettings", {}).get("scoringItems", [])
    categories = [
        {"id": it["statId"], "key": STAT_NAMES.get(it["statId"], f"Stat {it['statId']}"), "reverse": bool(it.get("isReverseItem"))}
        for it in items
    ]
    categories.sort(key=lambda c: CAT_ORDER.index(c["key"]) if c["key"] in CAT_ORDER else 99)

    # команды и менеджеры
    members = {m["id"]: m for m in raw.get("members", [])}
    teams = {}
    for t in raw.get("teams", []):
        owner_ids = t.get("owners") or ([t["primaryOwner"]] if t.get("primaryOwner") else [])
        espn_manager = ", ".join(member_name(members[o]) for o in owner_ids if o in members) or "—"
        ov = overrides.get(str(t["id"]))
        if isinstance(ov, dict):
            ov = ov.get("manager")
        rec = (t.get("record") or {}).get("overall") or {}
        teams[t["id"]] = {
            "id": t["id"],
            "name": (t.get("name") or "").strip() or f"Команда {t['id']}",
            "abbrev": t.get("abbrev") or "",
            "manager": ov if isinstance(ov, str) and ov.strip() else espn_manager,
            "seed": t.get("playoffSeed") or 0,
            "final_rank": t.get("rankCalculatedFinal") or 0,
            "wins": rec.get("wins", 0), "losses": rec.get("losses", 0), "ties": rec.get("ties", 0),
            "pct": rec.get("percentage", 0.0), "gb": rec.get("gamesBack", 0.0),
            "streak_type": rec.get("streakType", "NONE"), "streak_len": rec.get("streakLength", 0),
        }

    # календарь матчап-периодов
    final_sp = status.get("finalScoringPeriod") or league.finalScoringPeriod
    date_of, game_days = build_calendar(league.pro_schedule, final_sp)
    mp_weeks = {int(k): v for k, v in sched.get("matchupPeriods", {}).items()}
    reg_count = sched.get("matchupPeriodCount") or 0
    current_mp = status.get("currentMatchupPeriod") or 0
    known_sps: dict[int, set] = {}
    for g in raw.get("schedule", []):
        for side in ("home", "away"):
            pts = (g.get(side) or {}).get("pointsByScoringPeriod") or {}
            known_sps.setdefault(g["matchupPeriodId"], set()).update(int(k) for k in pts)

    week_map = {}
    if date_of:
        n_weeks = max((w for ws in mp_weeks.values() for w in ws), default=0)
        ranges = week_ranges(date_of(status.get("firstScoringPeriod") or 1), n_weeks, game_days, date_of(final_sp))
        week_map = {i + 1: r for i, r in enumerate(ranges)}

    today = datetime.now(ET).date()
    playoff_ids = sorted(k for k in mp_weeks if k > reg_count)
    playoff_teams = sched.get("playoffTeamCount") or 0
    periods = []
    for mp in sorted(mp_weeks):
        ws = [week_map[w] for w in mp_weeks[mp] if w in week_map]
        start = ws[0][0] if ws else None
        end = ws[-1][1] if ws else None
        estimated = True
        if date_of and known_sps.get(mp) and mp < current_mp:
            start, end, estimated = date_of(min(known_sps[mp])), date_of(max(known_sps[mp])), False
        elif date_of and known_sps.get(mp):
            start = date_of(min(known_sps[mp]))
        is_playoff = mp > reg_count
        round_name = None
        if is_playoff:
            round_name = playoff_round_name(playoff_teams // (2 ** playoff_ids.index(mp)))
        if mp < current_mp or (end and today > end and mp == current_mp and mp == max(mp_weeks)):
            state = "past"
        elif mp == current_mp:
            state = "current"
        else:
            state = "future"
        periods.append({
            "id": mp,
            "label": round_name or f"Матчап {mp}",
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "days": ((end - start).days + 1) if start and end else None,
            "weeks": round(((end - start).days + 1) / 7) if start and end else len(mp_weeks[mp]),
            "is_playoff": is_playoff,
            "state": state,
            "dates_estimated": estimated and state != "past",
        })
    if periods and periods[0]["start"] and today < date.fromisoformat(periods[0]["start"]):
        for p in periods:
            p["state"] = "future"

    # матчапы
    tier_names = {
        "NONE": None, "WINNERS_BRACKET": "Плей-офф",
        "WINNERS_CONSOLATION_LADDER": "Матчи за места", "LOSERS_CONSOLATION_LADDER": "Нижняя сетка",
    }
    by_period: dict[int, list] = {}
    records = {tid: {"w": 0, "l": 0, "t": 0} for tid in teams}
    state_of = {p["id"]: p["state"] for p in periods}
    for g in raw.get("schedule", []):
        mp = g["matchupPeriodId"]
        home, away = side_payload(g.get("home"), teams), side_payload(g.get("away"), teams)
        if not home or not away:
            continue  # пропуск раунда (bye)
        winner = {"HOME": "home", "AWAY": "away", "TIE": "tie"}.get(g.get("winner"))
        if state_of.get(mp) == "future":
            winner = None
        cats = [] if state_of.get(mp) == "future" else cats_payload(g, categories)
        by_period.setdefault(mp, []).append({
            "id": g.get("id"), "tier": tier_names.get(g.get("playoffTierType"), g.get("playoffTierType")),
            "tier_code": g.get("playoffTierType") or "NONE",
            "home": home, "away": away, "winner": winner, "cats": cats,
        })
        if state_of.get(mp) == "past" and mp <= reg_count and winner:
            h, a = records.get(home["team_id"]), records.get(away["team_id"])
            if h and a:
                if winner == "tie":
                    h["t"] += 1; a["t"] += 1
                else:
                    (h if winner == "home" else a)["w"] += 1
                    (a if winner == "home" else h)["l"] += 1

    tier_rank = {"WINNERS_BRACKET": 0, "WINNERS_CONSOLATION_LADDER": 1, "LOSERS_CONSOLATION_LADDER": 2}
    matchup_periods = [
        {**p, "matchups": sorted(by_period.get(p["id"], []), key=lambda m: (tier_rank.get(m["tier_code"], 0), m["id"] or 0))}
        for p in periods
    ]

    # таблица
    ordered = sorted(teams.values(), key=lambda t: (t["seed"] or 999, -t["pct"], t["name"]))
    has_streak = any(t["streak_type"] not in ("NONE", None) and t["streak_len"] for t in ordered)
    rows = []
    for i, t in enumerate(ordered, 1):
        played = t["wins"] + t["losses"] + t["ties"]
        rows.append({
            "rank": t["seed"] or i, "team_id": t["id"], "name": t["name"], "abbrev": t["abbrev"],
            "manager": t["manager"],
            "wins": t["wins"], "losses": t["losses"], "ties": t["ties"],
            "pct": fmt_stat("%", t["pct"]) if played else "—",
            "gb": ("—" if not t["gb"] else f"{t['gb']:g}") if played else "—",
            "matchups": records[t["id"]],
            "streak": (("В" if t["streak_type"] == "WIN" else "П" if t["streak_type"] == "LOSS" else "Н") + str(t["streak_len"]))
            if has_streak and t["streak_len"] else None,
            "in_playoff_zone": (t["seed"] or i) <= playoff_teams,
        })

    # состояние сезона
    final_period = matchup_periods[-1] if matchup_periods else None
    title_game = next((m for m in (final_period or {}).get("matchups", []) if m["tier_code"] == "WINNERS_BRACKET"), None)
    finished = bool(
        final_period and title_game and title_game["winner"]
        and final_period["end"] and today > date.fromisoformat(final_period["end"])
        and any(t["final_rank"] == 1 for t in teams.values())
    )
    if finished:
        for p in matchup_periods:
            p["state"] = "past"
        phase = "finished"
    elif not periods or all(p["state"] == "future" for p in periods):
        phase = "preseason"
    elif current_mp > reg_count:
        phase = "playoffs"
    else:
        phase = "regular"

    deadline_ms = s.get("tradeSettings", {}).get("deadlineDate")
    playoff_rounds = [
        {"name": p["label"], "start": p["start"], "end": p["end"], "weeks": p["weeks"]}
        for p in periods if p["is_playoff"]
    ]
    league_payload = {
        "name": "Bird Rights Keeper League",
        "espn_name": s.get("name"),
        "season": season,
        "season_label": f"{season - 1}-{str(season)[-2:]}",
        "phase": phase,
        "current_period": current_mp if phase in ("regular", "playoffs") else None,
        "regular_periods": reg_count,
        "playoff_spots": playoff_teams,
        "team_count": s.get("size") or len(teams),
        "categories": categories,
        "settings": settings_payload(
            s, categories, ms_to_iso(deadline_ms), playoff_rounds
        ),
    }
    team_cards = [
        {"id": t["id"], "name": t["name"], "abbrev": t["abbrev"], "manager": t["manager"],
         "rank": r["rank"], "wins": t["wins"], "losses": t["losses"], "ties": t["ties"],
         "pct": r["pct"], "matchups": r["matchups"], "in_playoff_zone": r["in_playoff_zone"]}
        for r in rows for t in [teams[r["team_id"]]]
    ]
    return {
        "league": league_payload,
        "standings": {"season": season, "playoff_spots": playoff_teams, "has_streak": has_streak, "rows": rows},
        "matchups": {"season": season, "periods": matchup_periods},
        "teams": {"season": season, "teams": team_cards},
        "power": compute_power({"season": season, "periods": matchup_periods}, categories, team_cards),
        "_final": {
            "finished": finished,
            "ranks": {t["id"]: t["final_rank"] for t in teams.values()},
        },
    }


# ---------------------------------------------------------------- история

def update_history(result: dict) -> bool:
    """После финала добавляет сезон в history.json и архивирует данные в data/<год>/."""
    if not result["_final"]["finished"]:
        return False
    season = result["league"]["season"]
    path = DATA / "history.json"
    history = read_json(path, {"seasons": []})
    if any(s.get("season") == season for s in history.get("seasons", [])):
        return False
    ranks = result["_final"]["ranks"]
    rows = result["standings"]["rows"]
    by_id = {r["team_id"]: r for r in rows}

    def brief(team_id):
        r = by_id.get(team_id)
        return {"team": r["name"], "manager": r["manager"]} if r else None

    final_order = sorted(rows, key=lambda r: (ranks.get(r["team_id"]) or 999, r["rank"]))
    history.setdefault("seasons", []).append({
        "season": season,
        "label": result["league"]["season_label"],
        "champion": brief(next((tid for tid, rk in ranks.items() if rk == 1), None)),
        "runner_up": brief(next((tid for tid, rk in ranks.items() if rk == 2), None)),
        "regular_season_winner": brief(next((r["team_id"] for r in rows if r["rank"] == 1), None)),
        "final_standings": [
            {"place": ranks.get(r["team_id"]) or i, "team": r["name"], "manager": r["manager"],
             "regular_rank": r["rank"], "record": f"{r['wins']}-{r['losses']}-{r['ties']}"}
            for i, r in enumerate(final_order, 1)
        ],
        "archive": f"data/{season}/",
    })
    history["seasons"].sort(key=lambda s: s["season"], reverse=True)
    write_json(path, history)
    archive = DATA / str(season)
    archive.mkdir(parents=True, exist_ok=True)
    for name in ("league.json", "standings.json", "matchups.json", "teams.json", "power.json"):
        if (DATA / name).exists():
            shutil.copy2(DATA / name, archive / name)
    log(f"Сезон {season} завершён: запись добавлена в history.json, данные сохранены в data/{season}/")
    return True


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    missing = [k for k in ("LEAGUE_ID", "ESPN_S2", "SWID") if not os.environ.get(k, "").strip()]
    if missing:
        log("Не заданы переменные окружения: " + ", ".join(missing))
        return 2
    year = int(os.environ.get("SEASON_YEAR") or 0)
    if not year:
        now = datetime.now(ET)
        year = now.year + 1 if now.month >= 9 else now.year  # сезон 2026-27 в ESPN — это 2027

    from espn_api.basketball import League

    try:
        league = League(
            league_id=int(os.environ["LEAGUE_ID"].strip()), year=year,
            espn_s2=os.environ["ESPN_S2"].strip(), swid=os.environ["SWID"].strip(),
        )
        result = collect(league)
    except Exception as exc:
        # Текст исключения может содержать адрес запроса — печатаем только тип ошибки.
        log(f"ESPN не отдал данные сезона {year}: {type(exc).__name__}. Файлы в data/ оставлены без изменений.")
        return 1

    files = {name: result[name] for name in ("league", "standings", "matchups", "teams", "power")}
    old_league = read_json(DATA / "league.json", {})
    old_updated = old_league.pop("updated_at", None)
    changed = [n for n, payload in files.items()
               if (old_league if n == "league" else read_json(DATA / f"{n}.json", None)) != payload]
    if not changed and old_updated:
        log("Изменений в данных нет.")
    else:
        files["league"] = {"updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **files["league"]}
        for name in set(changed) | {"league"}:
            write_json(DATA / f"{name}.json", files[name])
        log("Обновлены файлы: " + ", ".join(sorted(f"{n}.json" for n in set(changed) | {"league"})))

    if not (DATA / "history.json").exists():
        write_json(DATA / "history.json", {"seasons": []})
    # Шаблон переопределений: новые команды добавляются, вписанные имена не трогаем.
    ov_path = DATA / "managers_override.json"
    ov = read_json(ov_path, {})
    new_ov = dict(ov)
    for t in result["teams"]["teams"]:
        cur = ov.get(str(t["id"]))
        name = cur.get("manager", "") if isinstance(cur, dict) else (cur or "")
        new_ov[str(t["id"])] = {"team": t["name"], "manager": name}
    if new_ov != ov or not ov_path.exists():
        write_json(ov_path, new_ov)
    update_history(result)
    lg = result["league"]
    log(f"Сезон {lg['season_label']}, фаза: {lg['phase']}, команд: {lg['team_count']}, текущий период: {lg['current_period']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
