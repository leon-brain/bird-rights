"""Еженедельный рейтинг силы команд.

Идея: реальный счёт матчапа зависит от соперника, а сила — от того, скольких
команда обыграла бы. Для каждого завершённого матчапа сравниваем команду со всеми
остальными по каждой категории («каждый с каждым») и берём долю выигранных
категорий, ничья — половина. Получается «сила за матчап» от 0 до 100.

Рейтинг после матчапа k:
    WEIGHTS["form"]   × форма   — сила за последние FORM_WINDOW матчапов, свежие весят больше;
    WEIGHTS["season"] × сезон   — средняя сила за все матчапы;
    WEIGHTS["result"] × результат — реальный процент побед по категориям.
Веса можно менять здесь, рейтинг пересчитается за все недели при следующей выгрузке.
"""
from __future__ import annotations

import math

WEIGHTS = {"form": 0.60, "season": 0.25, "result": 0.15}
FORM_WINDOW = 3  # сколько последних матчапов входит в «форму»; веса 3 : 2 : 1


def _period_values(period: dict) -> dict:
    """{team_id: {категория: значение}} по одному матчап-периоду."""
    values = {}
    for m in period.get("matchups", []):
        for side in ("home", "away"):
            row = {c["key"]: c.get(f"{side}_raw") for c in m.get("cats", [])}
            if row and all(v is not None for v in row.values()):
                values[m[side]["team_id"]] = row
    return values


def _actual(period: dict) -> dict:
    """{team_id: (победы, поражения, ничьи)} по реальным матчапам периода."""
    out = {}
    for m in period.get("matchups", []):
        for side in ("home", "away"):
            s = m[side]
            out[s["team_id"]] = (s["wins"], s["losses"], s["ties"])
    return out


def _all_play(values: dict, categories: list) -> dict:
    """Сравнение каждого с каждым: {team_id: {"share": {кат: доля}, "w","l","t": ожидаемый счёт}}."""
    ids = list(values)
    n_opp = len(ids) - 1
    out = {}
    for i in ids:
        share, w, l, t = {}, 0.0, 0.0, 0.0
        for c in categories:
            key, rev = c["key"], c.get("reverse")
            cw = ct = 0
            for j in ids:
                if j == i:
                    continue
                a, b = values[i].get(key), values[j].get(key)
                if a is None or b is None:
                    continue
                if a == b:
                    ct += 1
                elif (a < b) if rev else (a > b):
                    cw += 1
            share[key] = (cw + ct / 2) / n_opp
            w += cw / n_opp
            t += ct / n_opp
            l += (n_opp - cw - ct) / n_opp
        out[i] = {"share": share, "w": w, "l": l, "t": t}
    return out


def _rank(scores: dict, reverse: bool = True) -> dict:
    """Места с учётом равенства (одинаковый результат — одинаковое место)."""
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=reverse)
    ranks, prev, place = {}, None, 0
    for i, (k, v) in enumerate(ordered, 1):
        if prev is None or abs(v - prev) > 1e-9:
            place = i
        ranks[k], prev = place, v
    return ranks


def compute_power(matchups: dict, categories: list, teams: list) -> dict:
    info = {t["id"]: t for t in teams}
    cat_keys = [c["key"] for c in categories]
    done = []
    for p in matchups.get("periods", []):
        if p.get("state") != "past":
            continue
        values = _period_values(p)
        if len(values) >= 2:
            done.append((p, _all_play(values, categories), _actual(p)))

    editions, prev_rank = [], {}
    hist: dict[int, list] = {}          # сила по матчапам
    exp = {}                            # накопленный ожидаемый счёт
    act = {}                            # накопленный реальный счёт
    cat_sum: dict[int, dict] = {}       # сумма долей по категориям
    for p, ap, actual in done:
        for tid, r in ap.items():
            hist.setdefault(tid, []).append(100 * sum(r["share"].values()) / max(len(r["share"]), 1))
            e = exp.setdefault(tid, [0.0, 0.0, 0.0])
            e[0] += r["w"]; e[1] += r["l"]; e[2] += r["t"]
            cs = cat_sum.setdefault(tid, {})
            for k, v in r["share"].items():
                cs[k] = cs.get(k, 0.0) + v
        for tid, (w, l, t) in actual.items():
            a = act.setdefault(tid, [0, 0, 0])
            a[0] += w; a[1] += l; a[2] += t

        rating, parts = {}, {}
        for tid, weeks in hist.items():
            recent = weeks[-FORM_WINDOW:][::-1]
            wts = list(range(FORM_WINDOW, FORM_WINDOW - len(recent), -1))
            form = sum(v * w for v, w in zip(recent, wts)) / sum(wts)
            season = sum(weeks) / len(weeks)
            a = act.get(tid, [0, 0, 0])
            games = sum(a)
            result = 100 * (a[0] + a[2] / 2) / games if games else season
            rating[tid] = WEIGHTS["form"] * form + WEIGHTS["season"] * season + WEIGHTS["result"] * result
            parts[tid] = (form, season, result, weeks[-1])

        ranks = _rank(rating)
        cat_ranks = {k: _rank({tid: cs.get(k, 0.0) for tid, cs in cat_sum.items()}) for k in cat_keys}
        rows = []
        for tid in sorted(rating, key=lambda t: (ranks[t], info.get(t, {}).get("name", ""))):
            form, season, result, week = parts[tid]
            e, a = exp[tid], act.get(tid, [0, 0, 0])
            t = info.get(tid, {})
            rows.append({
                "rank": ranks[tid],
                "prev_rank": prev_rank.get(tid),
                "move": (prev_rank[tid] - ranks[tid]) if tid in prev_rank else None,
                "team_id": tid,
                "name": t.get("name", "—"),
                "abbrev": t.get("abbrev", ""),
                "manager": t.get("manager", "—"),
                "rating": round(rating[tid], 1),
                "form": round(form, 1),
                "season": round(season, 1),
                "result": round(result, 1),
                "week": round(week, 1),
                "expected": f"{e[0]:.1f}-{e[1]:.1f}-{e[2]:.1f}",
                "expected_pct": round(100 * (e[0] + e[2] / 2) / max(sum(e), 1e-9), 1),
                "actual": f"{a[0]}-{a[1]}-{a[2]}",
                "luck": round((a[0] + a[2] / 2) - (e[0] + e[2] / 2), 1),
                "cat_ranks": [{"key": k, "rank": cat_ranks[k].get(tid)} for k in cat_keys],
            })
        editions.append({
            "period_id": p["id"], "label": p["label"], "start": p.get("start"), "end": p.get("end"),
            "is_playoff": p.get("is_playoff", False), "matchups_counted": len(hist[next(iter(hist))]) if hist else 0,
            "rows": rows,
        })
        prev_rank = ranks

    return {
        "season": matchups.get("season"),
        "weights": {k: round(v * 100) for k, v in WEIGHTS.items()},
        "form_window": FORM_WINDOW,
        "categories": cat_keys,
        "editions": editions,
        **compute_extras(matchups, categories, info, editions),
    }


# ---------------------------------------------------------------- прогноз, расписание, профиль, динамика

RECENCY = 0.8        # вес матчапа в прогнозе убывает в 0.8 раза с каждой неделей давности
STRONG_Z = 0.8       # отклонение от среднего по лиге, с которого категория считается сильной/слабой


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _fmt(key: str, value: float) -> str:
    if key.endswith("%"):
        text = f"{value:.3f}"
        return text[1:] if text.startswith("0") else text
    return str(int(round(value)))


def _weekly(period: dict, categories: list) -> dict:
    """Значения категорий, приведённые к 7 дням (проценты не масштабируются)."""
    days = period.get("days") or 7
    out = {}
    for tid, row in _period_values(period).items():
        out[tid] = {c["key"]: (row[c["key"]] if c["key"].endswith("%") else row[c["key"]] * 7 / days) for c in categories}
    return out


def _win_distribution(probs: list) -> tuple:
    """Вероятности победы, ничьей и поражения в матчапе по шансам в каждой категории."""
    dist = [1.0]
    for pr in probs:
        nxt = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nxt[k] += v * (1 - pr)
            nxt[k + 1] += v * pr
        dist = nxt
    n = len(probs)
    win = sum(v for k, v in enumerate(dist) if k * 2 > n)
    tie = sum(v for k, v in enumerate(dist) if k * 2 == n)
    return win, tie, 1 - win - tie


def compute_extras(matchups: dict, categories: list, info: dict, editions: list) -> dict:
    periods = matchups.get("periods", [])
    past = [p for p in periods if p.get("state") == "past" and len(_period_values(p)) >= 2]
    if not past or not editions:
        return {"forecast": None, "schedule": [], "profile": [], "trend": []}
    cat_keys = [c["key"] for c in categories]
    reverse = {c["key"]: bool(c.get("reverse")) for c in categories}
    weekly = [_weekly(p, categories) for p in past]
    latest = editions[-1]
    rank_of = {r["team_id"]: r["rank"] for r in latest["rows"]}
    rating_of = {r["team_id"]: r["rating"] for r in latest["rows"]}
    ids = [r["team_id"] for r in latest["rows"]]

    # средние по командам: обычное (профиль) и с упором на свежие матчапы (прогноз)
    mean, recent, n_weeks = {}, {}, {}
    for tid in ids:
        series = [w[tid] for w in weekly if tid in w]
        if not series:
            continue
        n_weeks[tid] = len(series)
        wts = [RECENCY ** (len(series) - 1 - i) for i in range(len(series))]
        mean[tid] = {k: sum(x[k] for x in series) / len(series) for k in cat_keys}
        recent[tid] = {k: sum(x[k] * w for x, w in zip(series, wts)) / sum(wts) for k in cat_keys}

    # разброс от недели к неделе (общий по лиге) и разброс между командами
    sd_week, sd_league, league_mean = {}, {}, {}
    for k in cat_keys:
        variances = []
        for tid in mean:
            series = [w[tid][k] for w in weekly if tid in w]
            if len(series) >= 2:
                m = sum(series) / len(series)
                variances.append(sum((x - m) ** 2 for x in series) / (len(series) - 1))
        vals = [mean[tid][k] for tid in mean]
        league_mean[k] = sum(vals) / len(vals)
        sd_league[k] = math.sqrt(sum((v - league_mean[k]) ** 2 for v in vals) / max(len(vals) - 1, 1)) or 1e-9
        sd_week[k] = math.sqrt(sum(variances) / len(variances)) if variances else sd_league[k]
        sd_week[k] = sd_week[k] or sd_league[k]

    # --- прогноз на текущий (или ближайший) матчап
    target = next((p for p in periods if p.get("state") == "current" and p.get("matchups")), None) \
        or next((p for p in periods if p.get("state") == "future" and p.get("matchups")), None)
    forecast = None
    if target:
        pairs = []
        for m in target["matchups"]:
            h, a = m["home"]["team_id"], m["away"]["team_id"]
            if h not in recent or a not in recent:
                continue
            n = min(n_weeks[h], n_weeks[a])
            cats, probs = [], []
            for k in cat_keys:
                diff = recent[h][k] - recent[a][k]
                if reverse[k]:
                    diff = -diff
                pr = _phi(diff / (sd_week[k] * math.sqrt(2 * (1 + 1 / n))))
                pr = min(max(pr, 0.03), 0.97)  # на короткой истории не обещаем 100%
                probs.append(pr)
                cats.append({"key": k, "home_pct": round(pr * 100), "home": _fmt(k, recent[h][k]), "away": _fmt(k, recent[a][k])})
            win, tie, loss = _win_distribution(probs)
            pairs.append({
                "home": {"team_id": h, "name": m["home"]["name"], "abbrev": m["home"]["abbrev"]},
                "away": {"team_id": a, "name": m["away"]["name"], "abbrev": m["away"]["abbrev"]},
                "home_exp": round(sum(probs), 1), "away_exp": round(len(probs) - sum(probs), 1),
                "home_win": round(win * 100), "tie": round(tie * 100), "away_win": 100 - round(win * 100) - round(tie * 100),
                "cats": cats,
            })
        if pairs:
            forecast = {"period_id": target["id"], "label": target["label"], "start": target.get("start"),
                        "end": target.get("end"), "state": target["state"], "based_on": len(past), "pairs": pairs}

    # --- сила расписания
    played, ahead, next_opp = {t: [] for t in ids}, {t: [] for t in ids}, {}
    for p in periods:
        bucket = played if p.get("state") == "past" else ahead
        for m in p.get("matchups", []):
            h, a = m["home"]["team_id"], m["away"]["team_id"]
            if h in bucket and a in bucket:
                bucket[h].append(a); bucket[a].append(h)
                if bucket is ahead and p.get("state") == "future":
                    next_opp.setdefault(h, a); next_opp.setdefault(a, h)

    def avg(opps):
        vals = [rating_of[o] for o in opps if o in rating_of]
        return round(sum(vals) / len(vals), 1) if vals else None

    schedule = [{
        "rank": rank_of[t], "team_id": t, "name": info.get(t, {}).get("name", "—"),
        "played": avg(played[t]), "played_n": len(played[t]),
        "ahead": avg(ahead[t]), "ahead_n": len(ahead[t]),
        "next": info.get(next_opp.get(t), {}).get("name"),
    } for t in ids]
    order = sorted((r for r in schedule if r["ahead"] is not None), key=lambda r: -r["ahead"])
    for i, r in enumerate(order, 1):
        r["ahead_rank"] = i

    # --- профиль по категориям
    profile = []
    for t in ids:
        if t not in mean:
            continue
        cells = []
        for k in cat_keys:
            z = (mean[t][k] - league_mean[k]) / sd_league[k]
            if reverse[k]:
                z = -z
            cells.append({"key": k, "value": _fmt(k, mean[t][k]), "raw": round(mean[t][k], 4), "z": round(z, 2)})
        strong = sorted((c for c in cells if c["z"] >= STRONG_Z), key=lambda c: -c["z"])
        weak = sorted((c for c in cells if c["z"] <= -STRONG_Z), key=lambda c: c["z"])
        profile.append({
            "rank": rank_of[t], "team_id": t, "name": info.get(t, {}).get("name", "—"), "cells": cells,
            "strong": [c["key"] for c in strong[:3]], "weak": [c["key"] for c in weak[:3]],
        })
    league_row = [{"key": k, "value": _fmt(k, league_mean[k])} for k in cat_keys]

    # --- динамика рейтинга
    trend = []
    for t in ids:
        points = []
        for e in editions:
            row = next((r for r in e["rows"] if r["team_id"] == t), None)
            if row:
                points.append({"period_id": e["period_id"], "label": e["label"], "rating": row["rating"], "rank": row["rank"]})
        trend.append({"team_id": t, "name": info.get(t, {}).get("name", "—"), "rank": rank_of[t], "points": points})

    return {"forecast": forecast, "schedule": schedule, "profile": profile, "league_avg": league_row, "trend": trend}
