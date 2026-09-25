#!/usr/bin/env python3
"""
GamePal Ratings Enrichment
---------------------------
Reads game_list.json (your library), fetches rating/genre/plot for each
title from the RAWG API, caches results in SQLite so reruns are cheap,
and generates a self-contained dashboard.html to browse the results.

Setup:
    pip install -r requirements.txt
    export RAWG_API_KEY=your_key_here      # https://rawg.io/apidocs
    python enrich.py

Safe to run repeatedly — already-cached titles are skipped unless
--refresh is passed.
"""

import json
import os
import re
import sqlite3
import sys
import time
import argparse
from pathlib import Path
from difflib import SequenceMatcher

import requests

ROOT = Path(__file__).parent
GAME_LIST_PATH = ROOT / "game_list.json"
DB_PATH = ROOT / "cache.sqlite"
UNMATCHED_PATH = ROOT / "unmatched.json"
DASHBOARD_TEMPLATE = ROOT / "dashboard_template.html"
DASHBOARD_OUTPUT = ROOT / "index.html"

RAWG_BASE = "https://api.rawg.io/api"
RAWG_API_KEY = os.environ.get("RAWG_API_KEY")

MAX_RETRIES = 3
BACKOFF_SECONDS = 2
REQUEST_DELAY = 0.6  # be polite to the free tier

# Suffixes that break title matching if left in — stripped before search,
# kept separately as an "edition" tag.
EDITION_SUFFIXES = [
    "gold edition", "definitive edition", "ultimate edition",
    "remastered", "remaster", "the complete edition", "complete edition",
    "goty edition", "game of the year edition",
]


def normalize_title(raw_title):
    """Split a library title into (search_title, edition_tag)."""
    title = raw_title
    edition = ""
    lowered = title.lower()
    for suffix in EDITION_SUFFIXES:
        pattern = re.compile(r"[:\-]?\s*" + re.escape(suffix) + r"\s*$", re.IGNORECASE)
        if pattern.search(lowered):
            title = pattern.sub("", title).strip(" :-")
            edition = suffix.title()
            break
    return title, edition


def similarity(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS games (
            title TEXT PRIMARY KEY,
            platform TEXT,
            rawg_id INTEGER,
            matched_name TEXT,
            rating REAL,
            rating_top INTEGER,
            genres TEXT,
            plot TEXT,
            released TEXT,
            match_status TEXT,
            match_score REAL,
            last_updated TEXT
        )
    """)
    conn.commit()
    return conn


def rawg_search(query):
    """Query RAWG's search endpoint with retry + backoff. Returns list of results or None on failure."""
    if not RAWG_API_KEY:
        raise RuntimeError("RAWG_API_KEY is not set. Get a free key at https://rawg.io/apidocs")

    params = {"key": RAWG_API_KEY, "search": query, "page_size": 5}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(f"{RAWG_BASE}/games", params=params, timeout=10)
            if resp.status_code == 429:
                wait = BACKOFF_SECONDS * attempt
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json().get("results", [])
        except requests.RequestException as e:
            wait = BACKOFF_SECONDS * attempt
            print(f"  network error ({e}), retry {attempt}/{MAX_RETRIES} in {wait}s")
            time.sleep(wait)
    return None  # all retries exhausted


def rawg_details(game_id):
    """Fetch full detail (for description) for a matched game."""
    params = {"key": RAWG_API_KEY}
    try:
        resp = requests.get(f"{RAWG_BASE}/games/{game_id}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return {}


def clean_plot(html_description):
    """RAWG descriptions are long HTML — trim to a tight 1-2 line summary."""
    if not html_description:
        return ""
    text = re.sub("<[^<]+?>", "", html_description)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    summary = ""
    for s in sentences:
        if len(summary) + len(s) > 220:
            break
        summary += (" " if summary else "") + s
    return summary or text[:220]


def find_best_match(search_title, candidates):
    """Score candidates by name similarity, return (best, score, is_ambiguous)."""
    if not candidates:
        return None, 0.0, False
    scored = sorted(
        ((c, similarity(search_title, c["name"])) for c in candidates),
        key=lambda x: x[1], reverse=True,
    )
    best, best_score = scored[0]
    ambiguous = len(scored) > 1 and (best_score - scored[1][1]) < 0.08 and best_score < 0.92
    return best, best_score, ambiguous


def enrich_game(conn, entry, force_refresh, unmatched, ambiguous_log):
    title, platform = entry["title"], entry.get("platform", "")

    if not force_refresh:
        row = conn.execute("SELECT match_status FROM games WHERE title = ?", (title,)).fetchone()
        if row and row[0] == "matched":
            print(f"  skip (cached): {title}")
            return

    search_title, edition = normalize_title(title)
    print(f"  fetching: {title}" + (f"  [edition: {edition}]" if edition else ""))

    time.sleep(REQUEST_DELAY)
    results = rawg_search(search_title)

    if results is None:
        conn.execute(
            "INSERT OR REPLACE INTO games (title, platform, match_status, last_updated) VALUES (?, ?, ?, datetime('now'))",
            (title, platform, "error"),
        )
        conn.commit()
        unmatched.append({"title": title, "reason": "network/API error after retries"})
        return

    best, score, ambiguous = find_best_match(search_title, results)

    if best is None or score < 0.45:
        conn.execute(
            "INSERT OR REPLACE INTO games (title, platform, match_status, last_updated) VALUES (?, ?, ?, datetime('now'))",
            (title, platform, "unmatched"),
        )
        conn.commit()
        unmatched.append({"title": title, "reason": "no confident match found"})
        return

    if ambiguous:
        ambiguous_log.append({
            "title": title,
            "candidates": [{"name": c["name"], "id": c["id"], "score": round(s, 2)} for c, s in
                            sorted(((c, similarity(search_title, c["name"])) for c in results),
                                   key=lambda x: x[1], reverse=True)[:3]],
        })
        # still store the top guess, but flag it for manual confirmation
        match_status = "ambiguous"
    else:
        match_status = "matched"

    details = rawg_details(best["id"])
    genres = ", ".join(g["name"] for g in best.get("genres", []))
    plot = clean_plot(details.get("description_raw") or details.get("description", ""))

    conn.execute("""
        INSERT OR REPLACE INTO games
        (title, platform, rawg_id, matched_name, rating, rating_top, genres, plot, released, match_status, match_score, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        title, platform, best["id"], best["name"], best.get("rating"), best.get("rating_top"),
        genres, plot, best.get("released"), match_status, round(score, 3),
    ))
    conn.commit()


def run_enrichment(force_refresh=False):
    if not GAME_LIST_PATH.exists():
        sys.exit(f"Missing {GAME_LIST_PATH} — create your master game list first.")

    entries = json.loads(GAME_LIST_PATH.read_text())
    conn = init_db()
    unmatched, ambiguous_log = [], []

    print(f"Enriching {len(entries)} games...")
    for entry in entries:
        try:
            enrich_game(conn, entry, force_refresh, unmatched, ambiguous_log)
        except Exception as e:
            print(f"  ERROR on '{entry.get('title')}': {e}")
            unmatched.append({"title": entry.get("title"), "reason": str(e)})

    if unmatched:
        UNMATCHED_PATH.write_text(json.dumps(unmatched, indent=2))
        print(f"\n{len(unmatched)} titles need attention — see {UNMATCHED_PATH.name}")
    if ambiguous_log:
        print(f"{len(ambiguous_log)} titles matched ambiguously — check match_status='ambiguous' rows in cache.sqlite")

    conn.close()
    print("Done.")


def export_dashboard():
    """Pull everything from the cache and bake it into a self-contained dashboard.html."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM games WHERE match_status IN ('matched', 'ambiguous') ORDER BY title").fetchall()
    conn.close()

    games = []
    for r in rows:
        games.append({
            "title": r["title"],
            "platform": r["platform"] or "",
            "rating": round((r["rating"] or 0) * 20, 1),  # RAWG rating is /5, scale to /100 for a familiar feel
            "genres": r["genres"].split(", ") if r["genres"] else [],
            "plot": r["plot"] or "",
            "released": (r["released"] or "")[:4],
            "flagged": r["match_status"] == "ambiguous",
        })

    if not DASHBOARD_TEMPLATE.exists():
        sys.exit(f"Missing {DASHBOARD_TEMPLATE} template.")

    template = DASHBOARD_TEMPLATE.read_text()
    output = template.replace("__GAME_DATA__", json.dumps(games))
    DASHBOARD_OUTPUT.write_text(output)
    print(f"Wrote {DASHBOARD_OUTPUT} with {len(games)} games.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="Re-fetch every title, ignoring cache")
    args = parser.parse_args()

    run_enrichment(force_refresh=args.refresh)
    export_dashboard()
