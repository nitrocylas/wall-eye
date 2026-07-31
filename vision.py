"""Vision checks via a local Ollama vision model.

Pipeline: downscale the frame, ask the model for a structured verdict
(areas sweep, item list, bounding boxes, alert decision), optionally
re-locate boxes with a dedicated grounding model, then re-judge each
flagged region as a full-resolution close-up before trusting it.
"""
import base64
import json
import re
from io import BytesIO

import requests
from PIL import Image

import errors

# Matches a bare coordinate list the model sometimes leaks into 'items',
# e.g. "[0,329,487,716]" or "(0.1, 0.2, 0.3, 0.4)".
_COORD_RE = re.compile(r"^[\[\(]?\s*-?\d+(\.\d+)?(\s*,\s*-?\d+(\.\d+)?){1,3}\s*[\]\)]?$")


def _good_item(it: object) -> bool:
    """True if a model 'item'/'label' string is a real object name, not junk.

    Rejects non-strings, near-empty strings, coordinate leakage (e.g. "[0,1,2,3]"),
    and path-like hallucinations ("./home/room-1") that small vision models sometimes
    emit instead of object names.
    """
    if not isinstance(it, str):
        return False
    s = it.strip()
    return (len(s) >= 2 and any(c.isalpha() for c in s)
            and "/" not in s and "\\" not in s
            and not _COORD_RE.match(s))

# Forces the model to answer in a machine-readable shape - no parsing prose.
# PROPERTY NAMES CONTROL FILL ORDER: Ollama's structured-output grammar emits
# object properties in ALPHABETICAL order, ignoring the order declared here.
# The names below are chosen so that the alphabetical order IS the intended
# reasoning order - sweep the areas, list the items, box them, summarize,
# and only THEN decide the verdict:
#   areas < items < regions < summary < verdict_alert < verdict_confidence
# check() renames verdict_alert/verdict_confidence back to the canonical
# alert/confidence keys the rest of the app uses.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "areas": {
            "type": "array",
            "description": "Work through the scene AREA BY AREA before anything "
                           "else: one entry for EVERY area/surface named in the "
                           "watch condition (e.g. floor, bed, desk, chairs). "
                           "Never skip an area because you already found mess "
                           "elsewhere, and report every mess on it - medium "
                           "messes included, not just the worst one. An area "
                           "with nothing wrong gets an empty out_of_place list.",
            "items": {
                "type": "object",
                # 'area' < 'out_of_place': name the area BEFORE judging it
                "properties": {
                    "area": {
                        "type": "string",
                        "description": "Short lowercase name of the area, "
                                       "e.g. 'floor' or 'desk'",
                    },
                    "out_of_place": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Each out-of-place thing on this area "
                                       "as a short phrase; empty list if the "
                                       "area is clear",
                    },
                },
                "required": ["area", "out_of_place"],
            },
        },
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "description": "EVERY out-of-place thing from the areas sweep above "
                           "as a SHORT human-readable phrase, e.g. 'jacket on "
                           "the bed'. Never put numbers or coordinates here - "
                           "bounding boxes go in 'regions'. Empty list if "
                           "nothing is out of place.",
        },
        "regions": {
            "type": "array",
            "description": "A bounding box around each out-of-place item, so it can "
                           "be highlighted on the image. MUST contain one entry for "
                           "EVERY item in 'items' - never leave it empty when 'items' "
                           "has entries. Empty list only if all clear.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short name of the messy item in this box",
                    },
                    "box": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "[x1, y1, x2, y2] pixel coordinates of the box: "
                                       "top-left corner (x1,y1) and bottom-right corner "
                                       "(x2,y2).",
                    },
                },
                "required": ["label", "box"],
            },
        },
        "summary": {
            "type": "string",
            "description": "One short sentence describing what was seen (or 'all clear')",
        },
        "verdict_alert": {
            "type": "boolean",
            "description": "Decide this from the 'items' you listed above and "
                           "the watch condition's decision rule: true only if the "
                           "watched condition is met and the user should be notified",
        },
        "verdict_confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
    },
    "required": ["areas", "items", "regions", "summary",
                 "verdict_alert", "verdict_confidence"],
}

SYSTEM_PROMPT = (
    "You are a home monitoring camera assistant. You look at camera frames and "
    "judge whether the user's watch condition is met. Be specific and concrete: "
    "name the actual objects you see and where they are. Do not invent objects "
    "you cannot clearly see.\n\n"
    "CRITICAL RULES:\n"
    "1. IGNORE people and pets completely. A human being or an animal present in "
    "the frame is NEVER mess, clutter, clothing, or an out-of-place object. A "
    "person sitting, standing, or moving in the room is normal - never report "
    "them, their body, or the clothes they are wearing as a problem.\n"
    "2. Only report INANIMATE objects that are genuinely out of place (e.g. "
    "clothes lying on the floor or bed, dishes left out, scattered items).\n"
    "3. Clothes being WORN by a person are not 'clothes on the floor'. Only count "
    "clothing that is discarded on a surface with no one wearing it.\n"
    "4. If the only notable thing in the frame is a person or pet, set alert to "
    "false.\n"
    "5. If image quality is too poor to judge, set alert to false and confidence "
    "to low.\n"
    "6. Identify surfaces correctly and never confuse them. A BED is a raised "
    "sleeping surface with a mattress, pillows and blankets - anything resting on "
    "it is 'on the bed', NOT 'on the floor'. The FLOOR is the ground-level surface "
    "you walk on. A DESK or table is a raised work surface. Always state each "
    "item's TRUE location (bed / floor / desk / chair) and do not mislabel it.\n"
    "7. For every item you list in 'items', add a matching entry in 'regions' with "
    "a bounding box around it. box = [x1, y1, x2, y2] are PIXEL coordinates in the "
    "image: (x1, y1) is the top-left corner and (x2, y2) is the bottom-right corner. "
    "Do NOT draw boxes around people or pets.\n"
    "8. Work in this order: FIRST sweep the frame area by area and fill 'areas' "
    "- one entry for EVERY area or surface the watch condition names, with an "
    "empty out_of_place list when that area is clear; report ALL messes there, "
    "medium ones included, not just the worst one. THEN copy every out-of-place "
    "thing into 'items' as short phrases, THEN write the summary and decide "
    "'alert' from the items you actually listed, and FINALLY box each listed "
    "item in 'regions'. 'regions' must get one bounding box per listed item - "
    "never leave it empty when 'items' is not empty.\n"
    "9. Do not confuse look-alikes. Bedding that belongs on the bed (pillows, a "
    "duvet, a folded blanket) is NOT discarded clothing. A rug or floor mat is "
    "NOT an item left on the floor. Furniture, bins, lamps, curtains, radiators "
    "and cables are part of the room, never clutter. A coat or bag hanging on a "
    "hook or door is stored, not out of place.\n\n"
    "Answer ONLY in the required JSON format."
)

# ---------- close-up verification (second pass) ----------
#
# The small model's main failure mode is DIFFERENTIATION: at 512px a duvet reads
# as "clothes on the bed", a rug as "something on the floor", a chair as a "pile".
# So after the first pass, each flagged region is cropped from the FULL-RES frame
# (far more pixels on the object than the model saw the first time) and shown back
# to the same model for a yes/no re-judgement. Same model, tiny images -> no extra
# VRAM; just a couple of extra seconds per flagged item.

# ALPHABETICAL fill order (see VERDICT_SCHEMA): 'described' sorts before the
# is_* judgements and 'out_of_place', so the model describes the crop BEFORE
# it judges it. (_verify_region renames it back to 'seen' for the callers.)
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "described": {
            "type": "string",
            "description": "What the main thing in this close-up crop ACTUALLY is, "
                           "as a short phrase, e.g. 'grey hoodie' or 'folded duvet'.",
        },
        "is_person_or_pet": {
            "type": "boolean",
            "description": "true if the crop mainly shows a person or an animal "
                           "(including just part of one)",
        },
        "is_part_of_room": {
            "type": "boolean",
            "description": "true if it is furniture, a rug or mat, a bin, a lamp, "
                           "a curtain, a radiator, a cable, bedding that belongs on "
                           "the bed, or anything else that is a permanent part of "
                           "the room rather than an item someone left out",
        },
        "out_of_place": {
            "type": "boolean",
            "description": "true ONLY if this is a loose item someone should pick "
                           "up or put away",
        },
    },
    "required": ["described", "is_person_or_pet", "is_part_of_room",
                 "out_of_place"],
}

VERIFY_SYSTEM_PROMPT = (
    "You double-check a single detection from a home monitoring camera. You are "
    "shown a CLOSE-UP crop of one region of the frame. The first detection pass "
    "is often wrong, so look with fresh eyes: first describe what the main thing "
    "in the crop actually is, then classify it. Typical first-pass mistakes to "
    "catch: bedding (pillows, duvet, folded blankets) mistaken for discarded "
    "clothes; a rug or mat mistaken for something lying on the floor; furniture, "
    "bins, lamps, cables or curtains mistaken for clutter; part of a person or "
    "pet mistaken for objects. Answer ONLY in the required JSON format."
)

_VERIFY_PAD = 0.15          # grow each crop by 15% so the object keeps context
_VERIFY_MAX_SIDE = 448      # close-up sent to the model; small = cheap tokens
_VERIFY_MAX_REGIONS = 4     # cap second-pass cost; extra regions pass unverified
_MIN_CROP_PX = 12           # a sliver this thin can't be judged - skip it

# Location/filler words ignored when matching a region label to an item phrase,
# so "box on the floor" doesn't match "sock on the floor" via "floor".
_MATCH_STOPWORDS = frozenset(
    "the and with near under over on in of a an left right top bottom corner "
    "floor bed desk chair table couch sofa shelf room".split())


def _crop_rect(frac_box, W, H, pad=_VERIFY_PAD):
    """Fractional [x, y, w, h] -> padded integer pixel rect (l, t, r, b) inside a
    WxH image, or None if the region is too small to be worth a close-up."""
    x, y, w, h = frac_box
    px, py = w * pad, h * pad
    left = int(max(0.0, x - px) * W)
    top = int(max(0.0, y - py) * H)
    right = int(min(1.0, x + w + px) * W)
    bottom = int(min(1.0, y + h + py) * H)
    if right - left < _MIN_CROP_PX or bottom - top < _MIN_CROP_PX:
        return None
    return (left, top, right, bottom)


def _crop_jpeg(jpeg, frac_box, max_side=_VERIFY_MAX_SIDE):
    """Cut the region out of the FULL-RES frame and return small JPEG bytes,
    or None if the crop is unusable."""
    try:
        im = Image.open(BytesIO(jpeg)).convert("RGB")
        rect = _crop_rect(frac_box, *im.size)
        if rect is None:
            return None
        im = im.crop(rect)
        scale = max_side / max(im.size)
        if scale < 1.0:
            im = im.resize((max(1, round(im.width * scale)),
                            max(1, round(im.height * scale))), Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def _words(text):
    return {w for w in re.findall(r"[a-z]+", text.lower())
            if len(w) >= 3 and w not in _MATCH_STOPWORDS}


# Filler ignored when comparing two item phrases for sameness. Unlike
# _MATCH_STOPWORDS this KEEPS location words - 'clothes on the bed' and
# 'clothes on the chair' are different messes, not duplicates.
_PHRASE_FILLER = frozenset("a an the on in of and with at".split())

# A place name usable in an "on the <place>" phrase: one or two plain words.
_PLACE_RE = re.compile(r"[a-z]{2,15}(?: [a-z]{2,15})?$")


def _phrase_key(phrase):
    """Word-set fingerprint for duplicate detection between item phrases
    ('bag on the floor' == 'bag on floor', != 'bag on the bed')."""
    return frozenset(w for w in re.findall(r"[a-z]+", phrase.lower())
                     if w not in _PHRASE_FILLER)


def _with_place(item, place):
    """'white bag' + 'floor' -> 'white bag on the floor' (reads well in the
    alert/voice and grounds better). The phrase is left alone when it already
    names the place or the place string is junk."""
    it = item.strip()
    p = place.strip().lower()
    if not _PLACE_RE.fullmatch(p) or p in it.lower():
        return it
    return f"{it} on the {p}"


def _merge_area_items(items, areas, cap=10):
    """Union of the model's flat 'items' list and everything it found in the
    per-area sweep. The 'areas' field is what forces the model to actually
    LOOK at every surface (it otherwise stops after the most salient mess);
    folding the finds back in here means downstream code - alert text, voice,
    grounding - keeps working off one item list. Deduped by word-set so the
    same mess reported in both places isn't listed twice."""
    merged = list(items)
    seen = {_phrase_key(it) for it in merged}
    for area in areas or []:
        if not isinstance(area, dict):
            continue
        place = str(area.get("area", ""))
        found = area.get("out_of_place")
        for raw in (found if isinstance(found, list) else []):
            if not _good_item(raw):
                continue
            phrase = _with_place(raw, place)
            key = _phrase_key(phrase)
            if key in seen:
                continue
            seen.add(key)
            merged.append(phrase)
    return merged[:cap]


def _label_matches_item(label, item):
    """True if a region label and an item phrase are talking about the same
    object (they share at least one meaningful word)."""
    return bool(_words(label) & _words(item))


def apply_verification(verdict, results):
    """Merge close-up verification results back into a first-pass verdict.

    results: list of (region, verify_dict_or_None) covering verdict['regions'];
    None means verification was skipped/failed -> FAIL OPEN (keep the detection,
    a broken second pass must never silence a real alert). Rejected regions are
    removed along with the item phrases naming the same object, rejected labels
    are recorded in verdict['vetoed'], and a confirmed 'seen' phrase replaces a
    vaguer first-pass label. If everything was rejected, the alert is cancelled.
    Pure function on plain data - unit-testable without a model.
    """
    kept, vetoed = [], []
    for region, v in results:
        if not isinstance(v, dict):
            kept.append(region)
            continue
        rejected = (bool(v.get("is_person_or_pet"))
                    or bool(v.get("is_part_of_room"))
                    or not v.get("out_of_place", True))
        if rejected:
            vetoed.append(region["label"])
            continue
        seen = str(v.get("seen", "")).strip()
        if _good_item(seen) and seen.lower() != region["label"].lower():
            region = {**region, "label": seen}
        kept.append(region)

    verdict = dict(verdict)
    verdict["regions"] = kept
    verdict["vetoed"] = vetoed
    if vetoed:
        verdict["items"] = [it for it in verdict["items"]
                            if not any(_label_matches_item(lbl, it)
                                       for lbl in vetoed)]
        if verdict["alert"] and not verdict["items"]:
            verdict["alert"] = False
            verdict["summary"] = (verdict["summary"].rstrip(". ")
                                  + ". On close-up inspection nothing is actually "
                                    "out of place.")
    return verdict


def _verify_region(crop_jpeg, label, url, model, options, keep_alive, timeout):
    """One close-up re-judgement. Returns the parsed verify dict, or None on any
    failure (caller fails open)."""
    messages = [
        {"role": "system", "content": VERIFY_SYSTEM_PROMPT},
        {"role": "user",
         "content": (f"The first pass thought this close-up shows: '{label}'. "
                     "It may be wrong. What is it really?"),
         "images": [base64.b64encode(crop_jpeg).decode()]},
    ]
    try:
        payload = {"model": model, "messages": messages, "format": VERIFY_SCHEMA,
                   "options": options, "keep_alive": keep_alive, "stream": False}
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=timeout)
        if resp.status_code != 200:
            return None
        parsed = json.loads(resp.json()["message"]["content"])
        # schema name 'described' exists only to force describe-first fill
        # order; callers speak 'seen'
        parsed["seen"] = parsed.pop("described", parsed.get("seen", ""))
        return parsed
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        return None


class VisionError(Exception):
    pass


# qwen-VL models often ground boxes in a 0-1000 normalized space instead of
# real pixels. Detected when a coordinate exceeds the frame but fits in ~1000.
_QWEN_NORM_SPACE = 1000.0
_SCALE_TOLERANCE = 1.05   # allow slight overshoot before rejecting a scale guess

# Model families known to ground boxes in qwen's 0-1000 space no matter what
# the prompt says about pixel coordinates. qwen2.5vl grounds in real pixels,
# so it stays on the pixel default.
_NORM1000_MODEL_HINTS = ("qwen3-vl",)


def _prefers_norm1000(model: str) -> bool:
    """True if this model is known to emit 0-1000 boxes even when every
    coordinate happens to fit inside the frame (the ambiguous case)."""
    name = (model or "").lower()
    return any(hint in name for hint in _NORM1000_MODEL_HINTS)


def _float_box(box):
    """Raw model box -> [x1, y1, x2, y2] floats, or None if malformed."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        return [float(v) for v in box]
    except (TypeError, ValueError):
        return None


def _detect_box_space(raw_boxes, W, H, prefer_norm1000=False):
    """Decide which coordinate space a response's boxes are in:
    'frac' (0-1), 'pixel' (frame pixels), or 'norm1000' (qwen's 0-1000 grid).

    ONE decision for the whole response: a model grounds every box of a reply
    in the same space, so the strongest evidence anywhere (e.g. a single y that
    overflows the frame height but fits in 1000) settles all of them. Deciding
    per box let one reply mix spaces - a 0-1000 box near the top-left fits
    inside the frame, got misread as pixels, and was drawn around the wrong
    thing while its siblings landed correctly.

    prefer_norm1000 resolves the truly ambiguous case (everything fits the
    frame in either reading) for models known to ground in 0-1000 regardless
    of the pixel-coordinate instructions (see _NORM1000_MODEL_HINTS).
    """
    xs, ys = [], []
    for raw in raw_boxes:
        fb = _float_box(raw)
        if fb:
            xs += [abs(fb[0]), abs(fb[2])]
            ys += [abs(fb[1]), abs(fb[3])]
    if not xs:
        return "pixel"
    biggest = max(max(xs), max(ys))
    if biggest <= 1.5:
        return "frac"
    fits_pixels = (max(xs) <= W * _SCALE_TOLERANCE
                   and max(ys) <= H * _SCALE_TOLERANCE)
    fits_norm1000 = biggest <= _QWEN_NORM_SPACE * _SCALE_TOLERANCE
    if fits_pixels and not (prefer_norm1000 and fits_norm1000):
        return "pixel"
    if fits_norm1000:
        return "norm1000"
    return "pixel"      # oversized junk - read as pixels, clamping caps it


def _clean_regions(raw, W, H, prefer_norm1000=False):
    """Model region dicts -> [{'label', 'box': frac [x,y,w,h]}] with junk
    dropped. The coordinate space is decided ONCE across all boxes (see
    _detect_box_space) so sibling boxes can't be read in different scales."""
    if not isinstance(raw, list):
        return []
    space = _detect_box_space(
        [r.get("box") for r in raw if isinstance(r, dict)],
        W, H, prefer_norm1000=prefer_norm1000)
    clean = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        label = str(r.get("label", "")).strip()
        # A box whose label is junk ("." / a path / coordinates) is itself a
        # hallucination - drawing it would just circle something random.
        if not _good_item(label):
            continue
        nb = _norm_box(r.get("box"), W, H, space)
        if nb:
            clean.append({"label": label, "box": nb})
    return clean


def _norm_box(box, W, H, space="pixel"):
    """Convert one model box to fractional [x, y, w, h] (each 0-1) given the
    response's coordinate space (decided once by _detect_box_space). Corners
    are sorted (so order doesn't matter) and scaled to fractions, which is all
    the GUI needs. Returns None for an unusable box.
    """
    fb = _float_box(box)
    if fb is None:
        return None
    a, b, c, d = fb
    if space == "norm1000":
        a, c = a / _QWEN_NORM_SPACE, c / _QWEN_NORM_SPACE
        b, d = b / _QWEN_NORM_SPACE, d / _QWEN_NORM_SPACE
    elif space == "pixel":
        a, c = a / W, c / W
        b, d = b / H, d / H
    # 'frac' boxes are already fractions
    lo_x, hi_x = sorted((a, c))
    lo_y, hi_y = sorted((b, d))
    x = min(max(lo_x, 0.0), 1.0)
    y = min(max(lo_y, 0.0), 1.0)
    w = min(max(hi_x - lo_x, 0.0), 1.0 - x)
    h = min(max(hi_y - lo_y, 0.0), 1.0 - y)
    if w < 0.01 or h < 0.01:
        return None
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


# ---------- dedicated grounding pass (box placement) ----------
#
# Some models judge the room well (items/alert/summary) but place their
# bounding boxes badly - even in the right coordinate space they land on the
# wrong object. An optional dedicated vision model re-locates each found item
# instead. The grounder only loads when mess was actually found, and unloads
# quickly to free VRAM.

_GROUND_KEEP_ALIVE = "30s"   # free the grounder's VRAM right after the boxes
_GROUND_MAX_ITEMS = 6        # cap the ask; more boxes than this is noise anyway

GROUND_SCHEMA = {
    "type": "object",
    "properties": {
        "regions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "The item from the list this box locates",
                    },
                    "box": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "[x1, y1, x2, y2]: top-left and "
                                       "bottom-right corners of a tight box "
                                       "around the item",
                    },
                },
                "required": ["label", "box"],
            },
        },
    },
    "required": ["regions"],
}

GROUND_SYSTEM_PROMPT = (
    "You locate objects in a photo. For each item in the user's list, return "
    "one bounding box [x1, y1, x2, y2] tightly around that item: (x1, y1) is "
    "the top-left corner and (x2, y2) the bottom-right corner. If an item is "
    "not visible, leave it out rather than guessing. Never box people or "
    "pets. Answer ONLY in the required JSON format."
)


def _ground_items(items, image_b64, W, H, url, model, num_ctx, timeout):
    """Ask a grounding model for one box per item phrase. Returns the raw
    regions list, or None on any failure (caller falls back to the main
    model's own boxes - a broken grounder must never erase detections)."""
    listing = "\n".join(f"- {it}" for it in items)
    messages = [
        {"role": "system", "content": GROUND_SYSTEM_PROMPT},
        {"role": "user",
         "content": (f"The image is {W}x{H} pixels. Draw one bounding box "
                     f"around each of these items:\n{listing}"),
         "images": [image_b64]},
    ]
    payload = {"model": model, "messages": messages, "format": GROUND_SCHEMA,
               "options": {"temperature": 0.0, "num_ctx": num_ctx,
                           "num_predict": 400},
               "keep_alive": _GROUND_KEEP_ALIVE, "stream": False}
    try:
        resp = requests.post(f"{url}/api/chat", json=payload, timeout=timeout)
        if resp.status_code != 200:
            return None
        regions = json.loads(resp.json()["message"]["content"]).get("regions")
        return regions if isinstance(regions, list) else None
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        return None


def _downscale_jpeg(jpeg, max_side=640):
    """Shrink a JPEG so its longest side is <= max_side, re-encode, and return
    (jpeg_bytes, (width, height)).

    A room is just as judgeable at 640px, but a smaller image means far fewer
    vision tokens -> much less GPU working memory and a faster check. Display
    still uses the full-res frame; only the copy sent to the model shrinks.
    """
    try:
        im = Image.open(BytesIO(jpeg))
        # draft() lets libjpeg decode straight to a reduced DCT scale (1/2, 1/4...),
        # so a 1280x720 frame never materializes at full size in RAM - the decode
        # itself is smaller and faster; LANCZOS then only closes the last gap.
        im.draft("RGB", (max_side, max_side))
        im = im.convert("RGB")
        w, h = im.size
        scale = max_side / max(w, h)
        if scale < 1.0:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                           Image.LANCZOS)
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), im.size
    except Exception:
        # If anything goes wrong, fall back to the original frame untouched.
        try:
            return jpeg, Image.open(BytesIO(jpeg)).size
        except Exception:
            return jpeg, (max_side, max_side)


def check(current_jpeg, prompt, reference_jpeg=None,
          url="http://127.0.0.1:11434",
          model="qwen3-vl:8b-instruct",  # smaller option: "qwen2.5vl:3b"
          timeout=180,
          keep_alive="30s", num_ctx=1536, max_image_side=640, num_gpu=None,
          verify=True, ground_model=None):
    """Ask the vision model whether the watch condition is met.

    keep_alive: how long Ollama keeps the model in VRAM after this call.
                "30s" frees the GPU between checks; "0" unloads immediately;
                "30m" keeps it resident (fast but holds ~10GB).
    num_ctx:    context window. Lower = less KV-cache VRAM.
    num_gpu:    how many model layers to put on the GPU. None = let Ollama decide
                (all on GPU). 0 = run entirely on the CPU -> ZERO GPU VRAM, but the
                check is much slower (CPU inference) and uses system RAM instead.
                A small number (e.g. 12) is a middle ground: partial GPU, less VRAM.
    verify:     re-judge each flagged region as a full-res close-up crop before
                trusting it (kills duvet-as-clothes / rug-as-clutter mistakes).
                Costs a few extra seconds per flagged item, zero extra VRAM.
    ground_model: optional second vision model that ONLY places the bounding
                boxes for the items the main model found (useful when the
                main model judges well but boxes badly). Loads only when
                items were found, unloads after 30s. None/"" = keep the
                main model's own boxes.

    Returns dict: {alert: bool, summary: str, items: [str], regions: [...],
    confidence: str, vetoed: [str] (labels the close-up pass rejected)}
    """
    # Downscale before sending - this is the big GPU-memory and speed win.
    cur_small, (W, H) = _downscale_jpeg(current_jpeg, max_image_side)
    images = []
    if reference_jpeg is not None:
        ref_small, _ = _downscale_jpeg(reference_jpeg, max_image_side)
        images.append(base64.b64encode(ref_small).decode())
        user_text = (
            "Image 1 is the REFERENCE photo showing this scene in its normal/clean "
            "state. Image 2 is the CURRENT camera frame. Compare them.\n\n"
            f"Watch condition: {prompt}\n\n"
            "Ignore differences in lighting, shadows, time of day, and camera "
            "noise. Only report meaningful differences relevant to the watch "
            "condition."
        )
    else:
        user_text = f"Look at the camera frame.\n\nWatch condition: {prompt}"
    # Telling the model the exact frame size improves its pixel-box grounding.
    user_text += f"\n\nThe current frame is {W}x{H} pixels."
    images.append(base64.b64encode(cur_small).decode())

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text, "images": images},
    ]
    # The vision model occasionally degenerates into a token-repetition loop
    # (e.g. items full of "."), producing truncated/invalid JSON. temperature 0
    # is deterministic, so a retry must nudge temperature to break the loop.
    verdict = None
    content = ""
    for attempt in range(2):
        options = {
            "temperature": 0.0 if attempt == 0 else 0.5,
            "num_ctx": num_ctx,
            "num_predict": 900,      # cap runaway generation (areas + regions
                                     # for a genuinely messy room fit in ~700;
                                     # headroom on top of that)
        }
        if num_gpu is not None:
            options["num_gpu"] = num_gpu   # 0 = CPU only (no GPU VRAM); N = N layers on GPU
        payload = {
            "model": model,
            "messages": messages,
            "format": VERDICT_SCHEMA,
            "options": options,
            "keep_alive": keep_alive,
            "stream": False,
        }
        try:
            resp = requests.post(f"{url}/api/chat", json=payload,
                                 timeout=timeout)
        except requests.ConnectionError as e:
            # Setup problem, not a bug - tell the user how to fix it.
            raise VisionError(errors.ollama_unreachable_message(url)) from e
        except requests.RequestException as e:
            raise VisionError(f"Ollama request failed: {e}") from e
        if resp.status_code != 200:
            raise VisionError(f"Ollama returned {resp.status_code}: {resp.text[:300]}")
        content = resp.json()["message"]["content"]
        try:
            verdict = json.loads(content)
            break
        except json.JSONDecodeError:
            continue   # retry with a temperature nudge
    if verdict is None:
        raise VisionError(f"Model returned non-JSON (after retry): {content[:300]}")

    # schema names verdict_alert/verdict_confidence exist only to force the
    # verdict to be filled LAST (alphabetical order); the app speaks alert/
    # confidence
    if "verdict_alert" in verdict:
        verdict["alert"] = verdict.pop("verdict_alert")
    if "verdict_confidence" in verdict:
        verdict["confidence"] = verdict.pop("verdict_confidence")
    verdict.setdefault("alert", False)
    verdict.setdefault("summary", "")
    verdict.setdefault("items", [])
    verdict.setdefault("regions", [])
    verdict.setdefault("confidence", "low")

    # Clean the human-readable items: drop coordinate junk, repetition spam
    # (".", "...", single chars), and anything with no real word in it. Then
    # fold in everything the per-area sweep found that the flat list missed.
    # Cap the list so a degenerate response can't flood the alert/voice.
    verdict["items"] = [it.strip() for it in verdict["items"] if _good_item(it)]
    verdict["items"] = _merge_area_items(verdict["items"],
                                         verdict.get("areas"))

    # Normalize bounding boxes to fractions using the SAME (downscaled) size the
    # model saw - fractions map cleanly back onto the full-res display frame.
    verdict["regions"] = _clean_regions(verdict["regions"], W, H,
                                        _prefers_norm1000(model))
    verdict["vetoed"] = []

    # Re-locate the found items with the dedicated grounding model, if one is
    # configured. Fail open: if the grounder returns nothing usable, the main
    # model's own boxes stay.
    if ground_model and verdict["items"]:
        raw = _ground_items(verdict["items"][:_GROUND_MAX_ITEMS], images[-1],
                            W, H, url, ground_model, num_ctx, timeout)
        grounded = _clean_regions(raw, W, H, _prefers_norm1000(ground_model))
        if grounded:
            verdict["regions"] = grounded

    # Second pass: show each flagged region back to the model as a full-res
    # close-up and let it veto its own mistakes. Same num_ctx/num_gpu as the main
    # call - changing them would make Ollama RELOAD the model (an SSD hit).
    if verify and verdict["items"] and verdict["regions"]:
        verify_options = {"temperature": 0.0, "num_ctx": num_ctx,
                          "num_predict": 150}
        if num_gpu is not None:
            verify_options["num_gpu"] = num_gpu
        results = []
        for region in verdict["regions"][:_VERIFY_MAX_REGIONS]:
            crop = _crop_jpeg(current_jpeg, region["box"])
            v = None
            if crop is not None:
                v = _verify_region(crop, region["label"], url, model,
                                   verify_options, keep_alive, timeout)
            results.append((region, v))
        # Regions beyond the cap pass through unverified (fail open).
        results.extend((r, None)
                       for r in verdict["regions"][_VERIFY_MAX_REGIONS:])
        verdict = apply_verification(verdict, results)
    return verdict


def unload(model="qwen3-vl:8b-instruct", url="http://127.0.0.1:11434"):
    """Tell Ollama to drop the model from VRAM right now (keep_alive=0)."""
    try:
        requests.post(f"{url}/api/generate",
                      json={"model": model, "keep_alive": 0}, timeout=10)
    except requests.RequestException:
        pass


def warm(model="qwen3-vl:8b-instruct", url="http://127.0.0.1:11434", keep_alive="24h",
         num_ctx=None):
    """Preload the model with one tiny generation so the first real check or voice
    reply isn't slow (avoids the ~30s cold-load on first use). Best-effort.

    num_ctx MUST match what the checks use - warming at a different context size
    makes Ollama reload the whole model on the first real check (double load)."""
    options = {"num_predict": 1}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
    try:
        requests.post(f"{url}/api/generate",
                      json={"model": model, "prompt": "ok", "stream": False,
                            "keep_alive": keep_alive, "options": options},
                      timeout=120)
    except requests.RequestException:
        pass
