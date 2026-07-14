#!/usr/bin/env python3
"""
AI brand enrichment for brandboost.

Sends a condensed digest of the Playwright scrape to the Shiprocket AI gateway
and merges the model's answer over the heuristic values from `derive()`.

Division of labour, and it is deliberate:

  Python decides everything it MEASURES  — header/body colours, cta radius,
  contrast math, and the logo treatment whenever the canvas luminance probe
  succeeded. A text model cannot beat a pixel average.

  The model decides everything that needs SEMANTICS — brand name, which of the
  repeated accent colours is the brand, which font is the body font, whether the
  top strip is a promo or a cookie notice, ad/hero copy, product names, tone,
  and (weakly) which image belongs in which slot.

Every value the model returns is re-validated here before it can reach the page.
`brandboost.PAGE` interpolates $brand and $body_font raw into a <style> block, so
an unvalidated hex or font name is a CSS injection, not a cosmetic bug.

The model never returns a URL. It returns an integer index into a list we built,
and we map it back. That removes URL hallucination as a class of failure.

Config (all env, no literals):
    SR_AI_GATEWAY_KEY   required
    SR_AI_GATEWAY_URL   default https://aigateway.shiprocket.in/api/v1/chat/completion
    SR_AI_MODEL         default gpt-5.4
    SR_AI_PROVIDER      default openai
    SR_AI_TIMEOUT       default 90 (seconds)
    SR_AI_WEB_SEARCH    "1" to enable (default off — it costs ~8.5k input tokens)
"""
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from urllib.parse import urlparse

import brandboost as bb


def _load_dotenv():
    """Populate os.environ from a sibling .env (KEY=VALUE lines). Real env vars
    win — .env only fills gaps, so prod secret managers stay authoritative."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


_load_dotenv()

GATEWAY_URL = os.environ.get(
    "SR_AI_GATEWAY_URL", "https://aigateway.shiprocket.in/api/v1/chat/completion"
)
MODEL = os.environ.get("SR_AI_MODEL", "gpt-5.4")
PROVIDER = os.environ.get("SR_AI_PROVIDER", "openai")
TIMEOUT = float(os.environ.get("SR_AI_TIMEOUT", "90"))
WEB_SEARCH = os.environ.get("SR_AI_WEB_SEARCH", "1") == "1"

MAX_IMAGES = 40
MAX_ACCENTS = 12
MAX_FONTS = 8
ALT_CAP = 80

# A colour used four times across an entire homepage is an accident, not an
# identity. Hammer's page yields a stray olive at count=4; without this floor the
# model dutifully elects it brand colour because it is the only eligible hex.
MIN_ACCENT_COUNT = 5

# A field is only trusted when the model scores its own confidence at or above
# this. A missing score counts as zero — silence is not confidence.
CONF_MIN = 0.5
# Image slots clear a lower bar: the fallback (geometry) is itself weak.
CONF_IMG_MIN = 0.4

# Copy caps. The page renders in a 430px column; these are the widths at which
# each string stops fitting, not arbitrary round numbers.
CAP_BRAND_NAME = 30
CAP_ANN = 120
CAP_HERO_LINE = 14
CAP_HERO_SUB = 70
CAP_AD_EYEBROW = 18
CAP_AD_L1 = 10
CAP_AD_L2 = 14
CAP_AD_SUB = 60
CAP_PROD_NAME = 22
CAP_PRICE = 12

LOGO_FILTERS = {
    "as-is": "",
    "darken": "filter:brightness(0);",
    "invert": "filter:brightness(0) invert(1);",
}


class AIConfigError(RuntimeError):
    """Misconfiguration — fail fast, do not silently degrade."""


class AIGatewayError(RuntimeError):
    """Runtime failure talking to the gateway — degrade to heuristics."""


# ----------------------------------------------------------------------------
# Digest — 57KB of scrape becomes ~4KB of decision-relevant signal
# ----------------------------------------------------------------------------
def _accent_digest(candidates):
    """Rank repeated colours and pre-compute sat/lum so the model never has to do
    colour arithmetic — it is bad at it and we already have the functions.

    Only colours `derive()` would itself elect are shown. Offering the model a
    candidate the heuristic rejected invites it to hand that candidate straight
    back, which is how Boult's page acquired the browser's default link-blue as
    its brand colour."""
    counts = defaultdict(int)
    for c in candidates or []:
        hx = bb._rgb_to_hex(c)
        if bb._accent_eligible(hx):
            counts[hx] += 1
    rows = [
        {"hex": hx, "count": n,
         "sat": round(bb._saturation(hx), 2), "lum": round(bb._luminance(hx), 2)}
        for hx, n in counts.items() if n >= MIN_ACCENT_COUNT
    ]
    rows.sort(key=lambda r: (-r["count"], -r["sat"]))
    return rows[:MAX_ACCENTS]


def image_pool(data):
    """Index-addressed image candidates. Returns (index->url, rows-for-model).

    Same filter as `select_images` so the model and the fallback reason about
    the same set."""
    imgs = [
        i for i in (data.get("images") or [])
        if (i.get("w") or 0) > 80 and (i.get("h") or 0) > 80
        and "logo" not in (i.get("src") or "").lower()
        and "logo" not in (i.get("alt") or "").lower()
    ]
    imgs.sort(key=lambda i: i["w"] * i["h"], reverse=True)
    imgs = imgs[:MAX_IMAGES]

    pool, rows = {}, []
    for idx, i in enumerate(imgs):
        pool[idx] = i["src"]
        rows.append({
            "i": idx, "w": i["w"], "h": i["h"],
            "ratio": round(i["w"] / i["h"], 2) if i["h"] else None,
            "alt": (i.get("alt") or "").strip()[:ALT_CAP],
        })
    return pool, rows


def _font_digest(data):
    out, seen = [], set()
    for f in (data.get("fonts") or []):
        raw = (f.get("family") or "").strip()
        if not raw or bb.ICON_FONT_RE.search(raw):
            continue
        fam = bb.FONT_BAD_RE.sub("", raw).strip()
        if not fam or fam.lower() in seen:
            continue
        seen.add(fam.lower())
        weight = re.sub(r"[^0-9]", "", str(f.get("weight") or "400")) or "400"
        out.append({"family": fam, "weight": weight})
        if len(out) >= MAX_FONTS:
            break

    computed = bb._first_font_family(data.get("bodyFont"))
    if computed and computed.lower() not in seen:
        out.insert(0, {"family": computed, "weight": "400"})
    return out


def build_digest(data, url):
    pool, rows = image_pool(data)
    with_alt = sum(1 for r in rows if r["alt"])
    return pool, {
        "url": url,
        "domain": urlparse(url).netloc.replace("www.", ""),
        "og_site_name": (data.get("ogSiteName") or "")[:80],
        "logo_alt": (data.get("logoAlt") or "")[:60],
        "logo_src": (data.get("logoSrc") or "")[:200],
        # null means the canvas probe was blocked (CORS) — only then may the
        # model choose a logo treatment.
        "logo_pixel_luminance": data.get("logoLum"),
        "logo_pixel_opacity": data.get("logoOpaque"),
        "header_bg": bb._rgb_to_hex(data.get("headerBg", "")),
        "body_bg": bb._rgb_to_hex(data.get("bodyBg", "")),
        "body_text": bb._rgb_to_hex(data.get("bodyColor", "")),
        "body_font_computed": (data.get("bodyFont") or "")[:120],
        "cta_bg": bb._rgb_to_hex(data.get("ctaBg", "")),
        "cta_color": bb._rgb_to_hex(data.get("ctaColor", "")),
        "accent_candidates": _accent_digest(data.get("accentCandidates")),
        "fonts": _font_digest(data),
        "ann_text": (data.get("annText") or "")[:200],
        "ann_bg": bb._rgb_to_hex(data.get("annBg", "")),
        "social": sorted((data.get("social") or {}).keys()),
        "images": rows,
        "images_total": len(rows),
        "images_with_alt": with_alt,
    }


# ----------------------------------------------------------------------------
# The wrapper prompt
# ----------------------------------------------------------------------------
SYSTEM_PROMPT = """\
You are a brand identity extractor. You receive a JSON digest of a scraped
e-commerce homepage and return ONE JSON object describing that brand's visual
identity. Return JSON only — no prose, no markdown fences.

RULES YOU MUST NOT BREAK
1. Never invent a URL. Refer to images only by the integer `i` given in
   `digest.images`. If no image fits a slot, return null for that slot.
2. Never invent a hex colour. Every colour you return must appear verbatim in
   the digest, or be "#ffffff" or "#111111".
3. Never invent a font family. `body_font` must be a `family` string present in
   `digest.fonts`, or null to fall back to the system stack.
4. Every field you fill gets an entry in `confidence` from 0.0 to 1.0. Guessing
   with high confidence is the worst possible failure: a low score makes the
   caller use its own measurement instead, which is often correct. A field with
   no confidence entry is discarded. Score honestly.

brand_color
  Choose from `accent_candidates` or `cta_bg`. Nothing else is accepted — the
  caller discards any other hex, including one you invent.
  `accent_candidates` is already filtered to plausible colours and ranked by
  `count`, the number of times the colour appears on the page. The brand colour
  is the one the site REPEATS, not the most saturated: prefer high `count`.
  Return null, with `confidence.brand_color` below 0.5, when:
    - `accent_candidates` is empty, or
    - the brand is genuinely achromatic (a black-and-white fashion label), or
    - nothing in the list looks like this brand's real colour.
  The caller derives a correct black on its own. An honest null is worth more
  than a colour you are not sure about — a wrong brand colour repaints the
  footer, the badges and the whole ad block.

brand_name
  Prefer `og_site_name`, then `logo_alt`, then `domain`. Strip a tagline after a
  "|" or an em-dash, but keep in-word hyphens: "Fire-Boltt" is a name, not a
  name plus a tagline. Never exceed 30 characters.

announcement
  `ann_text` was scraped from the top strip of the page. Set
  `is_real_promo: false` if it is a cookie notice, privacy or policy link, a
  slider control ("Next slide", "Pause"), or a login prompt. Set true ONLY for
  a genuine offer, shipping promise, or campaign line. When true, echo the copy
  back cleaned up, under 120 characters.

logo_treatment
  Return a value ONLY if `logo_pixel_luminance` is null. When it is a number the
  caller has measured the logo's actual pixels and will ignore you. When it is
  null, infer from `logo_src` and `logo_alt`: "invert" for a dark logo on a dark
  header, "darken" for a white/light logo on a light header, else "as-is".

copy you write yourself (ad, hero)
  Match the brand's register. A premium audio brand and a value fast-fashion
  retailer do not speak the same way. No emoji, ever. Never promise a discount
  that is not evidenced somewhere in the digest.

  Every limit below is a hard character count in a 430px-wide phone column.
  Copy that overruns is cut at the last whole word, so an over-long line loses
  its ending. Write to the limit, not past it.

    hero.headline_l1   <= 14   line one of a 26px all-caps hero headline
    hero.headline_l2   <= 14   line two; the two lines read as one sentence
    hero.subcopy       <= 70   one supporting line under the headline
    ad.eyebrow         <= 18   a tiny 9px uppercase LABEL above the ad, at 60%
                               opacity. It names the offer — "Limited Offer",
                               "New Drop", "Festive Sale". It is NOT the brand
                               name and NOT a product category.
    ad.line1           <= 10   line one of the 18px ad headline
    ad.line2           <= 14   line two of the ad headline
    ad.sub             <= 60   one 10px line under the ad headline

products
  Four plausible product names for this brand's actual catalogue, with rupee
  prices in the band this brand really sells at. A value retailer does not sell
  a Rs 12,000 t-shirt.

image slots
  `hero` wants a wide landscape image (ratio > 1.6). `products` wants four
  portrait images (ratio < 0.9). `instagram_indices` wants five roughly square
  images. `showcase_index` wants one wide lifestyle image.
  Read `images_with_alt` against `images_total` first. If most alt text is
  empty, you are guessing from dimensions alone and the caller's own geometry
  heuristic is at least as good: score `confidence.images` below 0.4 and return
  null indices. Filenames containing UUIDs or hashes carry no information — do
  not pretend to read them.

OUTPUT SHAPE
{
  "brand_name": "string",
  "brand_color": "#rrggbb",
  "body_font": "string or null",
  "logo_treatment": "as-is | darken | invert | null",
  "announcement": {"is_real_promo": bool, "copy": "string"},
  "ad": {"eyebrow": "...", "line1": "...", "line2": "...", "sub": "..."},
  "hero": {"image_index": int|null, "headline_l1": "...", "headline_l2": "...",
           "subcopy": "..."},
  "products": [{"image_index": int|null, "name": "...", "price": "Rs 1,199"}],
  "showcase_index": int|null,
  "instagram_indices": [int, ...],
  "tone": "short description of the brand's voice",
  "confidence": {"brand_name": 0.0, "brand_color": 0.0, "body_font": 0.0,
                 "announcement": 0.0, "ad": 0.0, "hero": 0.0, "images": 0.0},
  "notes": "anything the caller should know, especially what you could not tell"
}"""


# ----------------------------------------------------------------------------
# Gateway
# ----------------------------------------------------------------------------
FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _loads_lenient(text):
    """Models fence their JSON no matter how firmly you ask them not to."""
    if not text or not text.strip():
        raise AIGatewayError("gateway returned an empty completion")
    s = FENCE_RE.sub("", text.strip())
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise AIGatewayError("no JSON object in completion: %.120r" % text)
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AIGatewayError("malformed JSON in completion: %s" % exc)


def call_gateway(system_prompt, user_prompt, web_search=None):
    key = os.environ.get("SR_AI_GATEWAY_KEY")
    if not key:
        raise AIConfigError(
            "SR_AI_GATEWAY_KEY is not set. Export it, or pass --no-ai to run on "
            "heuristics alone."
        )
    payload = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "model": MODEL,
        "provider": PROVIDER,
        # Extraction, not creative writing. At 0.7 the same site yields a
        # different brand colour on consecutive runs.
        "temperature": 0.2,
        "max_tokens": 2000,
        # You cannot json.loads() an SSE stream, and nothing here is displayed
        # token by token.
        "stream": False,
    }
    if WEB_SEARCH if web_search is None else web_search:
        payload["tools"] = [{"type": "web_search", "search_context_size": "low"}]

    req = urllib.request.Request(
        GATEWAY_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AIGatewayError("HTTP %s from gateway" % exc.code)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AIGatewayError("gateway unreachable or unparseable: %s" % exc)

    if not body.get("success"):
        raise AIGatewayError(body.get("message") or "gateway returned success=false")
    summary = ((body.get("data") or {}).get("output") or {}).get("summary") or ""
    tokens = (body.get("data") or {}).get("tokens") or {}
    return _loads_lenient(summary), tokens


# ----------------------------------------------------------------------------
# Validation — nothing below trusts a single value from the model
# ----------------------------------------------------------------------------
def _clean(value, cap):
    """A display string: collapse whitespace, drop control chars, cap length.
    HTML escaping happens later in build_tracking_html."""
    if not isinstance(value, str):
        return ""
    s = re.sub(r"\s+", " ", value).strip()
    s = "".join(ch for ch in s if ch.isprintable())
    return s[:cap]


def _fit(value, cap):
    """Cap prose at a word boundary. A hard slice ships '...built for everyday
    li' to a real customer; the model overruns its limit often enough that this
    cannot be left to the prompt alone."""
    s = _clean(value, cap * 3)
    if len(s) <= cap:
        return s
    cut = s[:cap + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else s[:cap]).rstrip(" ,;:—-")


def _confident(ai, field, floor=CONF_MIN):
    c = (ai.get("confidence") or {}).get(field)
    return isinstance(c, (int, float)) and not isinstance(c, bool) and c >= floor


def _index(value, pool):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in pool else None


def _apply_colour(v, data, ai, applied):
    if not _confident(ai, "brand_color"):
        return
    hx = ai.get("brand_color")
    if not isinstance(hx, str):
        return
    hx = hx.strip().lower()
    if not bb.HEX_RE.match(hx):
        return
    # Three gates, and all three are load-bearing:
    #   _accent_eligible — the same filter derive() applies, so the model cannot
    #     resurrect a colour the heuristic already threw away.
    #   membership       — it must be a colour actually observed on the page, in
    #     the shortlist we showed it. No inventing hexes.
    #   _usable_brand    — it has to survive as a footer fill behind white text.
    allowed = {r["hex"] for r in _accent_digest(data.get("accentCandidates"))}
    cta = bb._rgb_to_hex(data.get("ctaBg", ""))
    if cta:
        allowed.add(cta)
    if hx not in allowed:
        return
    if bb._accent_eligible(hx) and bb._usable_brand(hx):
        v["brand"] = hx
        v["light"] = bb._lighten(hx, 0.88)
        applied.append("brand_color")


def _apply_font(v, data, ai, applied):
    if not _confident(ai, "body_font"):
        return
    fam = ai.get("body_font")
    if not isinstance(fam, str):
        return
    fam = bb.FONT_BAD_RE.sub("", fam.strip()).strip()
    if not fam:
        return
    # `@font-face{src:local(X)}` renders as the system stack when the browser
    # has no X. Only a family the page actually loaded can be honoured.
    allowed = {f["family"].lower() for f in _font_digest(data)}
    if fam.lower() not in allowed:
        return
    v["body_font"] = "'%s',system-ui,-apple-system,sans-serif" % fam
    applied.append("body_font")


def _apply_announcement(v, data, ai, applied):
    if not _confident(ai, "announcement"):
        return
    ann = ai.get("announcement")
    if not isinstance(ann, dict):
        return
    promo = ann.get("is_real_promo")
    if promo is False:
        # The whole point: the model just told us the strip was a cookie notice.
        v["ann_copy"], v["ann_bg"], v["ann_text_col"] = bb.DEFAULT_ANN
        applied.append("announcement(rejected)")
        return
    if promo is not True:
        return
    copy = _clean(ann.get("copy"), CAP_ANN)
    raw_bg = bb._rgb_to_hex(data.get("annBg", ""))
    if len(copy) < 5 or not bb.HEX_RE.match(raw_bg or ""):
        return  # no measured colour to paint it on; keep whatever derive chose
    raw_fg = bb._rgb_to_hex(data.get("annColor", ""))
    v["ann_copy"] = copy
    v["ann_bg"] = raw_bg
    v["ann_text_col"] = (
        raw_fg if bb.HEX_RE.match(raw_fg or "") and bb._contrasts(raw_fg, raw_bg)
        else bb._readable_on(raw_bg)
    )
    applied.append("announcement")


def _apply_logo(v, data, ai, applied):
    """Only when the canvas probe was blocked. A measured pixel average beats a
    model reading a filename, every time."""
    if data.get("logoLum") is not None:
        return
    t = ai.get("logo_treatment")
    if isinstance(t, str) and t in LOGO_FILTERS and not v["header_dark"]:
        v["logo_filter"] = LOGO_FILTERS[t]
        applied.append("logo_treatment")


def _apply_copy(v, ai, applied):
    if _confident(ai, "brand_name"):
        name = _clean(ai.get("brand_name"), CAP_BRAND_NAME)
        if name:
            v["brand_name"] = name
            v["prefix"] = re.sub(r"[^A-Za-z]", "", name).upper()[:3] or "ORD"
            applied.append("brand_name")

    if _confident(ai, "hero"):
        hero = ai.get("hero") or {}
        l1 = _fit(hero.get("headline_l1"), CAP_HERO_LINE)
        l2 = _fit(hero.get("headline_l2"), CAP_HERO_LINE)
        if l1 and l2:
            v["hero_l1"], v["hero_l2"] = l1, l2
            applied.append("hero_copy")
        sub = _fit(hero.get("subcopy"), CAP_HERO_SUB)
        if sub:
            v["hero_sub"] = sub

    if _confident(ai, "ad"):
        ad = ai.get("ad") or {}
        l1 = _fit(ad.get("line1"), CAP_AD_L1)
        l2 = _fit(ad.get("line2"), CAP_AD_L2)
        if l1 and l2:
            v["ad_l1"], v["ad_l2"] = l1, l2
            applied.append("ad_copy")
        eyebrow = _fit(ad.get("eyebrow"), CAP_AD_EYEBROW)
        if eyebrow:
            v["ad_eyebrow"] = eyebrow
        sub = _fit(ad.get("sub"), CAP_AD_SUB)
        if sub:
            v["ad_sub"] = sub

    tone = _clean(ai.get("tone"), 120)
    if tone:
        v["tone"] = tone


def _apply_images(v, ai, picks, pool, applied):
    """Image slots move as a unit. A model that cannot tell a hero from a
    product shot also cannot name the product, so the AI product names only ship
    when the AI product images did."""
    if not _confident(ai, "images", CONF_IMG_MIN):
        return

    hero = _index((ai.get("hero") or {}).get("image_index"), pool)
    show = _index(ai.get("showcase_index"), pool)

    prods, names, prices = [], [], []
    for p in (ai.get("products") or [])[:4]:
        if not isinstance(p, dict):
            continue
        idx = _index(p.get("image_index"), pool)
        if idx is None:
            continue
        prods.append(bb._bump_quality(pool[idx], 400))
        names.append(_clean(p.get("name"), CAP_PROD_NAME) or "Best Seller")
        prices.append(_clean(p.get("price"), CAP_PRICE) or "Rs 999")

    igs, seen = [], set()
    for i in (ai.get("instagram_indices") or [])[:5]:
        idx = _index(i, pool)
        if idx is not None and idx not in seen:
            seen.add(idx)
            igs.append(bb._bump_quality(pool[idx], 200))

    if hero is not None:
        picks["hero"] = bb._bump_quality(pool[hero], 800)
        applied.append("hero_image")
    if show is not None:
        picks["showcase"] = bb._bump_quality(pool[show], 800)
        applied.append("showcase_image")
    # Two or four — the grid renders no other count.
    if len(prods) >= 4:
        picks["prods"] = prods[:4]
        v["prod_names"], v["prod_prices"] = names[:4], prices[:4]
        applied.append("product_images")
    elif len(prods) >= 2:
        picks["prods"] = prods[:2]
        v["prod_names"], v["prod_prices"] = names[:2], prices[:2]
        applied.append("product_images")
    if len(igs) >= 3:
        picks["ig"] = igs
        applied.append("instagram_images")


def merge(v, data, picks, ai, pool):
    """Merge validated model output over the heuristic values. Mutates copies."""
    v, picks = dict(v), dict(picks)
    applied = []
    _apply_colour(v, data, ai, applied)
    _apply_font(v, data, ai, applied)
    _apply_announcement(v, data, ai, applied)
    _apply_logo(v, data, ai, applied)
    _apply_copy(v, ai, applied)
    _apply_images(v, ai, picks, pool, applied)
    return v, picks, applied


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def enrich(data, url, v, picks, web_search=None):
    """Returns (v, picks, report). Never raises on a gateway failure — a page
    built from heuristics beats no page at all.

    A missing API key DOES raise: that is a misconfiguration, not an outage,
    and silently degrading would hide it forever.

    `web_search=None` keeps the env-var default; the tiered loop in brandkit.py
    passes False for the cheap pass and True only when escalating.
    """
    pool, digest = build_digest(data, url)
    user_prompt = "Site: %s\n\nDigest:\n%s" % (url, json.dumps(digest, indent=1))
    try:
        ai, tokens = call_gateway(SYSTEM_PROMPT, user_prompt, web_search=web_search)
    except AIGatewayError as exc:
        return v, picks, {"ok": False, "error": str(exc), "applied": []}

    if not isinstance(ai, dict):
        return v, picks, {"ok": False, "error": "model returned a non-object",
                          "applied": []}

    v, picks, applied = merge(v, data, picks, ai, pool)
    return v, picks, {
        "ok": True, "applied": applied, "tokens": tokens,
        "notes": _clean(ai.get("notes"), 400),
        "tone": v.get("tone", ""),
        "confidence": ai.get("confidence") or {},
        "digest_bytes": len(user_prompt),
    }



# ----------------------------------------------------------------------------
# Iterative refinement — propose → judge → revise, bounded by SR_AI_MAX_LOOPS.
# The judge is a second, cheaper opinion with a different job description;
# a proposal only ships early when the judge signs off, and the loop always
# terminates at the depth cap with the best proposal so far.
# ----------------------------------------------------------------------------
MAX_AI_LOOPS = max(1, int(os.environ.get("SR_AI_MAX_LOOPS", "3")))


def _acc_tokens(total, tokens):
    for k in ("input", "output", "total"):
        total[k] = (total.get(k) or 0) + ((tokens or {}).get(k) or 0)
    return total


# ----------------------------------------------------------------------------
# Section curation — WHICH sections a brand's tracking page should carry, in
# what order, with copy in the brand's own voice. A food-delivery brand must
# not get a "NEW ARRIVALS" strip. Output is whitelist-validated against
# brandboost.SECTION_CATALOG — the model never emits markup.
# ----------------------------------------------------------------------------
CURATE_PROMPT = """\
You curate post-purchase order-TRACKING pages for consumer brands.
Given brand facts, choose which sections make the best tracking page for THIS
brand, and in what order. Return ONE JSON object, no prose, no fences:
{"sections": ["id", ...],
 "copy": {"hero_l1": "string or null", "hero_l2": "string or null",
          "hero_sub": "string or null", "ad_eyebrow": "string or null",
          "ad_l1": "string or null", "ad_l2": "string or null",
          "ad_sub": "string or null"},
 "notes": "one short line on the reasoning"}
Section vocabulary (use ONLY these ids):
  ann        promo announcement bar
  header     logo header (mandatory)
  order      order id card (mandatory)
  status     delivery status + timeline (mandatory)
  products   product cross-sell grid ("You may also like")
  hero       big lifestyle banner with promo copy
  ad         coloured promo strip (default copy says NEW ARRIVALS)
  nps        recommend-to-a-friend survey
  showcase   large video-style image
  instagram  real instagram feed
  facebook   facebook page card
  experience delivery-experience emoji feedback
  help       support contacts
  footer     social footer
Ordering principles:
- header, order, status open the page, in that order (ann may sit above them).
- "experience" is quick engagement — place it right after status while the
  delivery is fresh, never at the very bottom.
- instagram/facebook are strong social proof: when available they MUST be
  included and placed prominently (right after the commerce sections, or
  after status when there are no commerce sections) — never below help.
- help and footer close the page. nps sits late but above help.
Content principles:
- Include ONLY sections that fit the brand's actual business. A restaurant,
  food-delivery, grocery, travel or services brand does not sell
  "NEW ARRIVALS" — for such brands drop products/hero/ad, or rewrite the copy
  to fit (e.g. a re-order prompt for a food brand).
- Only include instagram/facebook if the facts say they are available.
- copy values REPLACE existing text; null keeps it. hero/ad l1 and l2 max 18
  chars, ad_eyebrow max 24, subs max 80.
- Copy must sound like the brand's OWN campaign voice: use the tone, bio and
  about facts. Mirror their vocabulary and energy (playful, premium, techy…).
  Never invent discounts, offers or claims not present in the facts."""

CURATE_JUDGE_PROMPT = """\
You review a proposed section plan for a brand's order-tracking page.
Return ONE JSON object, no prose: {"approved": true/false,
 "problems": ["specific fixable problem", ...]}
Reject when ANY of these hold:
- shop-style promos (products/hero/ad with retail copy) on a brand that does
  not sell retail products (restaurant/delivery/travel/services)
- instagram or facebook listed as available in the facts but missing from the
  plan, or buried below help/footer
- "experience" placed at the bottom instead of near the delivery status
- copy that ignores the brand's stated tone/voice, exceeds the length caps
  (l1/l2 18 chars, eyebrow 24, subs 80), or invents offers/claims
- header/order/status missing or not near the top
Otherwise approve. Problems must be specific enough to act on."""

_COPY_CAPS = {"hero_l1": 18, "hero_l2": 18, "hero_sub": 80,
              "ad_eyebrow": 24, "ad_l1": 18, "ad_l2": 18, "ad_sub": 80}


def _clean_curation(ai):
    if not isinstance(ai, dict):
        return None
    seen = set()
    sections = [s for s in (ai.get("sections") or [])
                if isinstance(s, str) and s in bb.DEFAULT_ORDER
                and not (s in seen or seen.add(s))]
    if not sections:
        return None
    for must in ("status", "order", "header"):      # tracking essentials
        if must not in sections:
            sections.insert(0, must)
    head = [s for s in ("ann", "header") if s in sections]
    sections = head + [s for s in sections if s not in ("ann", "header")]

    copy = {}
    raw_copy = ai.get("copy") if isinstance(ai.get("copy"), dict) else {}
    for k, cap in _COPY_CAPS.items():
        val = raw_copy.get(k)
        if isinstance(val, str) and val.strip():
            fitted = _fit(val, cap)
            if fitted:
                copy[k] = fitted
    clean = {"sections": sections, "notes": _clean(ai.get("notes"), 200)}
    if copy:
        clean["copy"] = copy
    return clean


def curate(facts, max_loops=None):
    """facts → (clean | None, tokens_total, meta) where clean is
    {'sections': [...], 'copy': {...}, 'notes': str}. Runs propose → judge →
    revise cycles until the judge approves or the loop cap is hit; the last
    proposal ships either way (a slightly-off plan beats no plan)."""
    depth = max_loops or MAX_AI_LOOPS
    tokens_total = {}
    facts_json = json.dumps(facts, indent=1, ensure_ascii=False)
    clean, problems, calls, approved = None, [], 0, False

    for _ in range(depth):
        prompt = "Brand facts:\n%s" % facts_json
        if clean and problems:
            prompt += ("\n\nYour previous plan:\n%s\n\nA reviewer rejected it "
                       "for these reasons — fix ALL of them:\n- %s"
                       % (json.dumps(clean, ensure_ascii=False),
                          "\n- ".join(problems)))
        ai, tokens = call_gateway(CURATE_PROMPT, prompt, web_search=False)
        _acc_tokens(tokens_total, tokens)
        calls += 1
        proposed = _clean_curation(ai)
        if proposed is None:
            break                                  # unusable answer; keep last
        clean = proposed

        judge_prompt = ("Brand facts:\n%s\n\nProposed plan:\n%s"
                        % (facts_json, json.dumps(clean, ensure_ascii=False)))
        verdict, jtokens = call_gateway(CURATE_JUDGE_PROMPT, judge_prompt,
                                        web_search=False)
        _acc_tokens(tokens_total, jtokens)
        calls += 1
        if isinstance(verdict, dict) and verdict.get("approved"):
            approved = True
            break
        problems = [p for p in (verdict.get("problems") or [])
                    if isinstance(p, str)][:6] if isinstance(verdict, dict) else []
        if not problems:
            break                                  # judge unusable; stop looping

    return clean, tokens_total, {"calls": calls, "approved": approved}


# ----------------------------------------------------------------------------
# Image vetting — the mined pool contains decorative junk (background
# gradients, category tiles, promo strips). The model re-picks hero /
# products / showcase from the pool by URL + alt + size, then a judge pass
# checks the choices; bounded by the same loop cap.
# ----------------------------------------------------------------------------
VET_PROMPT = """\
You pick the best images for a brand's order-tracking page from a numbered
candidate list (url tail, alt text, width x height). Return ONE JSON object:
{"hero_index": int|null, "product_indices": [int, ...],
 "showcase_index": int|null, "confidence": 0.0-1.0, "notes": "one line"}
Rules:
- hero: ONE wide lifestyle/product photo. White promo copy is overlaid on
  its lower third at render time, so AVOID campaign/landing-page banners
  that already carry baked-in headline text (urls with landing-page, banner,
  campaign, sale, collab, hero-text hints) and avoid images that are mostly
  bright/white in the lower half — clean photography beats designed banners.
- product_indices: up to 4 images that each clearly show ONE product
  (portrait product shots are ideal). All four must be DIFFERENT products.
- showcase: one rich wide lifestyle image (may be null).
- NEVER pick decorative assets: backgrounds, gradients, patterns, spacers,
  category/gender tiles, app-download banners, payment/offer strips, logos,
  icons. Filenames with bg/background/banner/grid/select/sale-strip are
  suspect — judge by what the URL and alt actually describe.
- Prefer images whose alt text names a real product.
- null / empty beats a bad pick. Set confidence honestly."""

VET_JUDGE_PROMPT = """\
You review images chosen for a brand's tracking page. Given the same
candidate list and the chosen indices, return ONE JSON object, no prose:
{"approved": true/false, "problems": ["...", ...]}
Reject when a chosen image is plainly a background/gradient/category tile/
promo strip/logo, when the hero is a designed campaign/landing-page banner
that carries its own baked-in headline text (white overlay copy will collide
with it), when two product picks look like the same asset, or when a clearly
better product photo exists in the list. Otherwise approve."""


def _vet_candidates(pool, cap=60):
    """Compact rows — this list is resent on every loop iteration, so its
    size multiplies straight into the input-token bill."""
    rows = []
    for i, im in enumerate(pool[:cap]):
        tail = (im.get("src") or "")[-70:]
        rows.append({"i": i, "url": tail, "alt": (im.get("alt") or "")[:50],
                     "size": "%dx%d" % (im.get("w") or 0, im.get("h") or 0)})
    return rows


def vet_images(facts, pool, max_loops=None):
    """→ (picks | None, tokens_total, meta). picks = {"hero": url|None,
    "prods": [urls], "showcase": url|None}. None when the model never
    produced a usable selection — caller keeps the heuristic picks."""
    if not pool:
        return None, {}, {"calls": 0, "approved": False}
    depth = max_loops or MAX_AI_LOOPS
    cands = _vet_candidates(pool)
    base = ("Brand facts:\n%s\n\nCandidates:\n%s"
            % (json.dumps(facts, ensure_ascii=False),
               json.dumps(cands, indent=0, ensure_ascii=False)))
    tokens_total = {}
    picks, problems, calls, approved = None, [], 0, False

    def _idx(val):
        return val if isinstance(val, int) and 0 <= val < len(cands) else None

    for _ in range(depth):
        prompt = base
        if picks is not None and problems:
            prompt += ("\n\nYour previous selection was rejected:\n- %s\n"
                       "Pick again and fix ALL problems." % "\n- ".join(problems))
        ai, tokens = call_gateway(VET_PROMPT, prompt, web_search=False)
        _acc_tokens(tokens_total, tokens)
        calls += 1
        if not isinstance(ai, dict):
            break
        hero_i = _idx(ai.get("hero_index"))
        seen = set()
        prod_i = []
        for x in (ai.get("product_indices") or [])[:4]:
            x = _idx(x)
            if x is not None and x not in seen:
                seen.add(x)
                prod_i.append(x)
        show_i = _idx(ai.get("showcase_index"))
        picks = {"hero_i": hero_i, "prod_i": prod_i, "show_i": show_i}

        judge_prompt = ("%s\n\nChosen: %s"
                        % (base, json.dumps(picks, ensure_ascii=False)))
        verdict, jtokens = call_gateway(VET_JUDGE_PROMPT, judge_prompt,
                                        web_search=False)
        _acc_tokens(tokens_total, jtokens)
        calls += 1
        if isinstance(verdict, dict) and verdict.get("approved"):
            approved = True
            break
        problems = [p for p in (verdict.get("problems") or [])
                    if isinstance(p, str)][:6] if isinstance(verdict, dict) else []
        if not problems:
            break

    if picks is None:
        return None, tokens_total, {"calls": calls, "approved": approved}
    out = {
        "hero": pool[picks["hero_i"]]["src"] if picks["hero_i"] is not None else None,
        "prods": [pool[i]["src"] for i in picks["prod_i"]],
        "showcase": pool[picks["show_i"]]["src"] if picks["show_i"] is not None else None,
    }
    return out, tokens_total, {"calls": calls, "approved": approved}
