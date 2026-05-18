#!/usr/bin/env python3
"""
Quick visual-scoring smoke test.

Run locally:
    set GEMINI_API_KEY=<your_key>
    python test_visual.py

Three test cases:
  1. SELF  — bike_image_2.jpeg as listing photo → expect high score (≥ 5.0)
  2. NEG   — forest.jpeg as listing photo        → expect 0.0 (instant reject)
  3. SIDE  — bike_image_1.jpeg as listing photo  → expect high score (≥ 4.0)
"""

import os, sys, base64, json, io, re
from pathlib import Path

# Resolve paths relative to this file so test can be run from any directory
REPO     = Path(__file__).parent
IMAGES   = REPO / "images"
REF1     = IMAGES / "bike_image_1.jpeg"
REF2     = IMAGES / "bike_image_2.jpeg"
NEGATIVE = Path(r"C:\Users\mario\Downloads\forest.jpeg")

# Inline the helpers (avoids importing the whole hunter which needs Playwright)
from google import genai
from google.genai import types
from PIL import Image

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash-8b",
]

def _shrink(image_bytes: bytes, max_px: int = 400) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if img.width > max_px:
        img = img.resize((max_px, int(img.height * max_px / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=65)
    return buf.getvalue()

def load_b64(path) -> str:
    return base64.standard_b64encode(_shrink(Path(path).read_bytes())).decode()

def _part(b64: str) -> types.Part:
    return types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=base64.b64decode(b64)))

def visual_score(client, img_b64: str, refs: list[str]) -> tuple[float, str, str]:
    prompt = (
        "Compare this finn.no listing photo against the two White CX Lite reference photos.\n\n"
        "INSTANT REJECT (score 0) if: glossy frame, non-black base color (red, blue, white, silver, "
        "green, landscape photo, clearly not a bicycle), suspension fork, flat bars.\n\n"
        "Otherwise score:\n"
        "+2.5 Reflective silver-white geometric shard/triangle pattern on top tube\n"
        "+2.0 Neon lime-yellow 'WHITE' wordmark on frame\n"
        "+1.5 'CX LITE' text on frame\n"
        "+1.0 Matte black frame (flat, non-glossy)\n"
        "+0.5 Disc brakes visible\n"
        "+0.5 Drop bars + CX geometry\n\n"
        'Respond ONLY with JSON: {"visual_score": <float>, "confirmed": [<signals>], "rejected": <bool>, "note": "<one sentence>"}'
    )
    contents = types.Content(role="user", parts=[
        types.Part(text=prompt),
        types.Part(text="Reference 1 (full side view):"), _part(refs[0]),
        types.Part(text="Reference 2 (close-up top tube — most diagnostic):"), _part(refs[1]),
        types.Part(text="Photo to evaluate:"), _part(img_b64),
    ])
    for model_name in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=8192,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            text = response.text.strip()
            text = re.sub(r'^```[^\n]*\n', '', text)
            text = re.sub(r'\n?```$', '', text.strip())
            result = json.loads(text)
            return float(result.get("visual_score", 0)), result.get("note", ""), model_name
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "not found" in err.lower() or "404" in err:
                print(f"    [{model_name}] skip: {err[:80]}")
                continue
            # JSON parse error — print raw response for debugging
            if "Expecting" in err or "json" in err.lower():
                raw = getattr(locals().get("response", None), "text", "no response")
                print(f"    [{model_name}] JSON error: {err[:80]}")
                print(f"    Raw response: {str(raw)[:200]}")
            return 0.0, f"error: {err[:120]}", model_name
    return 0.0, "all models exhausted", "—"


def run_test(name: str, img_path, refs, client, expect_reject: bool, min_score: float):
    print(f"\n{'─'*55}")
    print(f"TEST: {name}")
    print(f"  File: {img_path}")
    img_b64 = load_b64(img_path)
    score, note, model = visual_score(client, img_b64, refs)
    print(f"  Score: {score:.1f}  via {model}")
    print(f"  Note:  {note}")

    if expect_reject:
        ok = score == 0.0
        print(f"  Expected: 0.0 (reject)  →  {'PASS ✓' if ok else f'FAIL ✗ (got {score})'}")
    else:
        ok = score >= min_score
        print(f"  Expected: ≥ {min_score}  →  {'PASS ✓' if ok else f'FAIL ✗ (got {score})'}")
    return ok


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set"); sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("Loading reference images…")
    refs = [load_b64(REF1), load_b64(REF2)]
    print(f"  ref1: {len(refs[0])//1024} KB (b64)  ref2: {len(refs[1])//1024} KB (b64)")

    results = []
    results.append(run_test(
        "SELF — bike_image_2 (close-up, most diagnostic)",
        REF2, refs, client,
        expect_reject=False, min_score=5.0,
    ))
    results.append(run_test(
        "SIDE — bike_image_1 (full side view)",
        REF1, refs, client,
        expect_reject=False, min_score=4.0,
    ))
    if NEGATIVE.exists():
        results.append(run_test(
            "NEG — forest.jpeg (should be instant-reject)",
            NEGATIVE, refs, client,
            expect_reject=True, min_score=0,
        ))
    else:
        print(f"\nSkipping negative test (file not found: {NEGATIVE})")

    print(f"\n{'='*55}")
    passed = sum(results)
    print(f"RESULT: {passed}/{len(results)} tests passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
