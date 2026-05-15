#!/usr/bin/env python3
"""
FINN Bike Hunter — White CX Lite Oslo
Searches finn.no every 4 hours and creates a GitHub Issue when a match is found.
"""

import os
import re
import base64
import json
import time

import anthropic
import requests
from playwright.sync_api import sync_playwright

# ── Config ───────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO       = os.environ.get("GITHUB_REPOSITORY", "mariolallana/stolen-bike-finder")

LOCATION_CODE  = "0.20061"   # Oslo municipality
RADIUS_KM      = 60
MAX_PRICE      = None         # Set to int NOK to cap price; None = no cap
MAX_CANDIDATES = 10
MAX_SCREENSHOTS = 4
TEXT_GATE      = 4.0          # Min text score to proceed to visual check

SEARCH_QUERIES = [
    "White+CX+Lite",
    "White+Bikes+CX",
    "White+CX",
    "White+cyclocross",
    "White+krossykkel",
    "White+kross+sykkel",
    "krossykkel+svart+Sora",   # broad — capped at 3 new results
]

IMAGES_DIR = os.path.join(os.path.dirname(__file__), "images")
REFERENCE_IMAGES = [
    os.path.join(IMAGES_DIR, "bike_image_1.jpeg"),
    os.path.join(IMAGES_DIR, "bike_image_2.jpeg"),
]

# ── Text scoring ─────────────────────────────────────────────────────────────

def text_score(title: str, desc: str) -> tuple[float, list[str]]:
    t = title.lower()
    d = desc.lower()
    c = t + " " + d
    score, hits = 0.0, []

    if "white" in t:
        score += 3.0; hits.append("White✓(title)")
    elif "white" in d:
        score += 1.5; hits.append("White✓(desc)")

    if any(kw in c for kw in ["cx", "cyclocross", "kross"]):
        score += 2.5; hits.append("CX/kross✓")

    if "lite" in c:
        score += 1.5; hits.append("Lite✓")

    if any(kw in c for kw in ["52", "s/m", "small medium", "small/medium"]):
        score += 1.0; hits.append("52cm✓")

    if any(kw in c for kw in ["sora", "9-speed", "9s", "2x9"]):
        score += 0.5; hits.append("Sora✓")

    if any(kw in c for kw in ["disc", "skive", "promax", "tektro"]):
        score += 0.5; hits.append("Disc✓")

    if any(kw in c for kw in ["700c", '28"', "28 tommer"]):
        score += 0.3; hits.append("700c✓")

    if any(kw in c for kw in ["svart", "sort", "black", "matt"]):
        score += 0.2; hits.append("Svart✓")

    return score, hits

# ── finn.no helpers ───────────────────────────────────────────────────────────

def build_search_url(query: str) -> str:
    url = (
        f"https://www.finn.no/bap/forsale/search.html"
        f"?q={query}&location={LOCATION_CODE}&radius={RADIUS_KM}&vertical=bap"
    )
    if MAX_PRICE:
        url += f"&price_to={MAX_PRICE}"
    return url

def extract_finnkode(url: str) -> str | None:
    m = re.search(r"finnkode[=_](\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/article/(\d+)", url)
    return m.group(1) if m else None

def fetch_listing_html(url: str) -> str:
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if len(r.text) > 400:
            return r.text
    except Exception:
        pass
    return ""

def parse_listing(html: str, url: str) -> dict:
    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    title   = title_m.group(1).strip() if title_m else "?"

    price_m = re.search(r"([\d\s]{3,})\s*kr", html)
    price   = price_m.group(1).replace(" ", "").strip() if price_m else "N/A"

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()[:600]

    img_urls = re.findall(r'https://[^\s"\'<>]+?\.jpe?g', html, re.I)
    img_urls = list(dict.fromkeys(img_urls))[:2]

    return {"url": url, "title": title, "price": price, "description": text, "img_urls": img_urls}

# ── Visual scoring via Anthropic API ─────────────────────────────────────────

def load_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()

def visual_score(client: anthropic.Anthropic, screenshot_b64: str, refs: list[str]) -> tuple[float, str]:
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": (
                "Compare this finn.no listing photo against the White CX Lite reference photos.\n\n"
                "INSTANT REJECT (return score 0) if: glossy frame, non-black base color, "
                "suspension fork, flat bars, or clearly a different style of bike.\n\n"
                "Otherwise score these signals:\n"
                "+2.5 Reflective silver-white geometric shard pattern on top tube (mirror-like triangles)\n"
                "+2.0 Neon lime-yellow 'WHITE' wordmark on down tube\n"
                "+1.5 'CX LITE' text visible on frame\n"
                "+1.0 Matte black frame (flat, non-glossy finish)\n"
                "+0.5 Mechanical disc brakes visible\n"
                "+0.5 Drop bars + CX/gravel geometry\n\n"
                "Respond ONLY with JSON: "
                "{\"visual_score\": <float>, \"confirmed\": [<signals>], \"rejected\": <bool>, \"note\": \"<one sentence>\"}\n\n"
                "Reference photo 1 (full side view):"
            )},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": refs[0]}},
            {"type": "text", "text": "Reference photo 2 (close-up top tube — most diagnostic):"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": refs[1]}},
            {"type": "text", "text": "Listing photo to evaluate:"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}},
        ]}],
    )
    try:
        result = json.loads(msg.content[0].text)
        return float(result.get("visual_score", 0)), result.get("note", "")
    except Exception:
        return 0.0, "parse error"

# ── GitHub Issue ──────────────────────────────────────────────────────────────

def already_reported(finnkode: str) -> bool:
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
    return any(finnkode in (i.get("body") or "") for i in r.json())

def create_issue(matches: list[dict]) -> None:
    if not GITHUB_TOKEN:
        print("No GITHUB_TOKEN — skipping issue creation")
        return

    lines = [f"## {len(matches)} White CX Lite match(es) found on finn.no\n"]
    for m in matches:
        lines += [
            f"### {m['title']}",
            f"**Score:** {m['final_score']:.1f} / 18.0  (text {m['text_score']:.1f} + visual {m['visual_score']:.1f})",
            f"**Price:** {m['price']} NOK",
            f"**Matched:** {' · '.join(m['hits'])}",
            f"**Visual:** {m['visual_note']}",
            f"**Finnkode:** {m['finnkode']}",
            f"**Link:** {m['url']}\n",
        ]

    owner, repo = GITHUB_REPO.split("/")
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues",
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        json={"title": f"🚲 Bike match — {len(matches)} listing(s) on finn.no", "body": "\n".join(lines)},
    )
    if r.status_code == 201:
        print(f"Issue created: {r.json()['html_url']}")
    else:
        print(f"Issue creation failed: {r.status_code} {r.text[:200]}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    refs_b64 = [load_b64(p) for p in REFERENCE_IMAGES]

    candidates: dict[str, dict] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page    = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")

        # ── S1–S2: Collect listing URLs via Playwright ────────────────────
        for i, query in enumerate(SEARCH_QUERIES):
            if len(candidates) >= MAX_CANDIDATES:
                break
            broad    = i >= 5
            max_new  = 3 if broad else MAX_CANDIDATES
            new_here = 0
            max_pages = 1 if broad else 2

            for pg in range(1, max_pages + 1):
                url = build_search_url(query) + (f"&page={pg}" if pg > 1 else "")
                try:
                    page.goto(url, timeout=15000)
                    page.wait_for_timeout(2500)
                    html = page.content()
                except Exception as e:
                    print(f"Query {i+1} page {pg}: {e}"); break

                links = re.findall(
                    r'href="(/bap/forsale/ad\.html\?finnkode=\d+|/article/\d+)"', html
                )
                for link in links:
                    full = "https://www.finn.no" + link
                    kode = extract_finnkode(full)
                    if kode and kode not in candidates:
                        candidates[kode] = {"url": full, "finnkode": kode}
                        new_here += 1
                        if new_here >= max_new or len(candidates) >= MAX_CANDIDATES:
                            break

                if not links:
                    break

            print(f"Query {i+1} ({query[:20]}): pool now {len(candidates)}")
            time.sleep(1)

        print(f"\nTotal candidates: {len(candidates)}")

        # ── S3–S4: Enrich + text score ────────────────────────────────────
        shortlist = []
        for kode, c in candidates.items():
            html = fetch_listing_html(c["url"])
            if len(html) < 400:
                try:
                    page.goto(c["url"], timeout=15000)
                    page.wait_for_timeout(1500)
                    html = page.content()
                except Exception:
                    continue

            listing             = parse_listing(html, c["url"])
            listing["finnkode"] = kode
            sc, hits            = text_score(listing["title"], listing["description"])
            listing["text_score"] = sc
            listing["hits"]       = hits

            print(f"  [{sc:.1f}] {listing['title'][:60]}")
            if sc >= TEXT_GATE:
                shortlist.append(listing)

        print(f"\nPassed text gate (≥{TEXT_GATE}): {len(shortlist)}")

        # ── S5: Visual verification ───────────────────────────────────────
        matches, shots = [], 0

        for listing in sorted(shortlist, key=lambda x: x["text_score"], reverse=True):
            if shots >= MAX_SCREENSHOTS:
                break
            if already_reported(listing["finnkode"]):
                print(f"  Already reported: {listing['finnkode']}"); continue

            screenshot_b64 = None
            for img_url in listing.get("img_urls", []):
                try:
                    page.goto(img_url, timeout=10000)
                    screenshot_b64 = base64.standard_b64encode(page.screenshot()).decode()
                    shots += 1; break
                except Exception:
                    pass

            if not screenshot_b64:
                try:
                    page.goto(listing["url"], timeout=15000)
                    page.wait_for_timeout(2000)
                    screenshot_b64 = base64.standard_b64encode(page.screenshot()).decode()
                    shots += 1
                except Exception:
                    continue

            vis, note = visual_score(client, screenshot_b64, refs_b64)
            listing["visual_score"] = vis
            listing["visual_note"]  = note
            listing["final_score"]  = listing["text_score"] + vis
            print(f"  Visual +{vis:.1f}: {note}")

            if listing["final_score"] >= 5.0:
                matches.append(listing)

        browser.close()

    # ── Output ────────────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"MATCHES FOUND: {len(matches)}")
    for m in matches:
        print(f"  [{m['final_score']:.1f}/18.0] {m['title']}")
        print(f"  {m['url']}")

    if matches:
        create_issue(matches)
    else:
        print("No matches — no issue created.")

if __name__ == "__main__":
    main()
