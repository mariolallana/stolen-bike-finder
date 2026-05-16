#!/usr/bin/env python3
"""
FINN Bike Hunter — White CX Lite Oslo
Searches finn.no every 4 hours and creates a GitHub Issue when a match is found.

Two-layer strategy:
  Layer 1 (queries 0-10):  Brand queries — all include "White". Text gate applies.
  Layer 2 (queries 11-17): Spec-only queries — no brand. Text gate bypassed,
                            visual check always runs. The query itself is the filter.
"""

import os
import re
import base64
import json
import time

from google import genai
from google.genai import types
import requests
from playwright.sync_api import sync_playwright

# ── Config ───────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPOSITORY", "mariolallana/stolen-bike-finder")

LOCATION_CODE    = "0.20061"   # Oslo municipality
RADIUS_KM        = 60
MAX_PRICE        = None        # Set to int NOK to cap price; None = no cap
MAX_CANDIDATES   = 20
MAX_SCREENSHOTS  = 8
TEXT_GATE        = 3.5         # Only applies to brand queries (layer 1)

SEARCH_QUERIES = [
    # Layer 1 — brand queries (text gate applies)
    "White+CX+Lite",            # 0 — exact model
    "White+Bikes+CX",           # 1 — full brand name
    "White+CX",                 # 2 — brand + type
    "White+gravel",             # 3 — sellers often just write "White gravel"
    "White+gruselsykkel",       # 4 — Norwegian: gravel bike
    "White+grus+sykkel",        # 5 — Norwegian gravel, space variant
    "White+helårssykkel",       # 6 — "all-year bike" — common vague description
    "White+allround+sykkel",    # 7 — all-rounder framing
    "White+cyclocross",         # 8 — English full term
    "White+krossykkel",         # 9 — Norwegian cyclocross
    "White+kross+sykkel",       # 10 — Norwegian cyclocross, space variant
    # Layer 2 — spec-only queries (text gate bypassed, always screenshotted)
    "krossykkel+svart+disc",    # 11 — black CX + disc, no brand
    "gravel+sykkel+svart+disc", # 12 — black gravel + disc, no brand
    "krossykkel+sora",          # 13 — Sora groupset is very specific to this price point
    "gravel+sykkel+sora",       # 14 — gravel + Sora
    "krossykkel+sort+disc",     # 15 — "sort" = the other Norwegian word for black
    "cx+sykkel+svart",          # 16 — CX abbreviation + black
    "krossykkel+skivebremser",  # 17 — full Norwegian for disc brakes
]

SPEC_QUERY_START = 11  # queries from this index onward bypass the text gate

IMAGES_DIR = os.path.join(os.path.dirname(__file__), "images")
REFERENCE_IMAGES = [
    os.path.join(IMAGES_DIR, "bike_image_1.jpeg"),
    os.path.join(IMAGES_DIR, "bike_image_2.jpeg"),
]

# ── Text scoring ──────────────────────────────────────────────────────────────

def text_score(title: str, desc: str) -> tuple[float, list[str]]:
    t = title.lower()
    d = desc.lower()
    c = t + " " + d
    score, hits = 0.0, []

    # Brand
    if "white" in t:
        score += 3.0; hits.append("White✓(title)")
    elif "white" in d:
        score += 1.5; hits.append("White✓(desc)")

    # Bike type — specific CX/kross terms score higher than generic gravel
    if any(kw in c for kw in ["cx", "cyclocross", "kross", "cx-sykkel", "crosssykkel"]):
        score += 2.5; hits.append("CX/kross✓")
    elif any(kw in c for kw in ["gravel", "grus", "allround", "allroad", "helårssykkel"]):
        score += 1.5; hits.append("Gravel/grus✓")

    # Model variant
    if "lite" in c:
        score += 1.5; hits.append("Lite✓")

    # Size
    if any(kw in c for kw in ["52", "s/m", "small medium", "small/medium"]):
        score += 1.0; hits.append("52cm✓")

    # Groupset
    if any(kw in c for kw in ["sora", "9-speed", "9s", "2x9", "9-gir", "shimano sora"]):
        score += 0.5; hits.append("Sora✓")

    # Brakes
    if any(kw in c for kw in ["disc", "skive", "promax", "tektro", "mekanisk", "skivebremser"]):
        score += 0.5; hits.append("Disc✓")

    # Wheel size
    if any(kw in c for kw in ["700c", '28"', "28 tommer"]):
        score += 0.3; hits.append("700c✓")

    # Frame material
    if any(kw in c for kw in ["aluminium", " alu ", "6061"]):
        score += 0.2; hits.append("Alu✓")

    # Color
    if any(kw in c for kw in ["svart", "sort", "black", "matt"]):
        score += 0.2; hits.append("Svart✓")

    return score, hits

# ── finn.no helpers ───────────────────────────────────────────────────────────

def build_search_url(query: str) -> str:
    url = (
        f"https://www.finn.no/recommerce/forsale/search"
        f"?q={query}&location={LOCATION_CODE}&radius={RADIUS_KM}"
    )
    if MAX_PRICE:
        url += f"&price_to={MAX_PRICE}"
    return url

def extract_item_id(url: str) -> str | None:
    # New finn.no URL format: /recommerce/forsale/item/XXXXXXXXX
    m = re.search(r"/item/(\d+)", url)
    if m:
        return m.group(1)
    # Legacy format fallback
    m = re.search(r"finnkode[=_](\d+)", url)
    return m.group(1) if m else None

def extract_links_from_html(html: str) -> list[str]:
    # finn.no renders full absolute URLs — match both formats
    full = re.findall(r'https://www\.finn\.no/recommerce/forsale/item/(\d+)', html)
    rel  = re.findall(r'href="(/recommerce/forsale/item/\d+)"', html)
    rel_full = [f"https://www.finn.no{r}" for r in rel]
    # Build deduplicated list of full URLs
    all_urls = [f"https://www.finn.no/recommerce/forsale/item/{i}" for i in dict.fromkeys(full)]
    for u in rel_full:
        if u not in all_urls:
            all_urls.append(u)
    return all_urls

def fetch_listing_data(url: str, page) -> dict:
    """
    Extract listing data from JSON-LD (most reliable) with Playwright fallback.
    Returns dict with title, price, description, img_url.
    """
    # Try requests first (fast, no JS needed for JSON-LD which is in raw HTML)
    html = ""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if len(r.text) > 400:
            html = r.text
    except Exception:
        pass

    # Playwright fallback
    if not html:
        try:
            page.goto(url, timeout=15000)
            page.wait_for_timeout(1500)
            html = page.content()
        except Exception:
            return {}

    return parse_jsonld(html, url)

def parse_jsonld(html: str, url: str) -> dict:
    """Extract listing fields from JSON-LD schema (embedded in page, no JS required)."""
    # Find ALL JSON-LD scripts and pick the Product one (page may have multiple schemas)
    blocks = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S)
    data = None
    for block in blocks:
        try:
            candidate = json.loads(block)
            if candidate.get("@type") == "Product":
                data = candidate
                break
        except Exception:
            continue

    if not data:
        return {"url": url, "title": "?", "price": "N/A", "description": "", "img_url": ""}

    title  = data.get("name", "?")
    desc   = data.get("description", "")[:600]
    img    = data.get("image", "")
    price  = str(data.get("offers", {}).get("price", "N/A"))

    return {"url": url, "title": title, "price": price, "description": desc, "img_url": img}

# ── Visual scoring via Gemini 1.5 Flash (free tier) ──────────────────────────

def load_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()

def _img_part(b64: str):
    return types.Part.from_bytes(data=base64.b64decode(b64), mime_type="image/jpeg")

def visual_score(client, screenshot_b64: str, refs: list[str]) -> tuple[float, str]:
    prompt = (
        "Compare this finn.no listing photo against the White CX Lite reference photos.\n\n"
        "INSTANT REJECT (score 0) if: glossy frame, non-black base color (red, blue, white, silver, green, etc.), "
        "suspension fork, flat bars, or clearly a different bike type.\n\n"
        "Otherwise score these signals:\n"
        "+2.5 Reflective silver-white geometric shard/triangle pattern on top tube\n"
        "+2.0 Neon lime-yellow 'WHITE' wordmark visible on frame\n"
        "+1.5 'CX LITE' text on frame\n"
        "+1.0 Matte black frame (flat, non-glossy finish)\n"
        "+0.5 Disc brakes visible at wheels\n"
        "+0.5 Drop bars + CX/gravel geometry\n\n"
        "Respond ONLY with JSON (no markdown): "
        "{\"visual_score\": <float>, \"confirmed\": [<signals>], \"rejected\": <bool>, \"note\": \"<one sentence>\"}\n\n"
        "Reference photo 1 (full side view):"
    )
    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=[
                    prompt,
                    _img_part(refs[0]),
                    "Reference photo 2 (close-up top tube — most diagnostic):",
                    _img_part(refs[1]),
                    "Listing photo to evaluate:",
                    _img_part(screenshot_b64),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=256,
                ),
            )
            result = json.loads(response.text)
            return float(result.get("visual_score", 0)), result.get("note", "")
        except Exception as e:
            if attempt == 0 and "429" in str(e):
                time.sleep(30)   # back off on rate limit and retry once
                continue
            return 0.0, f"error: {str(e)[:120]}"
    return 0.0, "rate limit — skipped"

# ── GitHub Issue ──────────────────────────────────────────────────────────────

def already_reported(item_id: str) -> bool:
    if not GITHUB_TOKEN:
        return False
    owner, repo = GITHUB_REPO.split("/")
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        params={"state": "open", "per_page": 50},
    )
    if r.status_code != 200:
        return False
    return any(item_id in (i.get("body") or "") for i in r.json())

def _stars(pct: int) -> str:
    if pct >= 72: return "★★★★★"
    if pct >= 56: return "★★★★☆"
    if pct >= 39: return "★★★☆☆"
    return "★★☆☆☆"

def create_issue(matches: list[dict]) -> None:
    if not GITHUB_TOKEN:
        print("No GITHUB_TOKEN — skipping issue creation")
        return

    # One issue per match — cleaner email subjects
    for m in matches:
        pct   = round((m["final_score"] / 18.0) * 100)
        stars = _stars(pct)
        title = m.get("title", "?")
        price = m.get("price", "?")
        hits  = " · ".join(m.get("hits", [])) or "no brand in title (spec-only match)"
        note  = m.get("visual_note", "—")

        body = f"""## {title}

**{stars} {pct}% match** &nbsp;·&nbsp; text {m['text_score']:.1f} + visual {m['visual_score']:.1f}

> {note}

**Price:** {price} NOK
**Signals:** {hits}

## [→ Open on finn.no]({m['url']})
"""

        issue_title = f"🚲 {stars} {title} · {price} NOK"

        owner, repo = GITHUB_REPO.split("/")
        r = requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"title": issue_title, "body": body},
        )
        if r.status_code == 201:
            print(f"Issue created: {r.json()['html_url']}")
        else:
            print(f"Issue creation failed: {r.status_code} {r.text[:200]}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    client   = genai.Client(api_key=GEMINI_API_KEY)
    refs_b64 = [load_b64(p) for p in REFERENCE_IMAGES]

    candidates: dict[str, dict] = {}  # item_id → candidate dict

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")

        # ── Collect listing URLs ──────────────────────────────────────────
        for i, query in enumerate(SEARCH_QUERIES):
            if len(candidates) >= MAX_CANDIDATES:
                break

            is_spec_query = i >= SPEC_QUERY_START
            max_new  = 3 if is_spec_query else MAX_CANDIDATES
            max_pages = 1 if is_spec_query else 2
            new_here = 0

            for pg in range(1, max_pages + 1):
                url = build_search_url(query) + (f"&page={pg}" if pg > 1 else "")
                try:
                    page.goto(url, timeout=15000)
                    page.wait_for_timeout(2500)
                    html = page.content()
                except Exception as e:
                    print(f"Query {i+1} page {pg}: {e}"); break

                links = extract_links_from_html(html)
                for full_url in links:
                    item_id = extract_item_id(full_url)
                    if item_id and item_id not in candidates:
                        candidates[item_id] = {
                            "url":         full_url,
                            "item_id":     item_id,
                            "brand_query": not is_spec_query,
                        }
                        new_here += 1
                        if new_here >= max_new or len(candidates) >= MAX_CANDIDATES:
                            break

                if not links:
                    break

            print(f"Query {i+1} ({'spec' if is_spec_query else 'brand'}) [{query[:22]}]: pool={len(candidates)}")
            time.sleep(1)

        print(f"\nTotal candidates: {len(candidates)}")

        # ── Enrich + text score ───────────────────────────────────────────
        shortlist = []
        for item_id, c in candidates.items():
            data = fetch_listing_data(c["url"], page)
            if not data or not data.get("title"):
                continue

            c.update(data)
            sc, hits = text_score(c.get("title", ""), c.get("description", ""))
            c["text_score"] = sc
            c["hits"]       = hits

            # Spec-only candidates bypass the text gate — they go to visual regardless
            passes = (not c["brand_query"]) or (sc >= TEXT_GATE)
            label  = f"[{sc:.1f}{'*bypass' if not c['brand_query'] else ''}]"
            print(f"  {label} {c.get('title','?')[:60]}")

            if passes:
                shortlist.append(c)

        print(f"\nFor visual check: {len(shortlist)} listings")

        # ── Visual verification ───────────────────────────────────────────
        matches, shots = [], 0
        # Brand query candidates first (higher text score = more likely match),
        # then spec-only candidates
        shortlist.sort(key=lambda x: (not x["brand_query"], -x["text_score"]))

        for c in shortlist:
            if shots >= MAX_SCREENSHOTS:
                break
            if already_reported(c["item_id"]):
                print(f"  Already reported: {c['item_id']}"); continue

            screenshot_b64 = None
            img_url = c.get("img_url", "")

            if img_url:
                try:
                    page.goto(img_url, timeout=10000)
                    screenshot_b64 = base64.standard_b64encode(page.screenshot()).decode()
                    shots += 1
                except Exception:
                    pass

            if not screenshot_b64:
                try:
                    page.goto(c["url"], timeout=15000)
                    page.wait_for_timeout(2000)
                    screenshot_b64 = base64.standard_b64encode(page.screenshot()).decode()
                    shots += 1
                except Exception:
                    continue

            vis, note = visual_score(client, screenshot_b64, refs_b64)
            c["visual_score"] = vis
            c["visual_note"]  = note
            c["final_score"]  = c["text_score"] + vis
            print(f"  Visual +{vis:.1f}: {note}")

            if c["final_score"] >= 5.0:
                matches.append(c)

        browser.close()

    # ── Output ────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"MATCHES FOUND: {len(matches)}")
    for m in matches:
        print(f"  [{m['final_score']:.1f}/18.0] {m.get('title','?')}")
        print(f"  {m['url']}")

    if matches:
        create_issue(matches)
    else:
        print("No matches — no issue created.")

if __name__ == "__main__":
    main()
