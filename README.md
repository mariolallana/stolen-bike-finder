# stolen-bike-finder

Searches finn.no every 4 hours for a **White CX Lite** cyclocross bike in the Oslo area. Sends a GitHub Issue (+ email) when a match is found.

---

## How it works

1. Runs 7 targeted finn.no search queries ("White CX Lite", "White krossykkel", etc.)
2. Text-scores each listing against target specs (brand, model, size, groupset, brakes)
3. For top candidates: takes a screenshot and uses Claude vision to compare against reference photos
4. If any listing scores ≥ 5.0 / 18.0 → creates a GitHub Issue with price + link

Runs automatically every 4 hours via GitHub Actions. No server needed.

---

## Target bike

| Spec | Value |
|------|-------|
| Brand / Model | White CX Lite |
| Frame size | 52 cm (S/M, fits ~170 cm) |
| Groupset | Shimano Sora 2×9 |
| Brakes | Mechanical disc (Promax / Tektro) |
| Wheels | 700c / 28" |
| Color | Matte black + reflective silver shard pattern on top tube + lime-yellow "WHITE" logo |

---

## Setup

### 1. Add the API key secret

Go to **Settings → Secrets → Actions → New repository secret**:

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | Your key from console.anthropic.com |

`GITHUB_TOKEN` is provided automatically by GitHub — no setup needed.

### 2. Run it

**Automatic:** fires every 4 hours on its own.

**Manual:** Actions tab → FINN Bike Hunter → Run workflow.

---

## Configuration

All settings are at the top of `finn_bike_hunter.py`:

```python
RADIUS_KM   = 60    # search radius from Oslo
MAX_PRICE   = None  # set to e.g. 12000 to cap price in NOK
```

---

## Output

| Situation | What happens |
|-----------|-------------|
| No match | Run completes silently. Check Actions tab if curious. |
| Match found | GitHub Issue created → you get an email with price + finn.no link |

---

## Files

```
finn_bike_hunter.py   — main search script
images/               — reference photos of the target bike
.github/workflows/    — GitHub Actions schedule (every 4 hours)
requirements.txt      — Python dependencies
```
