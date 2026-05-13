from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

MATCHES_URL = os.getenv("CONGOBET_MATCHES_URL", "https://www.congobet.net/virtual/category/instant-league/8035/matches")
RESULTS_URL = os.getenv("CONGOBET_RESULTS_URL", "https://www.congobet.net/virtual/category/instant-league/8035/results")
GAP_THRESHOLD_MINUTES = int(os.getenv("GAP_THRESHOLD_MINUTES", "3"))
MAX_RUNTIME_MINUTES = int(os.getenv("MAX_RUNTIME_MINUTES", "54"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
DEFAULT_TIMEOUT_MS = 10000
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
ODDS_DIR = DATA_DIR / "odds"
RESULTS_DIR = DATA_DIR / "results"
STATE_FILE = DATA_DIR / "state" / "runtime_state.json"

for directory in (ODDS_DIR, RESULTS_DIR, STATE_FILE.parent):
    directory.mkdir(parents=True, exist_ok=True)


@dataclass
class RoundRef:
    index: int
    label: str
    at_utc: datetime


@dataclass
class OddsPlan:
    visible_rounds: list[RoundRef]
    rounds_to_save: list[RoundRef]
    trigger_round: RoundRef | None
    next_odds_run_at_utc: datetime


@dataclass
class OddsSnapshot:
    competition: str
    plan: OddsPlan
    rounds: list[dict[str, Any]]
    saved_file: Path | None = None
    snapshot_hash: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def parse_score(score_text: str) -> tuple[int, int]:
    match = re.search(r"(\d+)\s*[:\-]\s*(\d+)", clean_text(score_text))
    if not match:
        raise ValueError(f"Score introuvable dans: {score_text!r}")
    return int(match.group(1)), int(match.group(2))


def derive_gng(home_score: int, away_score: int) -> str:
    return "Oui" if home_score > 0 and away_score > 0 else "Non"


def parse_minutes(value: str) -> list[str]:
    value = clean_text(value)
    if not value:
        return []
    return re.findall(r"\d+'(?:\+\d+)?", value)


def hhmm_to_minutes(label: str) -> int:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", clean_text(label))
    if not match:
        raise ValueError(f"Format horaire inattendu: {label!r}")
    hours = int(match.group(1))
    minutes = int(match.group(2))
    if hours > 23 or minutes > 59:
        raise ValueError(f"Horaire invalide: {label!r}")
    return hours * 60 + minutes


def normalize_round_schedule(labels: list[str], now: datetime) -> list[RoundRef]:
    if not labels:
        return []

    anchor_index = 1 if len(labels) > 1 else 0
    anchor_minutes = hhmm_to_minutes(labels[anchor_index])
    base_today = datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc)
    anchor_candidate = base_today + timedelta(minutes=anchor_minutes)

    # Les rounds utiles sont censés être imminents ; s'ils semblent passés,
    # on les projette au jour suivant.
    if anchor_candidate < now - timedelta(minutes=5):
        anchor_candidate += timedelta(days=1)

    rounds: list[RoundRef | None] = [None] * len(labels)
    rounds[anchor_index] = RoundRef(index=anchor_index, label=labels[anchor_index], at_utc=anchor_candidate)

    previous = rounds[anchor_index]
    assert previous is not None

    for idx in range(anchor_index + 1, len(labels)):
        current_minutes = hhmm_to_minutes(labels[idx])
        previous_minutes = hhmm_to_minutes(labels[idx - 1])
        delta = current_minutes - previous_minutes
        if delta < 0:
            delta += 24 * 60
        previous = RoundRef(index=idx, label=labels[idx], at_utc=previous.at_utc + timedelta(minutes=delta))
        rounds[idx] = previous

    current = rounds[anchor_index]
    assert current is not None
    for idx in range(anchor_index - 1, -1, -1):
        current_minutes = hhmm_to_minutes(labels[idx + 1])
        previous_minutes = hhmm_to_minutes(labels[idx])
        delta = current_minutes - previous_minutes
        if delta < 0:
            delta += 24 * 60
        current = RoundRef(index=idx, label=labels[idx], at_utc=current.at_utc - timedelta(minutes=delta))
        rounds[idx] = current

    return [item for item in rounds if item is not None]


def build_odds_plan(rounds: list[RoundRef], gap_threshold_minutes: int) -> OddsPlan:
    if not rounds:
        raise ValueError("Aucun round détecté sur la page des cotes.")

    trigger_round: RoundRef | None = None
    save_until_index = len(rounds) - 1

    # Le premier round affiché n'est jamais sauvegardé et ne doit pas piloter
    # la détection de rupture, car il peut représenter un état déjà démarré.
    for idx in range(1, len(rounds) - 1):
        gap_minutes = int((rounds[idx + 1].at_utc - rounds[idx].at_utc).total_seconds() // 60)
        if gap_minutes >= gap_threshold_minutes:
            trigger_round = rounds[idx]
            save_until_index = idx - 1
            break

    rounds_to_save = [rnd for rnd in rounds[1:] if rnd.index <= save_until_index]

    if trigger_round is not None:
        next_odds_run_at_utc = trigger_round.at_utc
    elif rounds:
        next_odds_run_at_utc = rounds[-1].at_utc + timedelta(seconds=15)
    else:
        next_odds_run_at_utc = utc_now() + timedelta(minutes=2)

    return OddsPlan(
        visible_rounds=rounds,
        rounds_to_save=rounds_to_save,
        trigger_round=trigger_round,
        next_odds_run_at_utc=next_odds_run_at_utc,
    )


def payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def slug_time(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "recent_odds_hashes": [],
            "recent_results_hashes": [],
            "last_odds_started_at_utc": None,
            "last_odds_saved_at_utc": None,
            "last_results_saved_at_utc": None,
            "last_results_trigger_time_utc": None,
            "last_loop_finished_at_utc": None,
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def remember_hash(state: dict[str, Any], key: str, value: str, limit: int = 200) -> None:
    bucket = list(state.get(key, []))
    if value in bucket:
        bucket.remove(value)
    bucket.append(value)
    state[key] = bucket[-limit:]


def persist_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_day_dir(root: Path, dt: datetime) -> Path:
    folder = root / dt.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


async def safe_click(locator, timeout: int = 8000) -> bool:
    try:
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click(timeout=timeout)
        return True
    except Exception:
        return False


async def wait_for_matches(page: Page, selector: str = "div.match", minimum: int = 1, timeout_ms: int = 15000) -> None:
    end_time = time.time() + timeout_ms / 1000
    last_error: Exception | None = None
    while time.time() < end_time:
        try:
            count = await page.locator(selector).count()
            if count >= minimum:
                return
        except Exception as exc:
            last_error = exc
        await page.wait_for_timeout(300)
    if last_error:
        raise last_error
    raise TimeoutError(f"Les matchs ne se sont pas chargés à temps pour le sélecteur: {selector}")


async def wait_for_results(page: Page, minimum: int = 1, timeout_ms: int = 20000) -> None:
    deadline = time.time() + timeout_ms / 1000
    selector = "hg-instant-league-results .result-container"
    while time.time() < deadline:
        count = await page.locator(selector).count()
        if count >= minimum:
            return
        await asyncio.sleep(0.3)
    raise TimeoutError("Les résultats ne se sont pas chargés à temps.")


def get_round_tabs(page: Page):
    return page.locator("hg-instant-league-round-picker li")


async def get_round_time(tab) -> str:
    try:
        return clean_text(await tab.locator(".time").inner_text(timeout=3000))
    except Exception:
        return clean_text(await tab.inner_text(timeout=3000))


async def ensure_market_selected(page: Page, market_label: str) -> None:
    active_button = page.locator("hg-event-bet-type-picker button.active", has_text=market_label)
    if await active_button.count() > 0:
        return

    visible_button = page.locator("hg-event-bet-type-picker button", has_text=market_label)
    if await visible_button.count() > 0 and await safe_click(visible_button.first):
        await page.wait_for_timeout(1200)
        return

    select_box = page.locator("hg-event-bet-type-picker hg-select .selected").first
    if not await safe_click(select_box):
        raise RuntimeError(f"Impossible d'ouvrir le sélecteur pour le marché {market_label}.")

    option = page.locator("hg-event-bet-type-picker hg-select .dropdown .option", has_text=market_label).first
    if not await safe_click(option, timeout=10000):
        raise RuntimeError(f"Impossible de sélectionner le marché {market_label}.")

    await page.wait_for_timeout(1500)


async def extract_gng_labels(page: Page) -> list[str]:
    candidates: list[str] = []
    selectors = [
        "div[class*='header'] span",
        "div[class*='header'] div",
    ]
    for selector in selectors:
        try:
            texts = await page.locator(selector).all_inner_texts()
        except Exception:
            continue
        for text in texts:
            cleaned = clean_text(text)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)

    filtered = [x for x in candidates if x.lower() in {"oui", "non"}]
    if len(filtered) >= 2:
        return filtered[:2]
    return ["Oui", "Non"]


async def extract_1x2_for_current_round(page: Page, round_time: str, round_index: int) -> list[dict[str, Any]]:
    match_locator = page.locator("div.match.bet-type-1x2")
    count = await match_locator.count()
    rows: list[dict[str, Any]] = []

    for i in range(count):
        card = match_locator.nth(i)
        team_spans = card.locator(".teams span")
        odd_spans = card.locator("span.odds")

        teams = []
        for j in range(await team_spans.count()):
            txt = clean_text(await team_spans.nth(j).inner_text())
            if txt:
                teams.append(txt)

        odds = []
        for j in range(await odd_spans.count()):
            txt = clean_text(await odd_spans.nth(j).inner_text())
            if txt:
                odds.append(txt)

        if len(teams) >= 2 and len(odds) >= 3:
            rows.append(
                {
                    "unique_key": f"{round_time}|{teams[0]}|{teams[1]}",
                    "round_index": round_index,
                    "round_time": round_time,
                    "teams": {"home": teams[0], "away": teams[1]},
                    "market": "1X2",
                    "odds": {"1": odds[0], "X": odds[1], "2": odds[2]},
                }
            )

    return rows


async def extract_gng_for_current_round(page: Page, round_time: str, round_index: int) -> list[dict[str, Any]]:
    labels = await extract_gng_labels(page)
    cards = page.locator("div.match")
    rows: list[dict[str, Any]] = []

    for idx in range(await cards.count()):
        card = cards.nth(idx)
        teams = [clean_text(x) for x in await card.locator(".teams span").all_inner_texts() if clean_text(x)]
        odds = [clean_text(x) for x in await card.locator("hg-event-bet-type-item .odds").all_inner_texts() if clean_text(x)]

        if len(teams) < 2 or len(odds) < 2:
            continue

        rows.append(
            {
                "unique_key": f"{round_time}|{teams[0]}|{teams[1]}",
                "round_index": round_index,
                "round_time": round_time,
                "teams": {"home": teams[0], "away": teams[1]},
                "market": "G/NG",
                "odds": {labels[0]: odds[0], labels[1]: odds[1]},
            }
        )

    return rows


async def extract_competition_from_matches(page: Page) -> str:
    title_locator = page.locator("div.title-wrapper span").first
    if await page.locator("div.title-wrapper span").count() > 0:
        return clean_text(await title_locator.inner_text())
    return "Instant League"


def merge_market_rows(rows_1x2: list[dict[str, Any]], rows_gng: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for row in rows_1x2 + rows_gng:
        key = row["unique_key"]
        if key not in merged:
            merged[key] = {
                "unique_key": key,
                "round_index": row["round_index"],
                "round_time": row["round_time"],
                "teams": row["teams"],
                "markets": {},
            }
        merged[key]["markets"][row["market"]] = row["odds"]

    return list(merged.values())


async def extract_round_combined(page: Page, round_index: int) -> dict[str, Any]:
    tabs = get_round_tabs(page)
    total_tabs = await tabs.count()
    if round_index >= total_tabs:
        raise IndexError(f"round_index {round_index} hors limite ({total_tabs})")

    tab = tabs.nth(round_index)
    round_time = await get_round_time(tab)
    if not round_time:
        round_time = f"index_{round_index}"

    if not await safe_click(tab):
        await safe_click(tab.locator(".infos").first)

    await page.wait_for_timeout(1200)
    await wait_for_matches(page)

    await ensure_market_selected(page, "1X2")
    await wait_for_matches(page, selector="div.match.bet-type-1x2")
    rows_1x2 = await extract_1x2_for_current_round(page, round_time, round_index)

    await ensure_market_selected(page, "G/NG")
    await wait_for_matches(page)
    rows_gng = await extract_gng_for_current_round(page, round_time, round_index)

    matches = merge_market_rows(rows_1x2, rows_gng)

    return {
        "round_index": round_index,
        "round_time": round_time,
        "matches_count": len(matches),
        "matches": matches,
    }


async def read_visible_rounds(page: Page) -> list[str]:
    tabs = get_round_tabs(page)
    count = await tabs.count()
    labels: list[str] = []
    for index in range(count):
        label = await get_round_time(tabs.nth(index))
        if re.fullmatch(r"\d{1,2}:\d{2}", label):
            labels.append(label)
    return labels


async def scrape_odds_snapshot() -> OddsSnapshot:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        page = await browser.new_page(locale="fr-FR", viewport={"width": 1600, "height": 2400})
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        await page.goto(MATCHES_URL, wait_until="networkidle", timeout=120000)
        await page.wait_for_timeout(3000)
        await wait_for_matches(page)
        competition = await extract_competition_from_matches(page)
        labels = await read_visible_rounds(page)
        plan = build_odds_plan(normalize_round_schedule(labels, utc_now()), GAP_THRESHOLD_MINUTES)

        rounds_payload: list[dict[str, Any]] = []
        for round_ref in plan.rounds_to_save:
            try:
                round_data = await extract_round_combined(page, round_ref.index)
                round_data["round_at_utc"] = round_ref.at_utc.isoformat()
                rounds_payload.append(round_data)
                print(f"[ODDS] round={round_ref.label} matches={round_data['matches_count']}")
            except PlaywrightTimeoutError as exc:
                print(f"[ODDS][WARN] timeout round={round_ref.label}: {exc}")
            except Exception as exc:
                print(f"[ODDS][WARN] erreur round={round_ref.label}: {exc}")

        await browser.close()

    snapshot_body = {
        "source": {
            "site": "CongoBet",
            "competition": competition,
            "url": MATCHES_URL,
            "markets": ["1X2", "G/NG"],
            "timezone": "UTC+00:00",
        },
        "metadata": {
            "scraped_at_utc": iso_now(),
            "visible_rounds": [
                {
                    "index": r.index,
                    "round_time": r.label,
                    "round_at_utc": r.at_utc.isoformat(),
                }
                for r in plan.visible_rounds
            ],
            "saved_rounds": [
                {
                    "index": r.index,
                    "round_time": r.label,
                    "round_at_utc": r.at_utc.isoformat(),
                }
                for r in plan.rounds_to_save
            ],
            "first_round_skipped": plan.visible_rounds[0].label if plan.visible_rounds else None,
            "gap_threshold_minutes": GAP_THRESHOLD_MINUTES,
            "results_trigger_round_time": plan.trigger_round.label if plan.trigger_round else None,
            "results_trigger_at_utc": plan.trigger_round.at_utc.isoformat() if plan.trigger_round else None,
            "next_odds_run_at_utc": plan.next_odds_run_at_utc.isoformat(),
            "rounds_saved_count": len(rounds_payload),
            "matches_saved_count": sum(x["matches_count"] for x in rounds_payload),
        },
        "rounds": rounds_payload,
        "matches": [match for round_item in rounds_payload for match in round_item.get("matches", [])],
    }
    snapshot_hash = payload_hash({"rounds": snapshot_body["rounds"], "saved_rounds": snapshot_body["metadata"]["saved_rounds"]})
    return OddsSnapshot(competition=competition, plan=plan, rounds=rounds_payload, snapshot_hash=snapshot_hash)


async def click_show_more_until_end(page: Page) -> int:
    clicks = 0
    results_selector = "hg-instant-league-results .result-container"

    while True:
        button = page.locator("text=/Afficher plus/i")
        if await button.count() == 0:
            break

        before_count = await page.locator(results_selector).count()
        current_button = button.first
        await current_button.scroll_into_view_if_needed()
        await current_button.click(timeout=10000)
        clicks += 1

        deadline = time.time() + 20
        while time.time() < deadline:
            after_count = await page.locator(results_selector).count()
            if after_count > before_count or await page.locator("text=/Afficher plus/i").count() == 0:
                break
            await asyncio.sleep(0.4)

        await asyncio.sleep(1.2)

    return clicks


async def extract_competition_from_results(page: Page) -> str:
    body_text = clean_text(await page.locator("body").inner_text())
    lines = [clean_text(line) for line in body_text.splitlines() if clean_text(line)]
    for idx, line in enumerate(lines):
        if line.upper() == "RÉSULTATS" and idx > 0:
            previous = clean_text(lines[idx - 1])
            if previous and previous.upper() not in {"VIRTUEL", "PROMOS", "FAQ"}:
                return previous

    candidates = [
        "div.title-wrapper span",
        "hg-entrypoint-title .title-wrapper span",
        "hg-entrypoint-title span",
    ]
    for selector in candidates:
        locator = page.locator(selector)
        if await locator.count() > 0:
            texts = [clean_text(x) for x in await locator.all_inner_texts() if clean_text(x)]
            for text in texts:
                if text and text.upper() not in {"RÉSULTATS", "MATCHS", "CLASSEMENT"}:
                    return text

    return "Instant League"


async def extract_result_rows(page: Page) -> list[dict[str, Any]]:
    containers = page.locator("hg-instant-league-results .result-container")
    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for i in range(await containers.count()):
        container = containers.nth(i)
        round_label = clean_text(await container.locator(".header").inner_text())
        matchday_match = re.search(r"Journée\s+(\d+)", round_label, flags=re.IGNORECASE)
        matchday = int(matchday_match.group(1)) if matchday_match else None
        round_time = clean_text(round_label.split("-", 1)[1]) if "-" in round_label else ""

        rows = container.locator(".match-results .row")
        for j in range(await rows.count()):
            row = rows.nth(j)
            teams = [clean_text(x) for x in await row.locator(".team span").all_inner_texts() if clean_text(x)]
            if len(teams) < 2:
                continue

            score_text = clean_text(await row.locator(".match-score").inner_text())
            halftime_text = clean_text(await row.locator(".halfTime-score").inner_text()).replace("MT:", "").strip()
            home_score, away_score = parse_score(score_text)
            home_ht, away_ht = parse_score(halftime_text) if halftime_text else (None, None)

            home_goal_minutes = []
            away_goal_minutes = []
            try:
                home_goal_minutes = parse_minutes(await row.locator(".haltTime-goals.home span").inner_text())
            except Exception:
                pass
            try:
                away_goal_minutes = parse_minutes(await row.locator(".haltTime-goals.away span").inner_text())
            except Exception:
                pass

            gng_result = derive_gng(home_score, away_score)
            unique_key = f"{matchday}|{teams[0]}|{teams[1]}|{score_text}"
            if unique_key in seen_keys:
                continue
            seen_keys.add(unique_key)

            records.append(
                {
                    "unique_key": unique_key,
                    "round_label": round_label,
                    "matchday": matchday,
                    "round_time": round_time,
                    "home_team": teams[0],
                    "away_team": teams[1],
                    "score": score_text,
                    "home_score": home_score,
                    "away_score": away_score,
                    "halftime_score": halftime_text,
                    "home_halftime_score": home_ht,
                    "away_halftime_score": away_ht,
                    "home_goal_minutes": home_goal_minutes,
                    "away_goal_minutes": away_goal_minutes,
                    "both_teams_scored": gng_result == "Oui",
                    "gng_result": gng_result,
                }
            )

    return records


async def scrape_results_snapshot(trigger_round: RoundRef | None) -> dict[str, Any]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1600, "height": 2600})
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        await page.goto(RESULTS_URL, wait_until="networkidle", timeout=120000)
        await asyncio.sleep(3)
        await wait_for_results(page)
        competition = await extract_competition_from_results(page)
        show_more_clicks = await click_show_more_until_end(page)
        await wait_for_results(page, minimum=1)

        records = await extract_result_rows(page)
        round_labels = [clean_text(x) for x in await page.locator("hg-instant-league-results .result-container .header").all_inner_texts()]
        await browser.close()

    payload = {
        "source": {
            "site": "CongoBet",
            "url": RESULTS_URL,
            "competition": competition,
            "category": "Instant League",
            "market": "G/NG (résultat dérivé depuis le score final)",
            "rule": "Oui si les deux équipes marquent au moins un but, sinon Non.",
            "timezone": "UTC+00:00",
        },
        "metadata": {
            "scraped_at_utc": iso_now(),
            "trigger_round_time": trigger_round.label if trigger_round else None,
            "trigger_round_at_utc": trigger_round.at_utc.isoformat() if trigger_round else None,
            "show_more_clicks": show_more_clicks,
            "rounds_count": len(round_labels),
            "round_labels": round_labels,
            "records_count": len(records),
            "deduplication": "unique_key = matchday|home_team|away_team|score",
        },
        "matches": records,
    }
    return payload


def save_odds_snapshot(snapshot: OddsSnapshot, state: dict[str, Any]) -> Path | None:
    payload = {
        "source": {
            "site": "CongoBet",
            "competition": snapshot.competition,
            "url": MATCHES_URL,
            "markets": ["1X2", "G/NG"],
            "timezone": "UTC+00:00",
        },
        "metadata": {
            "scraped_at_utc": iso_now(),
            "visible_rounds": [
                {"index": r.index, "round_time": r.label, "round_at_utc": r.at_utc.isoformat()} for r in snapshot.plan.visible_rounds
            ],
            "saved_rounds": [
                {"index": r.index, "round_time": r.label, "round_at_utc": r.at_utc.isoformat()} for r in snapshot.plan.rounds_to_save
            ],
            "first_round_skipped": snapshot.plan.visible_rounds[0].label if snapshot.plan.visible_rounds else None,
            "gap_threshold_minutes": GAP_THRESHOLD_MINUTES,
            "results_trigger_round_time": snapshot.plan.trigger_round.label if snapshot.plan.trigger_round else None,
            "results_trigger_at_utc": snapshot.plan.trigger_round.at_utc.isoformat() if snapshot.plan.trigger_round else None,
            "next_odds_run_at_utc": snapshot.plan.next_odds_run_at_utc.isoformat(),
            "rounds_saved_count": len(snapshot.rounds),
            "matches_saved_count": sum(item["matches_count"] for item in snapshot.rounds),
        },
        "rounds": snapshot.rounds,
        "matches": [match for round_item in snapshot.rounds for match in round_item.get("matches", [])],
    }

    if not snapshot.rounds:
        print("[ODDS] aucun round à enregistrer.")
        return None
    if snapshot.snapshot_hash in state.get("recent_odds_hashes", []):
        print("[ODDS] snapshot déjà connu, pas de nouveau fichier.")
        return None

    folder = make_day_dir(ODDS_DIR, utc_now())
    first_saved = snapshot.plan.rounds_to_save[0]
    last_saved = snapshot.plan.rounds_to_save[-1]
    trigger = snapshot.plan.trigger_round
    filename = f"odds_{slug_time(utc_now())}_from_{first_saved.label.replace(':', '-')}_to_{last_saved.label.replace(':', '-')}"
    if trigger:
        filename += f"_trigger_{trigger.label.replace(':', '-')}"
    filename += ".json"

    path = persist_json(folder / filename, payload)
    remember_hash(state, "recent_odds_hashes", snapshot.snapshot_hash)
    state["last_odds_saved_at_utc"] = payload["metadata"]["scraped_at_utc"]
    print(f"[ODDS] fichier sauvegardé: {path.relative_to(ROOT)}")
    return path


def save_results_snapshot(payload: dict[str, Any], state: dict[str, Any]) -> Path | None:
    signature = payload_hash({"matches": payload["matches"], "trigger_round_time": payload["metadata"]["trigger_round_time"]})
    if signature in state.get("recent_results_hashes", []):
        print("[RESULTS] snapshot déjà connu, pas de nouveau fichier.")
        return None

    trigger_round_time = payload["metadata"].get("trigger_round_time") or "na"
    folder = make_day_dir(RESULTS_DIR, utc_now())
    filename = f"results_{slug_time(utc_now())}_trigger_{str(trigger_round_time).replace(':', '-')}.json"
    path = persist_json(folder / filename, payload)
    remember_hash(state, "recent_results_hashes", signature)
    state["last_results_saved_at_utc"] = payload["metadata"]["scraped_at_utc"]
    state["last_results_trigger_time_utc"] = payload["metadata"].get("trigger_round_at_utc")
    print(f"[RESULTS] fichier sauvegardé: {path.relative_to(ROOT)}")
    return path


async def sleep_until(target: datetime, hard_deadline: datetime) -> bool:
    now = utc_now()
    if target <= now:
        return True

    while True:
        now = utc_now()
        if now >= target:
            return True
        if now >= hard_deadline:
            return False
        remaining = (target - now).total_seconds()
        await asyncio.sleep(min(remaining, 15))


async def orchestrate() -> None:
    started_at = utc_now()
    hard_deadline = started_at + timedelta(minutes=MAX_RUNTIME_MINUTES)
    state = load_state()

    while utc_now() < hard_deadline:
        loop_started_at = utc_now()
        state["last_odds_started_at_utc"] = loop_started_at.isoformat()
        save_state(state)
        print(f"[LOOP] démarrage: {loop_started_at.isoformat()}")

        odds_snapshot = await scrape_odds_snapshot()
        save_odds_snapshot(odds_snapshot, state)
        save_state(state)

        if odds_snapshot.plan.trigger_round is not None:
            trigger_round = odds_snapshot.plan.trigger_round
            print(f"[PLAN] bascule résultats à {trigger_round.at_utc.isoformat()} (round {trigger_round.label})")
            can_wait = await sleep_until(trigger_round.at_utc, hard_deadline)
            if not can_wait:
                break
            results_payload = await scrape_results_snapshot(trigger_round)
            save_results_snapshot(results_payload, state)
            save_state(state)
            await asyncio.sleep(10)
        else:
            next_run = odds_snapshot.plan.next_odds_run_at_utc
            print(f"[PLAN] prochain scraping cotes à {next_run.isoformat()}")
            can_wait = await sleep_until(next_run, hard_deadline)
            if not can_wait:
                break

        state["last_loop_finished_at_utc"] = utc_now().isoformat()
        save_state(state)

    print("[DONE] fenêtre GitHub Actions terminée.")


if __name__ == "__main__":
    asyncio.run(orchestrate())
