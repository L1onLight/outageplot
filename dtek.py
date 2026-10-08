"""
Отримання графіка відключень з сайту ДТЕК КЕМ.
"""

from dataclasses import dataclass

from playwright.sync_api import sync_playwright

URL = "https://www.dtek-kem.com.ua/ua/shutdowns"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

_JS_FETCH = """async ([street, house]) => {
    const csrf = document.querySelector('meta[name=csrf-token]')?.content;
    const body = new URLSearchParams();
    body.append('method', 'getHomeNum');
    body.append('data[0][name]', 'street');
    body.append('data[0][value]', street);
    body.append('data[1][name]', 'updateFact');
    body.append('data[1][value]', DisconSchedule.fact?.update ?? '');
    const r = await fetch('/ua/ajax', {
        method: 'POST',
        body,
        headers: {'X-CSRF-Token': csrf, 'X-Requested-With': 'XMLHttpRequest'},
    });
    const homes = await r.json();
    return {
        home: homes.data?.[house] ?? null,
        houses: Object.keys(homes.data ?? {}),
        fact: DisconSchedule.fact,
        names: DisconSchedule.preset?.sch_names ?? {},
    };
}"""


@dataclass
class Schedule:
    lines: list[str]  # sub_type_reason, напр. ["GPV35.1", "GPV29.1"]
    names: dict[str, str]  # GPV35.1 -> "Черга 35.1"
    today: int  # unix-час початку сьогоднішньої доби
    tomorrow: int | None
    days: dict[int, dict[str, dict[str, str]]]  # день -> лінія -> {"1".."24": стан}
    updated: str  # "08.10.2026 08:01"
    home: dict

    def slots(self, day: int | None, line: str) -> dict[str, str] | None:
        if day is None:
            return None
        return self.days.get(day, {}).get(line)


def fetch(street: str, house: str) -> Schedule:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="uk-UA")
            page.goto(URL, wait_until="networkidle", timeout=90_000)
            page.wait_for_function(
                "typeof DisconSchedule !== 'undefined' && DisconSchedule.fact",
                timeout=60_000,
            )
            res = page.evaluate(_JS_FETCH, [street, house])
        finally:
            browser.close()

    if res["home"] is None:
        raise RuntimeError(f"Будинок {house!r} не знайдено на {street!r}. Є: {res['houses']}")

    fact = res["fact"]
    days = {int(k): v for k, v in fact["data"].items()}
    today = int(fact["today"])
    later = sorted(d for d in days if d > today)
    return Schedule(
        lines=list(res["home"].get("sub_type_reason") or []),
        names=res["names"],
        today=today,
        tomorrow=later[0] if later else None,
        days=days,
        updated=fact.get("update", ""),
        home=res["home"],
    )
