#!/usr/bin/env python3
"""
Visual-scoring smoke test — runs on any OS (no hardcoded paths).

Usage:
    GEMINI_API_KEY=<key> python test_visual.py

Tests:
  SELF  — bike_image_2.jpeg used as listing photo  → expect score ≥ 5.0
  SIDE  — bike_image_1.jpeg used as listing photo  → expect score ≥ 4.0
  NEG   — generated solid green square             → expect score 0.0 (instant reject)
"""

import os, sys, base64, json, io, re
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

REPO   = Path(__file__).parent
REF1   = REPO / "images" / "bike_image_1.jpeg"
REF2   = REPO / "images" / "bike_image_2.jpeg"

GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-latest",
    "gemini-1.5-flash-8b",
]

PROMPT = (
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


def _shrink(image_bytes: bytes, max_px: int = 400) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if img.width > max_px:
        img = img.resize((max_px, int(img.height * max_px / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=65)
    return buf.getvalue()

def load_b64(path) -> str:
    return base64.standard_b64encode(_shrink(Path(path).read_bytes())).decode()

def make_green_square_b64() -> str:
    """Solid green 100×100 JPEG — clearly not a bike, should score 0."""
    img = Image.new("RGB", (100, 100), color=(34, 139, 34))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return base64.standard_b64encode(buf.getvalue()).decode()

def _part(b64: str) -> types.Part:
    return types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=base64.b64decode(b64)))


def call_gemini(client, img_b64: str, refs: list[str]) -> tuple[float, str, str]:
    contents = types.Content(role="user", parts=[
        types.Part(text=PROMPT),
        types.Part(text="Reference 1 (full side view):"), _part(refs[0]),
        types.Part(text="Reference 2 (close-up top tube — most diagnostic):"), _part(refs[1]),
        types.Part(text="Photo to evaluate:"), _part(img_b64),
    ])
    for model_name in GEMINI_MODELS:
        config_kwargs: dict = {
            "response_mime_type": "application/json",
            "max_output_tokens": 8192,
        }
        if "2.5" in model_name:
            config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

        response = None
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            text = response.text.strip()
            text = re.sub(r'^```[^\n]*\n', '', text)
            text = re.sub(r'\n?```$', '', text.strip())
            result = json.loads(text)
            return float(result.get("visual_score", 0)), result.get("note", ""), model_name
        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower():
                print(f"    [{model_name}] quota — next model"); continue
            if any(s in err for s in ("404", "400")) or any(
                s in err.lower() for s in ("not found", "invalid", "unsupported", "unknown field")
            ):
                print(f"    [{model_name}] unsupported — next model"); continue
            if any(s in err.lower() for s in ("expecting", "unterminated", "json")):
                raw = getattr(response, "text", "")[:150]
                print(f"    [{model_name}] bad JSON: {err[:60]}  raw={raw!r} — next model"); continue
            print(f"    [{model_name}] fatal: {err[:200]}")
            return 0.0, f"error: {err[:120]}", model_name
    return 0.0, "all models exhausted", "—"


def run_test(label: str, img_b64: str, refs: list[str], client,
             expect_reject: bool, min_score: float) -> bool:
    print(f"\n{'─'*55}")
    print(f"TEST: {label}")
    score, note, model = call_gemini(client, img_b64, refs)
    print(f"  Score : {score:.1f}  via {model}")
    print(f"  Note  : {note}")
    if expect_reject:
        ok = score == 0.0
        print(f"  Expect: 0.0 (reject)  →  {'PASS ✓' if ok else f'FAIL ✗ (got {score})'}")
    else:
        ok = score >= min_score
        print(f"  Expect: ≥ {min_score}  →  {'PASS ✓' if ok else f'FAIL ✗ (got {score})'}")
    return ok


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set"); sys.exit(1)

    for p in (REF1, REF2):
        if not p.exists():
            print(f"ERROR: reference image missing: {p}"); sys.exit(1)

    client = genai.Client(api_key=api_key)

    print("Loading reference images…")
    refs = [load_b64(REF1), load_b64(REF2)]
    print(f"  ref1: {len(refs[0])//1024} KB (b64)   ref2: {len(refs[1])//1024} KB (b64)")

    results = [
        run_test(
            "SELF — bike_image_2 (close-up, most diagnostic)",
            load_b64(REF2), refs, client,
            expect_reject=False, min_score=5.0,
        ),
        run_test(
            "SIDE — bike_image_1 (full side view)",
            load_b64(REF1), refs, client,
            expect_reject=False, min_score=4.0,
        ),
        run_test(
            "NEG — solid green square (instant reject expected)",
            make_green_square_b64(), refs, client,
            expect_reject=True, min_score=0,
        ),
    ]

    print(f"\n{'='*55}")
    print(f"RESULT: {sum(results)}/{len(results)} tests passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
