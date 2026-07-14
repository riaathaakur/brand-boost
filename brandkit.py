#!/usr/bin/env python3
"""
Brand Kit — production pipeline for Shiprocket branded tracking pages.

    python3 brandkit.py <url> [slug] [--no-ai] [--html]

No browser. One HTML GET + a handful of small asset GETs, then a tiered
escalation loop that spends money only when the cheap tier failed:

    Tier 0  static extraction        ~1s, no tokens      always
    Tier 0b proxy re-fetch           1 ScrapingDog       only if the direct
            (api.scrapingdog.com)    credit              fetch failed or came
                                                         back image/asset-poor
    Tier 1  AI gateway, digest only  ~2k tokens          unless --no-ai
    Tier 2  AI gateway + web_search  ~10k tokens         only if earlier tiers
                                                         left color/font/name weak

The output is a validated JSON *brand kit* (<slug>_brandkit.json) shaped for
resources/views/tracking.blade.php. In production this runs once per seller in
a queue worker, the kit is cached (Redis/DB, ~30 day TTL), and page renders
never touch this pipeline — Blade only echoes the kit.

Everything downstream of extraction (colour math, validation, AI merge) is
shared with brandboost.py — this module only replaces the Playwright layer by
emitting the exact same data shape.
"""
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageFile

import brandboost as bb
from brandai import _load_dotenv

_load_dotenv()          # SCRAPINGDOG_* and SR_AI_* live in .env locally

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      "Accept-Language": "en-IN,en;q=0.9"}
FETCH_TIMEOUT = 12
CSS_TIMEOUT = 8
MAX_HTML = 3_000_000
MAX_CSS = 600_000
MAX_STYLESHEETS = 4
MAX_IMG_PROBES = 24
PROBE_BYTES = 65_536
MAX_LOGO_BYTES = 1_500_000

# Weights turn a single static observation into the "repetition count" that
# _pick_accent / _accent_digest rank by. A named --brand variable is worth more
# than a colour that shows up once in a button rule.
W_CSS_VAR = 10
W_THEME_META = 8
W_LOGO_COLOR = 8
W_CTA_RULE = 6
W_ACCENT_RULE = 3

HEX_ANY_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
COLOR_VAL_RE = re.compile(r"(#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|rgba?\([^)]*\))")
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
CSS_AT_OPEN_RE = re.compile(r"@(?:media|supports|layer|container)[^{]*\{")
CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
FONT_FACE_RE = re.compile(r"@font-face\s*\{([^}]*)\}", re.I)
VAR_DECL_RE = re.compile(r"--([\w-]+)\s*:\s*([^;]+)")
VAR_USE_RE = re.compile(r"var\(\s*--([\w-]+)[^)]*\)")
GOOGLE_FONTS_RE = re.compile(r"fonts\.googleapis\.com/css2?\?([^\"'>]+)")
ACCENT_SEL_RE = re.compile(r"btn|button|cta|add-to-cart|primary|accent|brand|badge|pill", re.I)
CTA_SEL_RE = re.compile(r"add-to-cart|btn-primary|product-form__submit|payment-button|primary-btn|buy", re.I)
HEADER_SEL_RE = re.compile(r"(^|[,\s>])(header|\.header\b|\.site-header|\.navbar|nav\b|\.top-header)", re.I)
BRAND_VAR_RE = re.compile(r"primary|accent|brand|button|main|theme", re.I)
# Vendored-library variables ship their OWN defaults (--swiper-theme-color is
# #007aff on every site that bundles Swiper) — never brand evidence.
LIB_VAR_RE = re.compile(
    r"swiper|splide|glide|slick|flickity|plyr|fancybox|lightbox|drift|"
    r"paypal|klarna|razorpay|stripe|whatsapp|facebook|twitter|instagram|"
    r"youtube|judgeme|yotpo|loox|gorgias|tidio|intercom", re.I)
# Stock framework palette values (Swiper/Bootstrap/link blues). Only a strong
# direct signal (CTA rule, logo pixels) may elect these — never a var boost.
FRAMEWORK_HEXES = {"#007aff", "#007bff", "#0d6efd", "#0069d9", "#0056b3",
                   "#0000ee", "#1e90ff", "#4285f4"}
FONT_VAR_RE = re.compile(r"font.*(body|base|primary|main|text)|(body|base|primary|main|text).*font", re.I)
ANN_CLASS_RE = re.compile(r"announcement|promo-bar|top-bar|topbar|marquee|ticker", re.I)
SOCIAL_KEYS = ("instagram", "facebook", "youtube", "twitter", "x.com")


def _norm_hex(value):
    """'#abc' → '#aabbcc'; passes rgb()/6-digit through for bb._rgb_to_hex."""
    s = (value or "").strip()
    m = re.match(r"^#([0-9a-fA-F]{3})$", s)
    if m:
        return "#" + "".join(ch * 2 for ch in m.group(1))
    return s


# ----------------------------------------------------------------------------
# Fetch
# ----------------------------------------------------------------------------
def _get(session, url, timeout, cap):
    r = session.get(url, timeout=timeout, headers=UA, stream=True)
    r.raise_for_status()
    buf, read = [], 0
    for chunk in r.iter_content(65_536):
        buf.append(chunk)
        read += len(chunk)
        if read > cap:
            break
    r.close()
    return b"".join(buf)


def fetch_page(session, url):
    body = _get(session, url, FETCH_TIMEOUT, MAX_HTML)
    return body.decode("utf-8", "replace")


def fetch_css(session, soup, base_url):
    """Inline <style> blocks plus the first few linked stylesheets. Third-party
    widget CSS (chat, reviews) is skipped — it only pollutes the colour pool."""
    chunks = [s.get_text() for s in soup.find_all("style")]
    fetched = 0
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href")
        if not href or fetched >= MAX_STYLESHEETS:
            continue
        full = urljoin(base_url, href)
        host = urlparse(full).netloc
        page_host = urlparse(base_url).netloc
        if host != page_host and not any(
                c in host for c in ("cdn.shopify.com", "shopifycdn", page_host.replace("www.", ""))):
            continue
        try:
            chunks.append(_get(session, full, CSS_TIMEOUT, MAX_CSS).decode("utf-8", "replace"))
            fetched += 1
        except requests.RequestException:
            continue
    return "\n".join(chunks)


# ----------------------------------------------------------------------------
# CSS mining
# ----------------------------------------------------------------------------
def parse_css(css):
    """One pass over the flattened CSS → var map, rule list, @font-face list."""
    css = CSS_COMMENT_RE.sub("", css)
    faces = []
    for body in FONT_FACE_RE.findall(css):
        fam = re.search(r"font-family\s*:\s*([^;]+)", body, re.I)
        wgt = re.search(r"font-weight\s*:\s*([^;]+)", body, re.I)
        src = re.search(r"src\s*:\s*([^;]+)", body, re.I)
        if fam and not (src and src.group(1).strip().startswith("data:")):
            faces.append({"family": fam.group(1).replace('"', "").replace("'", "").strip(),
                          "weight": (wgt.group(1).strip() if wgt else "400")})
    css = FONT_FACE_RE.sub("", css)
    css = CSS_AT_OPEN_RE.sub("", css)

    variables = {}
    rules = []
    for sel, body in CSS_RULE_RE.findall(css):
        for name, val in VAR_DECL_RE.findall(body):
            variables.setdefault(name.lower(), val.strip())
        rules.append((sel.strip(), body))
    return variables, rules, faces


def _resolve(value, variables, depth=0):
    if depth > 2 or not value:
        return value
    m = VAR_USE_RE.search(value)
    if not m:
        return value
    inner = variables.get(m.group(1).lower())
    return _resolve(inner, variables, depth + 1) if inner else value


def _decl(body, prop):
    m = re.search(r"(?:^|;)\s*%s\s*:\s*([^;]+)" % re.escape(prop), body, re.I)
    return m.group(1).strip() if m else ""


def mine_colors(variables, rules, data):
    """Fill accentCandidates / ctaBg / headerBg / body* from static CSS."""
    acc = data["accentCandidates"]

    for name, val in variables.items():
        if not BRAND_VAR_RE.search(name) or LIB_VAR_RE.search(name):
            continue
        val = _resolve(val, variables)
        for c in COLOR_VAL_RE.findall(val):
            hx = bb._rgb_to_hex(_norm_hex(c))
            if bb._accent_eligible(hx) and hx not in FRAMEWORK_HEXES:
                acc.extend([hx] * W_CSS_VAR)

    for sel, body in rules:
        sl = sel.lower()
        bg = _resolve(_decl(body, "background-color") or _decl(body, "background"),
                      variables)
        col = _resolve(_decl(body, "color"), variables)

        if not data["ctaBg"] and CTA_SEL_RE.search(sl):
            hx = bb._rgb_to_hex(_norm_hex((COLOR_VAL_RE.findall(bg or "") or [""])[0]))
            if hx:
                data["ctaBg"] = hx
                data["ctaColor"] = _norm_hex((COLOR_VAL_RE.findall(col or "") or [""])[0])
                radius = _resolve(_decl(body, "border-radius"), variables)
                if radius:
                    data["ctaRadius"] = radius.split()[0]

        if not data["headerBg"] and HEADER_SEL_RE.search(sel) and bg:
            hx = bb._rgb_to_hex(_norm_hex((COLOR_VAL_RE.findall(bg) or [""])[0]))
            if hx:
                data["headerBg"] = hx

        if re.match(r"^\s*body\b", sl):
            if not data["bodyBg"] and bg:
                data["bodyBg"] = _norm_hex((COLOR_VAL_RE.findall(bg) or [""])[0])
            if not data["bodyColor"] and col:
                data["bodyColor"] = _norm_hex((COLOR_VAL_RE.findall(col) or [""])[0])
            fam = _resolve(_decl(body, "font-family"), variables)
            if fam and not data["bodyFont"]:
                data["bodyFont"] = fam

        if ACCENT_SEL_RE.search(sl) and not LIB_VAR_RE.search(sl):
            weight = W_CTA_RULE if CTA_SEL_RE.search(sl) else W_ACCENT_RULE
            for raw in COLOR_VAL_RE.findall((bg or "") + " " + (col or "")):
                hx = bb._rgb_to_hex(_norm_hex(raw))
                if bb._accent_eligible(hx) and hx not in FRAMEWORK_HEXES:
                    acc.extend([hx] * weight)

    if not data["bodyFont"]:
        for name, val in variables.items():
            if FONT_VAR_RE.search(name):
                data["bodyFont"] = _resolve(val, variables)
                break


# ----------------------------------------------------------------------------
# Image probing — real dimensions without a browser
# ----------------------------------------------------------------------------
def _probe_size(session, url):
    """Read just enough bytes to learn width×height. ~10KB per image."""
    try:
        r = session.get(url, timeout=6, headers=UA, stream=True)
        parser = ImageFile.Parser()
        read = 0
        for chunk in r.iter_content(8192):
            parser.feed(chunk)
            read += len(chunk)
            if parser.image or read > PROBE_BYTES:
                break
        r.close()
        return parser.image.size if parser.image else None
    except Exception:
        return None


def _url_dims(src):
    w = h = 0
    m = re.search(r"[?&](?:w|width)=(\d+)", src)
    if m:
        w = int(m.group(1))
    m = re.search(r"[?&](?:h|height)=(\d+)", src)
    if m:
        h = int(m.group(1))
    # "..._600x600.jpg", "830x360_hash.jpg" — CDN filenames carry the size.
    # Lookarounds, not \b: the dims usually sit against underscores, and a
    # longer digit run (a version hash) must not bleed into the match.
    m = re.search(r"(?<!\d)(\d{2,4})x(\d{2,4})(?!\d)", src)
    if m and 50 <= int(m.group(1)) <= 4000 and 50 <= int(m.group(2)) <= 4000:
        w, h = w or int(m.group(1)), h or int(m.group(2))
    return w, h


def collect_images(session, soup, base_url):
    """<img> + og:image with real dimensions. Attribute dims are free; URL
    params are free; only the remainder costs a partial GET each."""
    rows, seen = [], set()

    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        src = urljoin(base_url, og["content"])
        rows.append({"src": src, "w": 0, "h": 0, "alt": "og:image"})
        seen.add(src)

    for im in soup.find_all("img"):
        src = (im.get("src") or im.get("data-src") or im.get("data-lazy-src")
               or im.get("data-original") or "")
        ss = im.get("srcset") or im.get("data-srcset") or ""
        if ss:
            best, score = "", -1.0
            for part in ss.split(","):
                bits = part.strip().split()
                if not bits or bits[0].startswith("data:"):
                    continue
                d = bits[1] if len(bits) > 1 else ""
                s = float(d[:-1]) if re.match(r"^[\d.]+w$", d) else \
                    float(d[:-1]) * 1000 if re.match(r"^[\d.]+x$", d) else 1.0
                if s > score:
                    score, best = s, bits[0]
            src = best or src
        if not src or src.startswith("data:"):
            continue
        src = urljoin(base_url, src)
        if src in seen:
            continue
        seen.add(src)
        try:
            w = int(float(im.get("width") or 0))
            h = int(float(im.get("height") or 0))
        except ValueError:
            w = h = 0
        if not (w and h):
            uw, uh = _url_dims(src)
            w, h = w or uw, h or uh
        rows.append({"src": src, "w": w, "h": h, "alt": (im.get("alt") or "").strip()})

    unknown = [r for r in rows if not (r["w"] and r["h"])][:MAX_IMG_PROBES]
    if unknown:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for row, size in zip(unknown, ex.map(lambda r: _probe_size(session, r["src"]),
                                                 unknown)):
                if size:
                    row["w"], row["h"] = size
    return [r for r in rows if r["w"] and r["h"]]


# ----------------------------------------------------------------------------
# Embedded-JSON image mining — JS-rendered sites (Next/Nuxt/SPA themes) ship
# few <img> tags but hundreds of image URLs inside __NEXT_DATA__, JSON-LD and
# inline state. Without a browser, THIS is where their images live.
# ----------------------------------------------------------------------------
# Backslashes stay IN the match (JSON-escaped "\/" paths); stripped after.
IMG_URL_RE = re.compile(
    r"https?:\\?/\\?/[^\"'\s<>)]+?\.(?:jpe?g|png|webp|gif)(?:\?[^\"'\s<>)]*)?", re.I)
JSON_LD_RE = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
# Chrome, not content: favicons, sprites, payment badges, country flags,
# loaders — and logos, which have their own slot.
JUNK_IMG_RE = re.compile(
    r"favicon|sprite|icons8|(?:^|[/._-])icons?(?:[/._-]|$)|logo|badge|payment|"
    r"flag|placeholder|loading|loader|spinner|pixel|1x1|blank|dummy|avatar",
    re.I)
MAX_MINED = 80
MAX_MINED_PROBES = 40


def mine_embedded_images(html_text, base_url, seen):
    """Ordered, deduped image URLs found anywhere in the page source.
    JSON-LD first (it labels real content), then the raw sweep."""
    found = []

    for m in JSON_LD_RE.finditer(html_text):
        try:
            stack = [json.loads(m.group(1))]
        except ValueError:
            continue
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                img = node.get("image")
                if isinstance(img, str):
                    found.append(img)
                elif isinstance(img, list):
                    found.extend(u for u in img if isinstance(u, str))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    found.extend(IMG_URL_RE.findall(html_text))

    out = []
    for u in found:
        u = urljoin(base_url, u.replace("\\/", "/"))
        if u in seen or JUNK_IMG_RE.search(u):
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= MAX_MINED:
            break
    return out


# ----------------------------------------------------------------------------
# Logo — pixel probe + dominant colours, same semantics as the canvas probe
# ----------------------------------------------------------------------------
LOGO_ATTRS_RE = re.compile(r"logo", re.I)


def find_logo(soup, base_url):
    scopes = [t for t in (soup.find("header"), soup.find(class_=re.compile(r"site-header|header", re.I)),
                          soup.find("nav"), soup) if t is not None]
    for scope in scopes:
        for im in scope.find_all("img"):
            hint = " ".join([im.get("src") or "", im.get("alt") or "",
                             " ".join(im.get("class") or [])])
            if LOGO_ATTRS_RE.search(hint):
                src = im.get("src") or im.get("data-src") or ""
                if src and not src.startswith("data:"):
                    return urljoin(base_url, src), (im.get("alt") or ""), ""
    header = soup.find("header") or soup.find(class_=re.compile(r"site-header", re.I))
    if header:
        home = header.find("a", href=re.compile(r"^/$|^%s/?$" % re.escape(base_url)))
        if home:
            im = home.find("img")
            if im and im.get("src") and not im["src"].startswith("data:"):
                return urljoin(base_url, im["src"]), (im.get("alt") or ""), ""
        sv = header.find("svg")
        if sv:
            return "", "", str(sv)[:20000]
    return "", "", ""


def logo_stats(session, url, data):
    """Average opaque luminance + opacity ratio (drives the logo filter) and the
    dominant saturated colours (a brand-colour candidate that exists for every
    seller, however small — their logo)."""
    if not url:
        return
    if url.lower().split("?")[0].endswith(".svg"):
        try:
            svg = _get(session, url, 6, 300_000).decode("utf-8", "replace")
            for c in HEX_ANY_RE.findall(svg):
                hx = bb._rgb_to_hex(_norm_hex("#" + c))
                if bb._accent_eligible(hx):
                    data["accentCandidates"].extend([hx] * W_LOGO_COLOR)
        except requests.RequestException:
            pass
        return
    try:
        raw = _get(session, url, 8, MAX_LOGO_BYTES)
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((64, 64))
        pix = im.load()
        px = [pix[x, y] for y in range(im.height) for x in range(im.width)]
    except Exception:
        return
    total = len(px) or 1
    lum_sum, opaque, buckets = 0.0, 0, {}
    for r, g, b, a in px:
        if a <= 30:
            continue
        opaque += 1
        lum_sum += 0.299 * r + 0.587 * g + 0.114 * b
        mx, mn = max(r, g, b), min(r, g, b)
        sat = 0 if mx == 0 else (mx - mn) / mx
        lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
        if sat > 0.25 and 0.08 < lum < 0.92:
            key = (r // 32, g // 32, b // 32)
            cnt, rs, gs, bs_ = buckets.get(key, (0, 0, 0, 0))
            buckets[key] = (cnt + 1, rs + r, gs + g, bs_ + b)
    if opaque:
        data["logoLum"] = lum_sum / opaque
        data["logoOpaque"] = opaque / total
    if buckets and opaque:
        cnt, rs, gs, bs_ = max(buckets.values())
        if cnt / opaque > 0.08:            # a stray anti-aliased pixel is not a colour
            hx = "#%02x%02x%02x" % (rs // cnt, gs // cnt, bs_ // cnt)
            if bb._accent_eligible(hx):
                data["accentCandidates"].extend([hx] * W_LOGO_COLOR)


# ----------------------------------------------------------------------------
# Shopify adapter — real products for the long tail of small sellers
# ----------------------------------------------------------------------------
def is_shopify(html_text):
    return "cdn.shopify.com" in html_text or "Shopify.theme" in html_text \
        or "shopify-features" in html_text


def shopify_products(session, base_url):
    origin = "%s://%s" % (urlparse(base_url).scheme, urlparse(base_url).netloc)
    items = []
    # Some stores disable /products.json but leave the collection feed open.
    for path in ("/products.json?limit=12", "/collections/all/products.json?limit=12"):
        try:
            raw = _get(session, origin + path, 8, 2_000_000)
            items = json.loads(raw.decode("utf-8", "replace")).get("products") or []
        except (requests.RequestException, ValueError):
            continue
        if any((p.get("images") or []) for p in items):
            break
    out = []
    for p in items:
        img = (p.get("images") or [{}])[0]
        price = ""
        for var in (p.get("variants") or []):
            if var.get("price"):
                try:
                    price = "₹%s" % ("{:,.0f}".format(float(var["price"])))
                except ValueError:
                    price = ""
                break
        if img.get("src"):
            handle = (p.get("handle") or "").strip()
            out.append({"name": (p.get("title") or "").strip(),
                        "price": price,
                        "src": img["src"], "w": img.get("width") or 0,
                        "h": img.get("height") or 0,
                        "link": (origin + "/products/" + handle) if handle else ""})
    return out


# ----------------------------------------------------------------------------
# Tier 0 — assemble the brandboost data shape, statically
# ----------------------------------------------------------------------------
def extract_static(url, html_text=None):
    """Parse the homepage into the brandboost data shape. `html_text` lets a
    caller substitute a proxy-fetched document; sub-assets (CSS, images, logo,
    catalogue) are still fetched directly — CDNs rarely block those."""
    session = requests.Session()
    if html_text is None:
        html_text = fetch_page(session, url)
    soup = BeautifulSoup(html_text, "html.parser")

    data = {"headerBg": "", "logoSrc": "", "logoAlt": "", "logoSvg": "",
            "logoLum": None, "logoOpaque": None, "accentCandidates": [],
            "ctaBg": "", "ctaColor": "", "ctaRadius": "", "images": [],
            "bgImgs": [], "fonts": [], "annBg": "", "annColor": "", "annText": "",
            "ogSiteName": "", "social": {}, "bodyBg": "", "bodyColor": "",
            "bodyFont": "", "platform": "generic"}

    og = soup.find("meta", property="og:site_name")
    data["ogSiteName"] = (og.get("content") or "") if og else ""
    if not data["ogSiteName"] and soup.title:
        data["ogSiteName"] = soup.title.get_text().strip()

    theme = soup.find("meta", attrs={"name": "theme-color"})
    if theme and theme.get("content"):
        hx = bb._rgb_to_hex(_norm_hex(theme["content"]))
        if bb._accent_eligible(hx) and hx not in FRAMEWORK_HEXES:
            data["accentCandidates"].extend([hx] * W_THEME_META)
        if hx and not data["headerBg"]:
            data["headerBg"] = hx

    data["logoSrc"], data["logoAlt"], data["logoSvg"] = find_logo(soup, url)

    # JS-rendered sites often have no logo in markup, but og:image frequently
    # IS the logo (thesouledstore ships newlogosticky.png there). Only take it
    # when the URL says so — a product shot as header logo is worse than a
    # wordmark.
    if not data["logoSrc"] and not data["logoSvg"]:
        og_im = soup.find("meta", property="og:image")
        cand = (og_im.get("content") or "").strip() if og_im else ""
        if cand and re.search(r"logo", cand, re.I):
            data["logoSrc"] = urljoin(url, cand)

    # Last resort for the COLOUR pool only: the favicon carries the brand's
    # colours even when nothing else is extractable. Never displayed.
    icon_url = ""
    if not data["logoSrc"]:
        for link in soup.find_all("link", rel=True):
            rel = " ".join(link.get("rel") or []).lower()
            if "icon" in rel and link.get("href"):
                icon_url = urljoin(url, link["href"])
                if "apple-touch" in rel:      # biggest, keep looking otherwise
                    break

    css = fetch_css(session, soup, url)
    variables, rules, faces = parse_css(css)
    data["fonts"] = [{"family": f["family"], "weight": f["weight"]} for f in faces]
    for qs in GOOGLE_FONTS_RE.findall(html_text):
        for fam in re.findall(r"family=([^&:]+)", qs):
            name = fam.replace("+", " ").strip()
            if name and all(f["family"].lower() != name.lower() for f in data["fonts"]):
                data["fonts"].append({"family": name, "weight": "400"})
    mine_colors(variables, rules, data)

    for a in soup.find_all("a", href=True):
        h = a["href"].lower()
        for k in SOCIAL_KEYS:
            if k in h and k not in data["social"]:
                data["social"][k] = a["href"]

    ann = soup.find(class_=ANN_CLASS_RE) or soup.find(id=ANN_CLASS_RE)
    if ann:
        txt = ann.get_text(" ", strip=True).split("  ")[0][:120]
        data["annText"] = txt
        # colour only from an inline style — a guessed bg paints black-on-black
        style = ann.get("style") or ""
        m = re.search(r"background(?:-color)?\s*:\s*([^;]+)", style)
        if m:
            data["annBg"] = _norm_hex((COLOR_VAL_RE.findall(m.group(1)) or [""])[0])

    data["images"] = collect_images(session, soup, url)

    # JS-rendered storefronts keep their images in embedded JSON, not <img>.
    mined = mine_embedded_images(html_text, url, {i["src"] for i in data["images"]})
    mined_rows, need_probe = [], []
    for src in mined:
        w, h = _url_dims(src)
        row = {"src": src, "w": w, "h": h, "alt": ""}
        mined_rows.append(row)
        if not (w and h):
            need_probe.append(row)
    if need_probe:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for row, size in zip(need_probe[:MAX_MINED_PROBES],
                                 ex.map(lambda r: _probe_size(session, r["src"]),
                                        need_probe[:MAX_MINED_PROBES])):
                if size:
                    row["w"], row["h"] = size
    data["images"].extend(r for r in mined_rows if r["w"] and r["h"])

    kit_products = []
    if is_shopify(html_text):
        data["platform"] = "shopify"
        kit_products = shopify_products(session, url)
        seen = {i["src"] for i in data["images"]}
        for p in kit_products:
            if p["src"] not in seen and p["w"] and p["h"]:
                data["images"].append({"src": p["src"], "w": p["w"], "h": p["h"],
                                       "alt": p["name"]})

    logo_stats(session, data["logoSrc"] or icon_url, data)
    if not data["logoSrc"]:
        # The favicon probe was colour-mining only; its pixel luminance must
        # not drive a logo filter for a logo that does not exist.
        data["logoLum"] = data["logoOpaque"] = None
    session.close()
    return data, kit_products


# ----------------------------------------------------------------------------
# Tier 0b — proxy re-fetch. When the direct GET was blocked (bot wall, CF
# challenge) or returned a thin document, re-fetch through ScrapingDog and
# re-parse. Runs BEFORE any AI call so the model always sees the best digest.
# ----------------------------------------------------------------------------
SCRAPINGDOG_URL = "https://api.scrapingdog.com/scrape"
PROXY_TIMEOUT = 60
MIN_GOOD_IMAGES = 6


def fetch_via_proxy(url):
    """Homepage HTML via ScrapingDog, '' when unconfigured or unhelpful.
    SCRAPINGDOG_DYNAMIC=true turns on JS rendering (5x credits) for SPA-heavy
    seller bases — default off per cost policy."""
    key = os.environ.get("SCRAPINGDOG_API_KEY")
    if not key:
        return ""
    try:
        r = requests.get(SCRAPINGDOG_URL, timeout=PROXY_TIMEOUT, params={
            "api_key": key, "url": url,
            "dynamic": "true" if os.environ.get("SCRAPINGDOG_DYNAMIC") == "true"
                       else "false"})
    except requests.RequestException:
        return ""
    # A proxy error page is worse than nothing — only accept a real document.
    if r.status_code != 200 or len(r.text) < 2000 or "<html" not in r.text[:2000].lower():
        return ""
    return r.text


def _richness(data):
    """How much brand signal a parse produced. Used to pick between the direct
    and the proxy parse — never adopt the proxy result on faith."""
    return (len(data.get("images") or [])
            + 5 * bool(data.get("logoSrc") or data.get("logoSvg"))
            + 2 * len(data.get("fonts") or [])
            + 3 * bool(data.get("accentCandidates"))
            + 2 * bool(data.get("headerBg")))


def _extraction_poor(data):
    return (len(data.get("images") or []) < MIN_GOOD_IMAGES
            or not (data.get("logoSrc") or data.get("logoSvg"))
            or not data.get("accentCandidates"))


# ----------------------------------------------------------------------------
# Tier 2 — web-search escalation (colour/font/name only, tightly validated)
# ----------------------------------------------------------------------------
WEB_PROMPT = """\
You identify the visual identity of e-commerce brands from public knowledge.
Use web search to confirm. Return ONE JSON object, no prose, no fences:
{"brand_name": "string or null",
 "brand_color": "#rrggbb or null",
 "body_font": "font family name or null",
 "confidence": {"brand_name": 0.0, "brand_color": 0.0, "body_font": 0.0}}
Rules: the hex must be the brand's real primary colour (not white/black/grey).
If the brand is too small or obscure to verify, return nulls with low
confidence — a wrong answer is far worse than null. Never guess."""


def enrich_web(url, domain, brand_name, v):
    import brandai
    prompt = ("Brand website: %s\nDomain: %s\nName as scraped: %s\n"
              "What are this brand's primary brand colour (hex), brand/body font, "
              "and canonical short name?" % (url, domain, brand_name))
    try:
        ai, tokens = brandai.call_gateway(WEB_PROMPT, prompt, web_search=True)
    except brandai.AIGatewayError as exc:
        return [], {"ok": False, "error": str(exc)}
    applied = []
    if isinstance(ai, dict):
        conf = ai.get("confidence") or {}

        hx = ai.get("brand_color")
        if isinstance(hx, str) and isinstance(conf.get("brand_color"), (int, float)) \
                and conf["brand_color"] >= 0.6:
            hx = hx.strip().lower()
            if bb.HEX_RE.match(hx) and bb._usable_brand(hx):
                v["brand"], v["light"] = hx, bb._lighten(hx, 0.88)
                applied.append("brand_color")

        fam = ai.get("body_font")
        if isinstance(fam, str) and isinstance(conf.get("body_font"), (int, float)) \
                and conf["body_font"] >= 0.6:
            fam = bb.FONT_BAD_RE.sub("", fam.strip()).strip()
            if fam and not bb.ICON_FONT_RE.search(fam):
                v["body_font"] = "'%s',system-ui,-apple-system,sans-serif" % fam
                v["google_font"] = fam        # blade loads it from Google Fonts
                applied.append("body_font")

        name = ai.get("brand_name")
        if isinstance(name, str) and isinstance(conf.get("brand_name"), (int, float)) \
                and conf["brand_name"] >= 0.6:
            name = re.sub(r"\s+", " ", name).strip()[:30]
            if name:
                v["brand_name"] = name
                v["prefix"] = re.sub(r"[^A-Za-z]", "", name).upper()[:3] or "ORD"
                applied.append("brand_name")
    return applied, {"ok": True, "tokens": tokens}


# ----------------------------------------------------------------------------
# Confidence gate — decides whether the next (paid) tier runs at all
# ----------------------------------------------------------------------------
def weak_fields(v, data):
    weak = []
    if v["brand"] == "#111111":                       # derive()'s last resort
        weak.append("brand_color")
    if v["body_font"].startswith("system-ui"):        # no real family found
        weak.append("body_font")
    # Only weak when it was GUESSED from the domain — a name confirmed by
    # og:site_name or the logo alt matching the domain is simply correct.
    if not (data.get("ogSiteName") or "").strip() and not (data.get("logoAlt") or "").strip():
        weak.append("brand_name")
    return weak


# ----------------------------------------------------------------------------
# Kit assembly — the ONLY shape the Blade template knows about
# ----------------------------------------------------------------------------
def build_kit(v, data, picks, slug, url, meta):
    logo_src = bb._safe_url(data.get("logoSrc"))
    logo_svg = bb._sanitize_svg(data.get("logoSvg"))
    if logo_src:
        logo = {"mode": "img", "src": logo_src, "filter": v["logo_filter"], "svg": ""}
    elif logo_svg:
        fill = "#fff" if v["header_dark"] else "#111"
        logo = {"mode": "svg", "src": "",
                "filter": "",
                "svg": re.sub(r'fill="(?!none)[^"]*"', 'fill="%s"' % fill, logo_svg)}
    else:
        logo = {"mode": "wordmark", "src": "", "filter": "", "svg": ""}

    # Only products that actually have an image ship; a grid of grey boxes
    # with invented names reads as broken. Fewer than 2 → the section hides.
    names = list(v.get("prod_names") or []) + bb.PROD_NAMES
    prices = list(v.get("prod_prices") or []) + bb.PROD_PRICES
    links = list(v.get("prod_links") or [])
    withimg = [(bb._safe_url(p), names[i], prices[i],
                bb._safe_url(links[i]) if i < len(links) else "")
               for i, p in enumerate(picks["prods"][:4]) if bb._safe_url(p)]
    count = 4 if len(withimg) >= 4 else 2 if len(withimg) >= 2 else 0
    products = [{"image": im, "name": n, "price": pr, "link": lk or ""}
                for im, n, pr, lk in withimg[:count]]

    m = re.search(r"(\d{1,2}\s*%\s*OFF)", v["ann_copy"], re.I)
    ad_l1, ad_l2 = ("FLAT", m.group(1).upper()) if m else ("NEW", "ARRIVALS")

    return {
        "brand_name": v["brand_name"], "domain": v["domain"], "url": url,
        "slug": slug, "prefix": v["prefix"],
        "colors": {"brand": v["brand"], "light": v["light"],
                   "header_bg": v["header_bg"], "header_icon": v["header_icon"],
                   "header_border": v["header_border"], "header_dark": v["header_dark"],
                   "body_bg": v["body_bg"], "body_text": v["body_text"],
                   "ann_bg": v["ann_bg"], "ann_text": v["ann_text_col"]},
        "typography": {"body_font": v["body_font"],
                       "font_face_css": v["font_face_css"],
                       "google_font": v.get("google_font") or ""},
        "cta_radius": v["cta_radius"],
        "logo": logo,
        "announcement": v["ann_copy"],
        "hero": {"image": bb._safe_url(picks["hero"]) or "",
                 "l1": v.get("hero_l1") or "NEW SEASON",
                 "l2": v.get("hero_l2") or "NEW DROPS",
                 "sub": v.get("hero_sub") or "Fresh styles, straight from %s" % v["brand_name"]},
        "ad": {"eyebrow": v.get("ad_eyebrow") or "Limited Offer",
               "l1": v.get("ad_l1") or ad_l1, "l2": v.get("ad_l2") or ad_l2,
               "sub": v.get("ad_sub") or "On all orders. Use code %s25" % v["prefix"]},
        "products": products,
        "showcase_image": bb._safe_url(picks["showcase"]) or "",
        "instagram": {"handle": slug,
                      "images": [bb._safe_url(u) for u in picks["ig"] if bb._safe_url(u)][:5]},
        "social": data.get("social") or {},
        "meta": meta,
    }


# ----------------------------------------------------------------------------
# Cost accounting — estimates, priced per token / per credit so the frontend
# can show what each run actually spent. AI rates default to GPT-5-class list
# prices. ScrapingDog pricing pulled July 2026: 1 credit per plain scrape,
# 5 with JS rendering, ~15 per dedicated social-API request; Lite plan is
# $40 / 200k credits → $0.0002 per credit. All env-tunable.
# ----------------------------------------------------------------------------
RATE_USD_PER_M_INPUT = float(os.environ.get("SR_AI_USD_PER_M_INPUT", "1.25"))
RATE_USD_PER_M_OUTPUT = float(os.environ.get("SR_AI_USD_PER_M_OUTPUT", "10.0"))
WEB_TOOL_USD_PER_CALL = float(os.environ.get("SR_AI_WEB_TOOL_USD", "0.01"))
USD_PER_CREDIT = float(os.environ.get("SCRAPINGDOG_USD_PER_CREDIT", "0.0002"))


def _proxy_credits():
    return 5 if os.environ.get("SCRAPINGDOG_DYNAMIC") == "true" else 1


def _tier_usd(tokens, web_tool=False):
    if not isinstance(tokens, dict):
        tokens = {}
    usd = ((tokens.get("input") or 0) / 1e6 * RATE_USD_PER_M_INPUT
           + (tokens.get("output") or 0) / 1e6 * RATE_USD_PER_M_OUTPUT)
    if web_tool:
        usd += WEB_TOOL_USD_PER_CALL
    return usd


def _cost_summary(meta):
    ran = set(meta["tiers"])
    cost = {
        "static": 0.0,
        "proxy_fetch": round(_proxy_credits() * USD_PER_CREDIT, 4)
                       if "proxy_fetch" in ran else None,
        "shopify_products": 0.0 if "shopify_products" in ran else None,
        "ai_digest": _tier_usd(meta.get("tokens_digest"))
                     if "ai_digest" in ran else None,
        "ai_web_search": _tier_usd(meta.get("tokens_web"), web_tool=True)
                         if "ai_web_search" in ran else None,
    }
    total = sum(v for v in cost.values() if v)
    return {"per_tier_usd": {k: (round(v, 4) if v is not None else None)
                             for k, v in cost.items()},
            "total_usd": round(total, 4),
            "rates": {"input_per_m": RATE_USD_PER_M_INPUT,
                      "output_per_m": RATE_USD_PER_M_OUTPUT,
                      "web_tool_per_call": WEB_TOOL_USD_PER_CALL,
                      "usd_per_credit": USD_PER_CREDIT,
                      "proxy_credits": _proxy_credits()}}


# ----------------------------------------------------------------------------
# The loop
# ----------------------------------------------------------------------------
def run(url, slug, use_ai=True):
    t0 = time.time()
    meta = {"platform": "generic", "tiers": ["static"], "ai_applied": [],
            "weak_after_static": [], "weak_final": []}

    # Tier 0 — direct fetch. A blocked/broken direct fetch is not fatal yet;
    # the proxy below gets a chance first.
    data = kit_products = None
    direct_error = None
    try:
        data, kit_products = extract_static(url)
    except requests.RequestException as exc:
        direct_error = exc

    # Tier 0b — proxy re-fetch, only when direct failed or parsed thin.
    if data is None or _extraction_poor(data):
        proxy_html = fetch_via_proxy(url)
        if proxy_html:
            meta["tiers"].append("proxy_fetch")
            try:
                pdata, pprods = extract_static(url, html_text=proxy_html)
            except requests.RequestException:
                pdata = None
            if pdata is not None and (data is None
                                      or _richness(pdata) > _richness(data)):
                data, kit_products = pdata, pprods
                meta["proxy_adopted"] = True
            else:
                meta["proxy_adopted"] = False
    if data is None:
        raise direct_error

    meta["platform"] = data["platform"]

    v = bb.derive(data, url)
    picks = bb.select_images(data)
    domain = v["domain"]
    meta["weak_after_static"] = weak_fields(v, data)

    # Tier 1 — semantics from the digest, no web search.
    if use_ai:
        import brandai
        v, picks, report = brandai.enrich(data, url, v, picks, web_search=False)
        meta["tiers"].append("ai_digest")
        if report["ok"]:
            meta["ai_applied"] = report["applied"]
            meta["ai_notes"] = report.get("notes") or ""
            meta["tokens_digest"] = report.get("tokens") or {}
            # The brand's voice, as read from the site — downstream copy
            # passes (curation) must write in it, not in generic ad-speak.
            meta["tone"] = report.get("tone") or ""
        else:
            meta["ai_error"] = report["error"]

    # Real store data beats anything invented: Shopify names/prices/images win.
    if kit_products:
        usable = [p for p in kit_products if bb._safe_url(p["src"])][:4]
        if len(usable) >= 2:
            n = 4 if len(usable) >= 4 else 2
            picks["prods"] = [bb._bump_quality(p["src"], 400) for p in usable[:n]]
            v["prod_names"] = [p["name"][:22] for p in usable[:n]]
            v["prod_prices"] = [p["price"] or "₹999" for p in usable[:n]]
            v["prod_links"] = [p.get("link") or "" for p in usable[:n]]
            meta["ai_applied"] = [a for a in meta["ai_applied"] if a != "product_images"]
            meta["tiers"].append("shopify_products")

    # Tier 2 — web search, only for what is still weak, only if AI is on.
    weak = weak_fields(v, data)
    if use_ai and set(weak) & {"brand_color", "body_font", "brand_name"}:
        applied, wreport = enrich_web(url, domain, v["brand_name"], v)
        meta["tiers"].append("ai_web_search")
        meta["web_applied"] = applied
        if wreport["ok"]:
            meta["tokens_web"] = wreport.get("tokens") or {}
        else:
            meta["web_error"] = wreport["error"]

    meta["weak_final"] = weak_fields(v, data)
    meta["elapsed_s"] = round(time.time() - t0, 1)
    meta["images_found"] = len(data.get("images") or [])
    meta["cost"] = _cost_summary(meta)
    return build_kit(v, data, picks, slug, url, meta), v, data, picks


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        sys.exit(1)
    url = args[0]
    slug = args[1] if len(args) > 1 else \
        urlparse(url).netloc.replace("www.", "").split(".")[0]

    print("→ static extraction (no browser): %s" % url)
    kit, v, data, picks = run(url, slug, use_ai="--no-ai" not in flags)

    out = "%s_brandkit.json" % slug
    with open(out, "w") as f:
        json.dump(kit, f, indent=2, ensure_ascii=False)

    m = kit["meta"]
    print("\n--- brand kit ---")
    print("  platform   : %s" % m["platform"])
    print("  tiers run  : %s" % " → ".join(m["tiers"]))
    print("  brand_name : %s" % kit["brand_name"])
    print("  brand      : %s" % kit["colors"]["brand"])
    print("  header_bg  : %s" % kit["colors"]["header_bg"])
    print("  font       : %s" % kit["typography"]["body_font"])
    print("  logo       : %s %s" % (kit["logo"]["mode"], kit["logo"]["src"][:70]))
    print("  products   : %d | hero=%s showcase=%s ig=%d"
          % (len(kit["products"]), bool(kit["hero"]["image"]),
             bool(kit["showcase_image"]), len(kit["instagram"]["images"])))
    print("  ai applied : %s" % (", ".join(m.get("ai_applied") or []) or "—"))
    print("  web applied: %s" % (", ".join(m.get("web_applied") or []) or "—"))
    print("  weak fields: %s" % (", ".join(m["weak_final"]) or "none"))
    print("  elapsed    : %ss" % m["elapsed_s"])
    print("✓ wrote %s" % out)

    if "--html" in flags:
        html_out = bb.build_tracking_html(v, data, picks, slug)
        with open("%s_tracking_kit.html" % slug, "w") as f:
            f.write(html_out)
        print("✓ wrote %s_tracking_kit.html (local preview)" % slug)


if __name__ == "__main__":
    main()
