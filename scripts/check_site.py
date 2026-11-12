"""Проверка качества: скриншоты страниц на контрольных ширинах, горизонтальный скролл, ссылки.

Запуск:  python scripts/check_site.py [--site _site] [--out screenshots] [--widths 375,1440]
Нужен Playwright:  pip install playwright && python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import functools
import http.server
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PAGES = ["index", "standings", "power", "matchups", "teams", "rules", "history"]
EXTRA_768 = {"standings", "matchups", "power"}


def serve(directory: Path):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass

    handler = functools.partial(Quiet, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=str(ROOT / "_site"))
    ap.add_argument("--out", default=str(ROOT / "screenshots"))
    ap.add_argument("--widths", default="360,375,414,768,1280,1440")
    ap.add_argument("--shots", default="375,1440", help="ширины, на которых сохранять скриншоты всех страниц")
    args = ap.parse_args()
    site, out = Path(args.site), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    widths = [int(w) for w in args.widths.split(",")]
    shots = {int(w) for w in args.shots.split(",")}
    srv = serve(site)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    problems = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in widths:
            mobile = width < 700
            ctx = browser.new_context(
                viewport={"width": width, "height": 800 if mobile else 900},
                device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile,
            )
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("requestfailed", lambda r: errors.append(f"не загрузилось: {r.url}"))
            for name in PAGES:
                resp = page.goto(f"{base}/{name}.html", wait_until="networkidle")
                if not resp or resp.status != 200:
                    problems.append(f"{name}@{width}: статус {resp.status if resp else '—'}")
                page.evaluate("document.fonts.ready")
                over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if over > 0:
                    wide = page.evaluate(
                        """() => [...document.querySelectorAll('body *')].filter(e => {
                              const r = e.getBoundingClientRect();
                              return r.right > document.documentElement.clientWidth + 1 && !e.closest('.table-scroll');
                           }).slice(0, 5).map(e => e.tagName + '.' + e.className)"""
                    )
                    problems.append(f"{name}@{width}: горизонтальный скролл страницы +{over}px: {wide}")
                small = page.evaluate(
                    """() => [...document.querySelectorAll('a, button, select, summary')].filter(e => {
                          const r = e.getBoundingClientRect();
                          if (!r.width || !r.height) return false;
                          if (e.closest('.charter, .charter-intro, .empty, .legend')) return false;
                          const min = e.closest('table, .period-nav, .subnav, .toc, .mu-foot') ? 35.5 : 43.5;
                          return r.height < min || (r.width < min && !e.closest('table'));
                       }).map(e => (e.className || e.tagName) + ' ' + Math.round(r = e.getBoundingClientRect().width) + 'x' + Math.round(e.getBoundingClientRect().height)).slice(0, 6)"""
                ) if mobile else []
                if small:
                    problems.append(f"{name}@{width}: мелкие элементы управления: {small}")
                if width in shots or (width == 768 and name in EXTRA_768):
                    page.screenshot(path=str(out / f"{name}-{width}.png"), full_page=True)
                if name == "index" and width == widths[0]:
                    hrefs = page.evaluate("[...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))")
                    for h in hrefs:
                        target = h.split("#")[0]
                        if target and not target.startswith("http") and not (site / target).exists():
                            problems.append(f"битая ссылка: {h}")
            # интерактив: меню, раскрытие категорий, переключение периода
            if mobile:
                page.goto(f"{base}/matchups.html", wait_until="networkidle")
                page.click(".nav-toggle")
                if not page.is_visible("#site-nav a[href='history.html']"):
                    problems.append(f"меню@{width}: не открывается")
                if width in shots:
                    page.screenshot(path=str(out / f"menu-{width}.png"))
                page.click(".nav-toggle")
                btn = page.locator(".period.is-active .mu-toggle").first
                if btn.count():
                    btn.click()
                    if not page.locator(".period.is-active .mu-cats.is-open").count():
                        problems.append(f"матчапы@{width}: категории не раскрываются")
                    if width in shots:
                        page.screenshot(path=str(out / f"matchups-open-{width}.png"), full_page=True)
            else:
                page.goto(f"{base}/matchups.html", wait_until="networkidle")
            if page.locator("#period-select").count():
                before = page.locator(".period.is-active").get_attribute("id")
                step = page.locator(".period-step[data-step='-1']")
                if step.is_enabled():
                    step.click()
                    after = page.locator(".period.is-active").get_attribute("id")
                    if after == before:
                        problems.append(f"матчапы@{width}: переключение периода не работает")
            if errors:
                problems.append(f"ошибки в консоли @{width}: {errors[:3]}")
            ctx.close()
        browser.close()
    srv.shutdown()

    if problems:
        print("Найдены проблемы:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"Проверка пройдена: ширины {widths}, страниц {len(PAGES)}. Скриншоты: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
