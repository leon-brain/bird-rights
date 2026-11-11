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
    }
