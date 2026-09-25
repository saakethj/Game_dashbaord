# GamePal ratings enrichment

Pulls rating, genre, and a short plot summary for every game in `game_list.json`
using the free RAWG API, caches it locally, and generates `dashboard.html` —
a self-contained page you can open in any browser.

## First-time setup

1. **Get a free RAWG API key** — sign up at https://rawg.io/apidocs (instant, no
   approval wait, generous free tier).
2. **Fill in the 4 blank platforms** in `game_list.json` — Assassin's Creed
   Valhalla, Shadow of the Tomb Raider, Silent Hill 2, Silent Hill f. Their
   platform text was cut off in your screenshots. Anything works, even leaving
   it blank — it's just metadata, not used for matching.
3. Push this folder to a new GitHub repo.
4. In the repo's Settings → Secrets and variables → Actions, add a secret
   named `RAWG_API_KEY` with your key.
5. Go to the Actions tab → "Update game ratings" → Run workflow (manual
   trigger). It'll fetch all ~86 games, commit `cache.sqlite` and
   `dashboard.html` back to the repo.
6. After that it runs automatically every Monday, picking up any new games
   you've added to `game_list.json` since the last run.

## Viewing your dashboard

Two options once `dashboard.html` exists in the repo:

- **Simplest**: download `dashboard.html` from the repo and open it locally
  in any browser — it's fully self-contained, no server needed.
- **Always-on link**: turn on GitHub Pages for the repo (Settings → Pages →
  deploy from branch, root folder). You'll get a permanent URL like
  `https://yourusername.github.io/gamepal-ratings/dashboard.html` that
  updates itself every time the weekly workflow runs.

## Adding a new game later

Just add `{"title": "...", "platform": "..."}` to `game_list.json` and
either wait for the weekly run or trigger it manually from the Actions tab.

## When something doesn't match

Check `unmatched.json` after a run — titles RAWG couldn't confidently find.
Common cause: unusual formatting or a very new/obscure release RAWG hasn't
indexed yet. Fix the title spelling in `game_list.json` and rerun, or add
`--refresh` to force re-fetching everything:

```
python enrich.py --refresh
```

Titles that matched ambiguously (e.g. a remaster vs. the original) are
still included but flagged "Match needs confirming" on the dashboard —
check `cache.sqlite`'s `match_status` column to see RAWG's top 3
candidates and lock in the right one if needed. ':'
