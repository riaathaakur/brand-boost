#!/usr/bin/env python3
"""
Brand Boost — Shiprocket tracking page generator.

Usage:
    export SR_AI_GATEWAY_KEY=...
    python3 brandboost.py <url> [slug] [--no-ai]
    python3 brandboost.py https://www.zudio.com/ zudio

Extracts brand assets with Playwright, derives a heuristic baseline, then asks
the AI gateway (see brandai.py) to resolve the parts that need semantics rather
than measurement. The page HTML is always assembled here in Python — the model
never writes markup, only structured JSON that is re-validated before use.

`--no-ai` skips the gateway and builds from heuristics alone.
"""
import html
import json
import os
import re
import sys
from collections import defaultdict
from string import Template
from urllib.parse import urlparse, quote

# ----------------------------------------------------------------------------
# Step 1 — Playwright extraction.  EXTRACT_JS MUST be a raw string: JS regex
# patterns (\d, \s, \() are silently mangled into Python escapes otherwise.
# ----------------------------------------------------------------------------
EXTRACT_JS = r"""() => {
  const out = {};
  const gcs = el => getComputedStyle(el);
  const clear = c => !c || c === 'transparent' || c === 'rgba(0, 0, 0, 0)';
  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return ''; } };

  /* Hidden elements must never define the brand: cookie banners and cart
     popups ship stock-theme colours that have nothing to do with the site. */
  const isVisible = el => {
    try {
      if (typeof el.checkVisibility === 'function')
        return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
    } catch (e) { /* fall through */ }
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const s = gcs(el);
    return s.display !== 'none' && s.visibility !== 'hidden' &&
           parseFloat(s.opacity || '1') > 0.01;
  };

  const CONSENT_RE = /cookie|consent|gdpr|privacy|onetrust|gotrust|cmp[-_]/i;
  const inConsent = el => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cls = n.className && n.className.toString ? n.className.toString() : '';
      if (CONSENT_RE.test((n.id || '') + ' ' + cls)) return true;
    }
    return false;
  };
  const usable = el => isVisible(el) && !inConsent(el);

  const fromSrcset = ss => {
    if (!ss) return '';
    let best = '', bestScore = -1;
    ss.split(',').forEach(part => {
      const bits = part.trim().split(/\s+/);
      const u = bits[0];
      if (!u || u.indexOf('data:') === 0) return;
      const d = bits[1] || '';
      let score = 1;
      if (/w$/.test(d)) score = parseFloat(d);
      else if (/x$/.test(d)) score = parseFloat(d) * 1000;
      if (score > bestScore) { bestScore = score; best = u; }
    });
    return best;
  };

  /* ---- header background: walk UP until a non-transparent bg is found ---- */
  const HEADER_SELS = ['header', '#header', '.header', '.site-header',
                       '[class*="header"]', '.navbar', 'nav'];
  let headerEl = null;
  for (const s of HEADER_SELS) {
    const e = document.querySelector(s);
    if (e) { headerEl = e; break; }
  }
  let headerBg = '';
  if (headerEl) {
    let n = headerEl;
    while (n && n !== document.documentElement) {
      const bg = gcs(n).backgroundColor;
      if (!clear(bg)) { headerBg = bg; break; }
      n = n.parentElement;
    }
  }
  out.headerBg = headerBg;

  /* ---- logo ---- */
  const LOGO_SELS = [
    'header img[src*="logo"]', 'header img[alt*="logo" i]', 'header img[class*="logo"]',
    'header .logo img', 'header .brand img', 'header a[href="/"] img',
    '.site-header img[src*="logo"]', '.site-header .logo img',
    'nav img[src*="logo"]', 'nav .logo img',
    'img[src*="logo"][src*="cdn"]', 'img[alt*="logo" i]'
  ];
  /* Average opaque luminance of the logo image. A near-white logo (built for a
     dark header) is invisible on a light header, and no filename hint is
     reliable — measure the actual pixels. null when unmeasurable (e.g. CORS). */
  const logoStats = im => {
    try {
      if (!im.complete || !im.naturalWidth) return null;
      const c = document.createElement('canvas');
      c.width = Math.min(im.naturalWidth, 64);
      c.height = Math.min(im.naturalHeight, 64);
      const ctx = c.getContext('2d');
      ctx.drawImage(im, 0, 0, c.width, c.height);
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      const total = d.length / 4;
      let tot = 0, n = 0;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] > 30) { tot += 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; n++; }
      }
      return n ? { lum: tot / n, opaque: n / total } : null;
    } catch (e) { return null; }
  };

  out.logoSrc = ''; out.logoAlt = ''; out.logoSvg = '';
  out.logoLum = null; out.logoOpaque = null;
  for (const s of LOGO_SELS) {
    const im = document.querySelector(s);
    if (im && im.tagName === 'IMG') {
      const cand = fromSrcset(im.srcset) || im.currentSrc || im.src || '';
      if (cand && cand.indexOf('data:') !== 0) {
        out.logoSrc = abs(cand);
        out.logoAlt = im.alt || '';
        const st = logoStats(im);
        if (st) { out.logoLum = st.lum; out.logoOpaque = st.opaque; }
        break;
      }
    }
  }
  if (!out.logoSrc) {
    const sv = document.querySelector('header svg, .site-header svg, nav svg');
    if (sv) out.logoSvg = sv.outerHTML.slice(0, 20000);
  }

  /* ---- accent candidates ---- */
  const sat = (r, g, b) => { const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
                             return mx === 0 ? 0 : (mx - mn) / mx; };
  const lum = (r, g, b) => (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  const acc = [];
  document.querySelectorAll('a,button,.btn,[class*="btn"],[class*="cta"],[class*="add-to-cart"]')
    .forEach(el => {
      if (!usable(el)) return;
      const s = gcs(el);
      [s.color, s.backgroundColor, s.borderColor].forEach(v => {
        if (clear(v)) return;
        const m = v.match(/(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
        if (!m) return;
        const r = +m[1], g = +m[2], b = +m[3];
        const L = lum(r, g, b);
        if (sat(r, g, b) > 0.25 && L > 0.08 && L < 0.92) acc.push(v);
      });
    });
  out.accentCandidates = acc.slice(0, 3000);

  /* ---- CTA button ---- */
  const CTA_SELS = ['.add-to-cart', '[name="add"]', '.btn-primary', '.product-form__submit',
                    'button[type="submit"]', '.shopify-payment-button__button', '.primary-btn'];
  outer: for (const s of CTA_SELS) {
    for (const e of document.querySelectorAll(s)) {
      if (!usable(e)) continue;
      const st = gcs(e);
      out.ctaBg = st.backgroundColor; out.ctaColor = st.color; out.ctaRadius = st.borderRadius;
      break outer;
    }
  }

  /* ---- images (srcset-aware, lazy-load-aware) ---- */
  const imgs = [], seen = new Set();
  document.querySelectorAll('img').forEach(im => {
    const ds = im.dataset || {};
    let src = fromSrcset(im.srcset) || fromSrcset(ds.srcset) || im.currentSrc || im.src ||
              ds.src || ds.lazySrc || im.getAttribute('data-original') || '';
    if (!src || src.indexOf('data:') === 0) return;
    src = abs(src);
    if (!src || seen.has(src)) return;
    seen.add(src);
    let w = im.naturalWidth || 0, h = im.naturalHeight || 0;
    if (!w) { const m = src.match(/[?&](?:w|width)=(\d+)/); if (m) w = parseInt(m[1], 10); }
    if (!w) w = im.offsetWidth || 0;
    if (!h) h = im.offsetHeight || 0;
    imgs.push({ src: src, w: w, h: h, alt: im.alt || '' });
  });
  out.images = imgs;

  /* ---- css background images ---- */
  const bgImgs = [];
  document.querySelectorAll('[class*="hero"],[class*="banner"],[class*="slider"],[class*="slideshow"],[class*="carousel"],[class*="swiper"],section')
    .forEach(el => {
      const bi = gcs(el).backgroundImage;
      if (!bi || bi === 'none') return;
      const m = bi.match(/url\(["']?(.*?)["']?\)/);
      if (m && m[1] && m[1].indexOf('data:') !== 0) { const u = abs(m[1]); if (u) bgImgs.push(u); }
    });
  out.bgImgs = bgImgs;

  /* ---- fonts ---- */
  const fams = [], fseen = new Set();
  Array.from(document.styleSheets).forEach(sheet => {
    let rules;
    try { rules = sheet.cssRules; } catch (e) { return; }
    if (!rules) return;
    Array.from(rules).forEach(r => {
      if (r.type !== 5) return;                       // CSSFontFaceRule
      const st = r.style;
      const fam = (st.getPropertyValue('font-family') || '').replace(/["']/g, '').trim();
      const src = st.getPropertyValue('src') || '';
      if (!fam || src.indexOf('data:') === 0) return;
      const key = fam.toLowerCase();
      if (fseen.has(key)) return;
      fseen.add(key);
      fams.push({ family: fam, weight: (st.getPropertyValue('font-weight') || '400').trim() });
    });
  });
  out.fonts = fams;

  /* ---- announcement bar ---- */
  /* A real announcement bar is a visible, wide, short strip at the top of the
     document. Without those checks '[class*="notice"]' happily matches a
     hidden cookie-policy link. */
  const ANN = ['[class*="announcement"]', '[class*="promo-bar"]', '[class*="top-bar"]',
               '[class*="topbar"]', '[id*="announcement"]', '[class*="notice"]'];
  annLoop: for (const s of ANN) {
    for (const e of document.querySelectorAll(s)) {
      if (!usable(e)) continue;
      const r = e.getBoundingClientRect();
      if (r.width < window.innerWidth * 0.6) continue;
      if (r.height > 120) continue;
      if (r.top + window.scrollY > 300) continue;
      const txt = (e.innerText || '').trim().split('\n')[0].slice(0, 120);
      if (!txt) continue;
      const st = gcs(e);
      out.annBg = st.backgroundColor;
      out.annColor = st.color;
      out.annText = txt;
      break annLoop;
    }
  }

  /* ---- brand name + social + body ---- */
  const og = document.querySelector('meta[property="og:site_name"]');
  out.ogSiteName = og ? (og.content || '') : '';
  const social = {};
  document.querySelectorAll('a[href]').forEach(a => {
    const h = a.href.toLowerCase();
    ['instagram', 'facebook', 'youtube', 'twitter', 'x.com'].forEach(k => {
      if (!social[k] && h.indexOf(k) !== -1) social[k] = a.href;
    });
  });
  out.social = social;
  const bs = gcs(document.body);
  out.bodyBg = bs.backgroundColor;
  out.bodyColor = bs.color;
  out.bodyFont = bs.fontFamily || '';
  return out;
}"""


def _scroll_through(page):
    """Scroll in 600px steps to trigger lazy-loaded images. Re-read the height
    each step — lazy content grows the page as we go."""
    y = 0
    for _ in range(20):
        height = min(page.evaluate("document.body.scrollHeight"), 12000)
        if y >= height:
            break
        page.mouse.wheel(0, 600)
        page.wait_for_timeout(180)
        y += 600
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(600)


def extract(url):
    # Imported here, not at module top: production paths use brandkit.py's
    # browserless extractor and must not require a Playwright install just to
    # import derive()/select_images() from this module.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = ctx.new_page()
        # "load" NOT "networkidle" — networkidle times out on heavy JS sites.
        page.goto(url, wait_until="load", timeout=60_000)
        page.wait_for_timeout(2000)
        _scroll_through(page)
        data = page.evaluate(EXTRACT_JS)
        browser.close()
    return data


# ----------------------------------------------------------------------------
# Step 2 — colour derivation
# ----------------------------------------------------------------------------
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# Matches both legacy `rgb(1, 2, 3)` / `rgba(1, 2, 3, 0)` and modern `rgb(1 2 3 / 50%)`.
RGB_RE = re.compile(
    r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)", re.I
)
RADIUS_RE = re.compile(r"^\d+(?:\.\d+)?(?:px|rem|em|%)$")
FONT_BAD_RE = re.compile(r"[^A-Za-z0-9 _-]")
MIN_ALPHA = 0.5


def _rgb_to_hex(rgb_str):
    """Transparent colours are ABSENT, not black. `rgba(0,0,0,0)` must not
    become #000000 — that silently paints black-on-black bars."""
    if not rgb_str:
        return ""
    s = str(rgb_str).strip()
    if HEX_RE.match(s):
        return s.lower()
    m = RGB_RE.search(s)
    if not m:
        return ""
    alpha = m.group(4)
    if alpha is not None:
        a = float(alpha[:-1]) / 100 if alpha.endswith("%") else float(alpha)
        if a < MIN_ALPHA:
            return ""
    r, g, b = (min(255, max(0, int(round(float(m.group(i)))))) for i in (1, 2, 3))
    return "#%02x%02x%02x" % (r, g, b)


def _safe_hex(value, default):
    return value if value and HEX_RE.match(value) else default


def _e(value):
    """Escape untrusted remote text for HTML text nodes and quoted attributes."""
    return html.escape(str(value or ""), quote=True)


def _safe_url(url):
    return url if url and re.match(r"^https?://", url.strip(), re.I) else ""


SVG_BLOCK_RE = re.compile(
    r"<\s*(script|foreignObject|iframe|object|embed|style|link)\b[^>]*>.*?"
    r"<\s*/\s*\1\s*>", re.I | re.S
)
SVG_VOID_RE = re.compile(r"<\s*(script|foreignObject|iframe|object|embed|link)\b[^>]*/?>", re.I)
SVG_ON_ATTR_RE = re.compile(r"""\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.I)
SVG_JS_URL_RE = re.compile(r"""(?:xlink:)?href\s*=\s*("|')?\s*javascript:[^"'>]*("|')?""", re.I)
SVG_DANGER_RE = re.compile(r"<\s*script|\son[a-z]+\s*=|javascript:", re.I)


def _sanitize_svg(svg):
    """Strip active content from a remote logo SVG, then verify. If anything
    dangerous survives, drop the SVG entirely rather than ship it."""
    if not svg:
        return ""
    cleaned = SVG_BLOCK_RE.sub("", svg)
    cleaned = SVG_VOID_RE.sub("", cleaned)
    cleaned = SVG_ON_ATTR_RE.sub("", cleaned)
    cleaned = SVG_JS_URL_RE.sub("", cleaned)
    return "" if SVG_DANGER_RE.search(cleaned) else cleaned


def _rgb_triplet(hex_color):
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(hex_color):
    r, g, b = _rgb_triplet(hex_color)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _lighten(hex_color, factor=0.88):
    r, g, b = _rgb_triplet(hex_color)
    r = int(r + (255 - r) * factor)
    g = int(g + (255 - g) * factor)
    b = int(b + (255 - b) * factor)
    return "#%02x%02x%02x" % (r, g, b)


def _saturation(hex_color):
    r, g, b = _rgb_triplet(hex_color)
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


def _usable_brand(hx):
    """A brand colour must be visible as a footer/badge fill against white text.
    Greys and near-white/near-black are never valid brand colours."""
    if not hx or len(hx) != 7:
        return False
    return _saturation(hx) > 0.15 and 0.08 < _luminance(hx) < 0.80


def _accent_eligible(hx):
    """Can this colour ever be elected the brand accent? Never a grey, and never
    so dark it is indistinguishable from black — `#0000ee` (the browser's
    default unstyled-link blue, lum 0.11) recurs on every un-themed link and
    would otherwise win on count alone.

    Single source of truth: `_pick_accent` and `brandai` must agree on this, or
    the AI layer can hand back a colour this one already rejected."""
    if not hx or not HEX_RE.match(hx):
        return False
    return _saturation(hx) > 0.15 and 0.12 < _luminance(hx) < 0.88


def _pick_accent(candidates):
    """A brand colour is the one the site REPEATS, not the most saturated pixel
    on the page — max-saturation alone elects a lone outlier link. Rank by how
    often a colour recurs, break ties on saturation."""
    counts = defaultdict(int)
    for c in candidates or []:
        hx = _rgb_to_hex(c)
        if _accent_eligible(hx):
            counts[hx] += 1
    if not counts:
        return ""
    return max(counts, key=lambda hx: (counts[hx], _saturation(hx)))


def _bump_quality(url, width=800):
    """Ask the CDN for exactly the width we render at. Never over-fetch."""
    if not url:
        return url
    # \d* not \d+: an EMPTY value ("?width=&quality=50", seen on mamaearth's
    # CDN) must be filled too, or the CDN gets a malformed request.
    if re.search(r"[?&]width=", url):
        return re.sub(r"([?&]width=)\d*", r"\g<1>%d" % width, url)
    if re.search(r"[?&]w=", url):
        return re.sub(r"([?&]w=)\d*", r"\g<1>%d" % width, url)
    if "cdn.shopify.com" in url:
        return url + ("&" if "?" in url else "?") + "width=%d" % width
    return url


GENERIC_ALTS = {"logo", "image", "img", "header", "brand", "home", "site"}
NAV_WORDS = r"\b(Previous|Next|Prev)\b|[›‹«»]"
# Copy that proves we grabbed chrome (cookie link, slider control), not a promo.
ANN_REJECT_RE = re.compile(
    r"cookie|privacy|consent|gdpr|policy|terms|sign in|log in|"
    r"slideshow|slide\s*\d|previous slide|next slide|pause|play video",
    re.I,
)
DEFAULT_ANN = ("Free delivery on orders above ₹499", "#111111", "#ffffff")

# Icon/webfonts render text as glyphs or boxes — they must never become the
# body font, even when the site declares them first in the cascade.
ICON_FONT_RE = re.compile(
    r"font\s*awesome|fontawesome|icomoon|ionicons|glyphicon|material icons|"
    r"swiper|boxicons|judgeme|feather|dashicons|elegant|themify|linearicons|"
    r"\bicons?\b|\bstar\b",
    re.I,
)
# Split a page title into brand vs tagline, but only on real separators —
# a bare in-word hyphen ("Fire-Boltt") is part of the name, not a delimiter.
TITLE_SPLIT_RE = re.compile(r"\s*\|\s*|\s+[-–—:]\s+|\s*[–—]\s*")
_NAME_NORM_RE = re.compile(r"[^a-z0-9]")


def _name_from_title(title, domain):
    """The word-span of the title that spells the domain — 'Online Shopping
    for Men & Women at The Souled Store' + thesouledstore.com → 'The Souled
    Store'. Marketing titles bury the brand anywhere; the domain always
    names it. '' when no span matches."""
    base = _NAME_NORM_RE.sub("", domain.split(".")[0].lower())
    if not title or len(base) < 4:
        return ""
    words = re.split(r"\s+", title.strip())
    for i in range(len(words)):
        cat = ""
        for j in range(i, min(i + 6, len(words))):
            cat += _NAME_NORM_RE.sub("", words[j].lower())
            if cat == base:
                return " ".join(words[i:j + 1]).strip(" -–—|:,")
            if len(cat) >= len(base):
                break
    return ""
# Filenames/alts that mark a logo built for a dark background.
LIGHT_LOGO_RE = re.compile(r"white|light|inverse|inverted|reverse", re.I)
DARK_LOGO_RE = re.compile(r"black|dark|color|colour", re.I)

_GENERIC_FAMILIES = {"sans-serif", "serif", "monospace", "system-ui", "cursive",
                     "fantasy", "-apple-system", "blinkmacsystemfont", "ui-sans-serif",
                     "ui-serif", "inherit", "initial", "unset"}


def _first_font_family(computed):
    """First real family from a CSS font-family list ('"Open Sans", sans-serif'
    → 'Open Sans'). Skips generics and icon fonts; returns '' if none qualify."""
    for part in (computed or "").split(","):
        fam = FONT_BAD_RE.sub("", part.replace('"', "").replace("'", "").strip()).strip()
        if not fam or fam.lower() in _GENERIC_FAMILIES or ICON_FONT_RE.search(fam):
            continue
        return fam
    return ""


def _contrasts(fg, bg):
    return abs(_luminance(fg) - _luminance(bg)) > 0.35


def _readable_on(bg):
    return "#ffffff" if _luminance(bg) < 0.5 else "#111111"


def derive(data, url):
    d = urlparse(url).netloc.replace("www.", "")
    v = {}

    header_bg = _safe_hex(_rgb_to_hex(data.get("headerBg", "")), "#ffffff")
    header_dark = _luminance(header_bg) < 0.5

    brand = _pick_accent(data.get("accentCandidates"))
    if not _usable_brand(brand):
        brand = _rgb_to_hex(data.get("ctaBg", ""))          # 2nd choice: CTA fill
    if not _usable_brand(brand) and header_dark:
        brand = header_bg                                   # 3rd: dark header
    if not HEX_RE.match(brand or "") or _luminance(brand) > 0.80:
        brand = "#111111"                                   # last resort: near-black

    # cta radius
    raw_r = ((data.get("ctaRadius") or "0px").split() or ["0px"])[0]
    if "%" in raw_r:
        cta_radius = "999px"
    elif not RADIUS_RE.match(raw_r):
        cta_radius = "0px"
    else:
        try:
            cta_radius = "999px" if float(re.sub(r"[^\d.]", "", raw_r) or 0) >= 50 else raw_r
        except ValueError:
            cta_radius = "0px"

    # body bg — reject near-black (dark hero leaking into body)
    raw_bg = _safe_hex(_rgb_to_hex(data.get("bodyBg", "")), "#ffffff")
    body_bg = raw_bg if _luminance(raw_bg) > 0.15 else "#ffffff"
    body_text = _safe_hex(_rgb_to_hex(data.get("bodyColor", "")), "#111111")
    if _luminance(body_text) > 0.7:
        body_text = "#111111"

    # brand name — a title span that spells the domain beats any splitting
    # ("Online Shopping … at The Souled Store" → "The Souled Store"); else
    # strip a trailing tagline but keep in-word hyphens.
    raw_og = (data.get("ogSiteName") or "").strip()
    og_site_name = _name_from_title(raw_og, d) or \
        (TITLE_SPLIT_RE.split(raw_og)[0].strip() if raw_og else "")
    logo_alt = (data.get("logoAlt") or "").strip()
    if og_site_name and len(og_site_name) <= 30:
        brand_name = og_site_name
    elif logo_alt and len(logo_alt) <= 25 and logo_alt.lower() not in GENERIC_ALTS:
        brand_name = logo_alt
    else:
        brand_name = d.split(".")[0].title()

    # announcement bar — a transparent bar has no colour, so keep the defaults
    # together. Mixing an extracted bg with a default fg paints black on black.
    ann_bg = _rgb_to_hex(data.get("annBg", ""))
    ann_text_col = _rgb_to_hex(data.get("annColor", ""))
    ann_copy = re.sub(NAV_WORDS, "", data.get("annText") or "", flags=re.I).strip(" ·|-\n\t")
    if len(ann_copy) < 5 or ANN_REJECT_RE.search(ann_copy) or not HEX_RE.match(ann_bg or ""):
        ann_copy, ann_bg, ann_text_col = DEFAULT_ANN
    elif not HEX_RE.match(ann_text_col or "") or not _contrasts(ann_text_col, ann_bg):
        ann_text_col = _readable_on(ann_bg)

    # fonts — family names land inside a CSS literal, so allow only safe chars.
    # Skip icon fonts entirely: they break both the @font-face list and, if
    # first, the body text (which would render as glyphs/boxes).
    fams, faces = [], []
    for f in (data.get("fonts") or []):
        raw_fam = (f.get("family") or "").strip()
        if ICON_FONT_RE.search(raw_fam):
            continue
        fam = FONT_BAD_RE.sub("", raw_fam).strip()
        if not fam:
            continue
        weight = re.sub(r"[^0-9]", "", str(f.get("weight") or "400")) or "400"
        fams.append(fam)
        faces.append("@font-face{font-family:'%s';src:local('%s');font-weight:%s}"
                     % (fam, fam, weight))
        if len(faces) >= 6:
            break
    font_face_css = "\n".join(faces)

    # The body font is whatever the site COMPUTES on <body> — not the first
    # @font-face declared (that order is arbitrary and often a display/icon
    # font). Fall back to the first real @font-face family, then the system stack.
    body_family = _first_font_family(data.get("bodyFont")) or (fams[0] if fams else "")
    body_font = ("'%s',system-ui,-apple-system,sans-serif" % body_family) if body_family \
        else "system-ui,-apple-system,'Segoe UI',sans-serif"

    # A near-white wordmark built for a dark bg vanishes on a light header;
    # brightness(0) repaints it black so it stays visible. Only safe for a
    # mostly-transparent glyph/wordmark — a near-solid block (an app-icon square)
    # would just become a black blob, so leave those untouched.
    logo_lum = data.get("logoLum")
    logo_opaque = data.get("logoOpaque")
    if isinstance(logo_lum, (int, float)):
        mostly_solid = isinstance(logo_opaque, (int, float)) and logo_opaque > 0.85
        light_logo = logo_lum > 200 and not mostly_solid
    else:
        logo_hint = "%s %s" % ((data.get("logoSrc") or ""), logo_alt)
        light_logo = bool(LIGHT_LOGO_RE.search(logo_hint)) and not DARK_LOGO_RE.search(logo_hint)
    if header_dark:
        logo_filter = "filter:brightness(0) invert(1);"
    elif light_logo:
        logo_filter = "filter:brightness(0);"
    else:
        logo_filter = ""

    v.update(
        domain=d,
        url=url,
        brand=brand,
        light=_lighten(brand, 0.88),
        header_bg=header_bg,
        header_icon="#fff" if header_dark else "#111",
        header_border="none" if header_dark else "1px solid #e8e8e8",
        logo_filter=logo_filter,
        header_dark=header_dark,
        cta_radius=cta_radius,
        body_bg=body_bg,
        body_text=body_text,
        body_font=body_font,
        font_face_css=font_face_css,
        brand_name=brand_name,
        ann_bg=ann_bg,
        ann_text_col=ann_text_col,
        ann_copy=ann_copy,
        prefix=re.sub(r"[^A-Za-z]", "", brand_name).upper()[:3] or "ORD",
    )
    return v


# ----------------------------------------------------------------------------
# Step 3 — image selection
# ----------------------------------------------------------------------------
# Decorative page furniture that must never become a hero or product shot:
# bewakoof ships bg-desktop-gender-select gradients that outrank every real
# product photo on raw pixel area.
DECOR_IMG_RE = re.compile(
    r"(^|[/_.-])bg[-_.]|background|gradient|pattern|texture|placeholder|"
    r"swatch|spacer|sprite|divider|-select|payment|trust[-_]badge",
    re.I,
)


def _decorative(i):
    return bool(DECOR_IMG_RE.search(i["src"]))


def select_images(data):
    imgs = [i for i in (data.get("images") or [])
            if i["w"] > 80 and i["h"] > 80
            and "logo" not in i["src"].lower()
            and "logo" not in (i.get("alt") or "").lower()]
    imgs.sort(key=lambda i: i["w"] * i["h"], reverse=True)
    used = set()

    def take(pred, cap):
        for i in imgs:
            if i["src"] in used:
                continue
            if pred(i) and not _decorative(i):
                used.add(i["src"])
                return _bump_quality(i["src"], cap)
        return None

    hero = take(lambda i: i["w"] >= 400, 800)
    if not hero and data.get("bgImgs"):
        hero = _bump_quality(data["bgImgs"][0], 800)

    prods = []
    for _ in range(4):
        p = take(lambda i: i["h"] > i["w"] and i["w"] >= 100, 400)
        if p:
            prods.append(p)
    if len(prods) < 2:
        for _ in range(2 - len(prods)):
            p = take(lambda i: i["w"] >= 300, 400)
            if p:
                prods.append(p)

    showcase = take(lambda i: i["w"] >= 300, 800)

    ig = []
    for _ in range(5):
        g = take(lambda i: True, 200)
        if g:
            ig.append(g)

    return {"hero": hero, "prods": prods, "showcase": showcase, "ig": ig,
            "usable": len(imgs)}


# ----------------------------------------------------------------------------
# Step 4 — HTML build (structure baked in; only the skin changes per brand)
# ----------------------------------------------------------------------------
def img_tag(src, cls="", style="", alt="", fallback=None):
    """Never emoji. Fallback is a neutral grey div. No loading="lazy" — this
    is a single self-contained page, not an infinite feed, and native lazy
    loading only fires on a genuine viewport/visibility signal that doesn't
    reliably arrive for every embedding context (e.g. an iframe preview) —
    better to always load eagerly than risk a banner that never appears."""
    src = _safe_url(src)
    if src:
        return ('<img src="%s" alt="%s" class="%s" style="%s" '
                'onerror="this.style.display=\'none\'">' % (_e(src), _e(alt), cls, style))
    return '<div class="%s" style="background:#f0f0f0;%s"></div>' % (cls, style)


PROD_NAMES = ["Best Seller", "New Arrival", "Trending Now", "Editor's Pick"]
PROD_PRICES = ["₹1,199", "₹899", "₹1,499", "₹749"]

PAGE = Template(r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>$brand_name — Track your order</title>
<style>
$font_face_css
*{box-sizing:border-box;margin:0;padding:0}
body,*{font-family:$body_font}
body{background:#e0e0e0;display:flex;justify-content:center}
.page{width:100%;max-width:430px;background:$body_bg;color:$body_text;min-height:100vh;overflow:hidden}
.ann{background:$ann_bg;color:$ann_text_col;font-size:11px;text-align:center;padding:9px 12px;letter-spacing:.03em}
.ann a{color:inherit;font-weight:700}
.hdr{display:flex;align-items:center;gap:12px;padding:14px 16px;background:$header_bg;border-bottom:$header_border}
.hbg{width:22px;display:flex;flex-direction:column;gap:4px;flex-shrink:0;text-decoration:none;cursor:pointer}
.hbg span{height:2px;border-radius:2px;background:$header_icon}
.hlogo{flex:1;text-align:center;min-width:0}
.hact{flex-shrink:0;width:22px}
section{padding:18px 16px}
.card{background:#fff;border-radius:10px;padding:16px;border-bottom:2px solid $brand;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.g2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.lbl{font-size:10px;text-transform:uppercase;letter-spacing:.09em;opacity:.55;font-weight:700}
.val{font-size:13px;font-weight:700;margin-top:4px}
.div{height:1px;background:#eee;margin:14px 0}
.otp{display:flex;align-items:center;gap:12px;font-size:11px;opacity:.75}
.otp b{color:$brand;font-weight:800;white-space:nowrap;cursor:pointer}
.eta{font-size:28px;font-weight:900;margin-top:2px}
.st{font-size:22px;font-weight:900;margin-top:2px}
.status-badge{display:inline-block;margin-top:10px;background:$brand;color:#fff;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;padding:6px 12px;border-radius:$cta_radius}
.tl{border-left:3px solid $brand;padding-left:12px;background:$light;padding:10px 12px;border-radius:0 6px 6px 0}
.tl-s{font-size:12px;font-weight:800}
.tl-d{font-size:11px;opacity:.7;margin-top:2px}
.tl-t{font-size:10px;opacity:.5;margin-top:3px}
.sec-t{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.pimg{width:100%;aspect-ratio:3/4;object-fit:contain;background:#fff;display:block;border-radius:6px}
.pn{font-size:11px;font-weight:700;margin-top:6px}
.pp{font-size:12px;font-weight:900;margin-top:2px}
/* min-height + overflow:hidden: if the hero image dies, the absolutely
   positioned overlay must sit on a brand-coloured block, not bleed over the
   sections below (seen live on boat-lifestyle). */
.hero{position:relative;line-height:0;overflow:hidden;min-height:190px;background:$brand}
.hero img{width:100%;height:auto;display:block}
/* Copy must survive any photo underneath without a boxy plate: a taller,
   deeper bottom scrim + text-shadow keeps white type legible even on bright
   or baked-text campaign images. */
.hero .ov{position:absolute;inset:0;background:linear-gradient(to top,rgba(0,0,0,.82) 0%,rgba(0,0,0,.45) 34%,rgba(0,0,0,0) 62%);display:flex;flex-direction:column;justify-content:flex-end;padding:20px;line-height:1.2}
.hero h2{font-size:26px;font-weight:900;color:#fff;text-shadow:0 1px 4px rgba(0,0,0,.65),0 2px 14px rgba(0,0,0,.45)}
.hero p{font-size:12px;color:rgba(255,255,255,.92);margin:6px 0 12px;text-shadow:0 1px 3px rgba(0,0,0,.7)}
.hero button{align-self:flex-start;background:#fff;color:#111;border:none;padding:10px 18px;font-size:11px;font-weight:800;letter-spacing:.08em;border-radius:$cta_radius;cursor:pointer}
.ad-bg{display:flex;align-items:center;justify-content:space-between;padding:18px 20px;gap:12px;background:$brand}
.ad-text{min-width:0;flex:1}
.ad-text .at{font-size:9px;font-weight:700;color:#fff;opacity:.6;text-transform:uppercase;letter-spacing:.1em}
.ad-text .ab{font-size:18px;font-weight:900;color:#fff;line-height:1.1;word-break:keep-all;margin:4px 0}
.ad-text .ac{font-size:10px;color:rgba(255,255,255,.8)}
.ad-cta{flex-shrink:0;padding:10px 14px;white-space:nowrap;background:#fff;color:$brand;border:none;border-radius:$cta_radius;font-size:11px;font-weight:800;cursor:pointer}
.nps-card{background:#fff;border-radius:10px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.nps-q{font-size:13px;font-weight:600;line-height:1.45}
.nps-nums{display:flex;justify-content:space-between;margin:16px 0 10px}
.nn{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;cursor:pointer;background:#f2f2f2}
.nn.active{background:$brand;color:#fff}
.nps-track{position:relative;height:6px;border-radius:6px;background:linear-gradient(90deg,#ff5b5b,#ffc107,#28c76f)}
.nps-handle{position:absolute;top:-4px;width:14px;height:14px;border-radius:50%;background:#fff;border:3px solid $brand;transition:left .18s}
.nps-lbls{display:flex;justify-content:space-between;font-size:10px;opacity:.6;margin-top:10px}
.show{position:relative;line-height:0}
.show img{width:100%;height:auto;display:block;filter:brightness(.75)}
.play{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:56px;height:56px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center}
.ig-h{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.ig-av{width:34px;height:34px;border-radius:50%;object-fit:cover;background:#eee}
.ig-hn{font-size:12px;font-weight:800}
.ig-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:2px}
.ig-cell{aspect-ratio:1;overflow:hidden;background:#f0f0f0;position:relative;display:block}
.ig-cell img{width:100%;height:100%;object-fit:cover;display:block}
.ig-more{aspect-ratio:1;background:$brand;color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;text-decoration:none}
.ig-followers{font-size:10px;opacity:.55;font-weight:600;margin-left:auto}
.ig-stat{position:absolute;left:0;right:0;bottom:0;background:linear-gradient(transparent,rgba(0,0,0,.72));color:#fff;font-size:9px;font-weight:700;display:flex;gap:10px;justify-content:flex-end;padding:16px 6px 4px;line-height:1}
.ig-vid{position:absolute;top:6px;right:6px;opacity:.9}
.fb-card{background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.06);border-bottom:2px solid #1877f2}
.fb-head{display:flex;gap:10px;padding:14px 16px;align-items:center}
.fb-av{width:38px;height:38px;border-radius:50%;background:#1877f2;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:16px;flex-shrink:0;overflow:hidden}
.fb-av img{width:100%;height:100%;object-fit:cover}
.fb-name{font-weight:800;font-size:13px}
.fb-meta{font-size:10px;opacity:.6;margin-top:2px}
.fb-about{font-size:11px;padding:0 16px 12px;opacity:.8;line-height:1.5}
.fb-post{border-top:1px solid #eee;padding:12px 16px}
.fb-post-t{font-size:11px;line-height:1.5}
.fb-post img{width:100%;border-radius:8px;margin-top:8px;display:block}
.fb-stats{font-size:10px;opacity:.55;margin-top:8px;display:flex;gap:12px}
.fb-cta{display:block;margin:4px 16px 14px;text-align:center;background:#1877f2;color:#fff;padding:9px;border-radius:$cta_radius;font-size:11px;font-weight:800;text-decoration:none;letter-spacing:.06em}
.exp-t{font-size:13px;font-weight:700;text-align:center;margin-bottom:16px}
.exp-row{display:flex;justify-content:space-between;gap:6px}
.exp-item{flex:1;text-align:center;cursor:pointer}
.exp-c{width:42px;height:42px;margin:0 auto;border-radius:50%;background:#f2f2f2;display:flex;align-items:center;justify-content:center;font-size:20px;transition:.15s}
.exp-item.sel .exp-c{background:$brand;transform:scale(1.1)}
.exp-l{font-size:9px;margin-top:6px;opacity:.6}
.exp-sub{width:100%;margin-top:18px;padding:12px;background:transparent;border:1.5px solid $body_text;color:$body_text;font-size:11px;font-weight:800;letter-spacing:.08em;border-radius:$cta_radius;cursor:pointer}
.exp-sub:hover{background:$body_text;color:#fff}
.help-row{display:flex;align-items:center;gap:12px;margin-top:12px}
.help-ic{width:36px;height:36px;border-radius:8px;background:#f3f3f3;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.help-v{font-size:12px;font-weight:600}
.ft{background:$brand;padding:22px 16px;text-align:center}
.ft-l{color:#fff;font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;margin-bottom:14px}
.ft-r{display:flex;justify-content:center;gap:8px}
.ft-i{width:34px;height:34px;border-radius:8px;background:rgba(255,255,255,.15);display:flex;align-items:center;justify-content:center;text-decoration:none}
.ft-i svg{fill:#fff}
body.bb-edit [data-section]{cursor:pointer}
body.bb-edit [data-section]:hover{outline:1px dashed rgba(124,92,255,.55);outline-offset:-1px}
body.bb-edit [data-section].bb-sel{outline:2px solid #7c5cff;outline-offset:-2px}
</style></head><body><div class="page">
%%SECTIONS%%
</div>
<script>
(function(){
  const w=document.getElementById('npsNums'),h=document.getElementById('npsHandle');
  if(!w||!h)return;
  let s=5;
  function r(){
    w.innerHTML='';
    for(let i=0;i<=10;i++){
      const d=document.createElement('div');
      d.className='nn'+(i===s?' active':'');
      d.textContent=i;
      const _i=i;
      d.onclick=function(){s=_i;r();h.style.left='calc('+(_i/10*100)+'% - 7px)'};
      w.appendChild(d);
    }
    h.style.left='calc('+(s/10*100)+'% - 7px)';
  }
  r();
})();
function selExp(el){
  document.querySelectorAll('.exp-item').forEach(e=>e.classList.remove('sel'));
  el.classList.add('sel');
}
// Studio editor only: the live /page/<domain> link a seller shares never
// carries ?edit=1, so a real customer never sees hover outlines or fires
// selection messages — only the iframe preview inside the studio does.
(function(){
  if(new URLSearchParams(location.search).get('edit')!=='1') return;
  document.body.classList.add('bb-edit');
  function clearSel(){
    document.querySelectorAll('[data-section].bb-sel').forEach(function(e){
      e.classList.remove('bb-sel');
    });
  }
  document.querySelectorAll('[data-section]').forEach(function(el){
    el.addEventListener('click', function(e){
      // A link that already opens in a new tab (social icons, IG posts, the
      // FB CTA) is harmless to let through — the editor's own tab and state
      // are untouched, and a seller needs to actually be able to click these
      // to verify they go somewhere. Same-tab links (announcement CTA, the
      // hamburger) would navigate the preview away, so those stay select-only.
      if(!e.target.closest('a[target="_blank"]')) e.preventDefault();
      e.stopPropagation();
      clearSel(); el.classList.add('bb-sel');
      window.parent.postMessage({source:'brandboost',type:'select-section',
        id: el.getAttribute('data-section')}, '*');
    }, true);
  });
  window.addEventListener('message', function(e){
    const d=e.data||{};
    if(d.source!=='brandboost-editor'||d.type!=='highlight-section') return;
    clearSel();
    if(!d.id) return;
    const t=document.querySelector('[data-section="'+CSS.escape(d.id)+'"]');
    if(t){ t.classList.add('bb-sel'); t.scrollIntoView({block:'center',behavior:'smooth'}); }
  });
})();
</script></body></html>""")


# The editor's section vocabulary. Order here is the default page order;
# labels are what the studio side panel shows. Engagement (experience) sits
# right after the delivery status while it's fresh; social proof sits above
# the fold of the long tail, never below help.
SECTION_CATALOG = [
    ("ann", "Announcement bar"),
    ("header", "Header / logo"),
    ("order", "Order details"),
    ("status", "Delivery status"),
    ("experience", "Delivery feedback"),
    ("products", "You may also like"),
    ("hero", "Hero banner"),
    ("ad", "Promo strip"),
    ("instagram", "Instagram feed"),
    ("facebook", "Facebook page"),
    ("showcase", "Video showcase"),
    ("nps", "NPS survey"),
    ("help", "Need help"),
    ("footer", "Footer / social"),
]
DEFAULT_ORDER = [k for k, _ in SECTION_CATALOG]

# Uploaded logos arrive as data URIs; only raster formats — an SVG data URI
# could smuggle scripts into the page.
DATA_IMG_RE = re.compile(r"^data:image/(?:png|jpe?g|webp);base64,[A-Za-z0-9+/=]{40,}$")


def _compact(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%dK" % (n // 1_000)
    return str(n)


# Meta's CDNs answer curl but send Cross-Origin-Resource-Policy: same-origin,
# so a browser refuses to render them on our page. Those hosts must be routed
# through the studio's /img proxy; anything else hotlinks as before.
_CORP_HOST_RE = re.compile(r"\.(fbcdn\.net|cdninstagram\.com)$", re.I)


def proxy_img(url):
    u = _safe_url(url)
    if not u:
        return ""
    host = (urlparse(u).hostname or "").lower()
    return "/img?u=" + quote(u, safe="") if _CORP_HOST_RE.search(host) else u


def _logo_html(v, data, custom):
    """logo: uploaded data URI → <img> → inline svg → wordmark."""
    logo_data = (custom or {}).get("logo_data") or ""
    if DATA_IMG_RE.match(logo_data):
        return ('<img src="%s" alt="%s" style="height:24px;width:auto;'
                'display:block;margin:0 auto">' % (logo_data, _e(v["brand_name"])))
    logo_src = _safe_url(data.get("logoSrc"))
    if logo_src:
        return ('<img src="%s" alt="%s" style="height:24px;width:auto;display:block;'
                'margin:0 auto;%s" onerror="this.style.display=\'none\'">'
                % (_e(logo_src), _e(v["brand_name"]), v["logo_filter"]))
    logo_svg = _sanitize_svg(data.get("logoSvg"))
    if logo_svg:
        fill = "#fff" if v["header_dark"] else "#111"
        svg = re.sub(r'fill="(?!none)[^"]*"', 'fill="%s"' % fill, logo_svg)
        return '<div style="height:24px;display:flex;justify-content:center">%s</div>' % svg
    return ('<span style="font-size:18px;font-weight:900;letter-spacing:.18em;'
            'color:%s">%s</span>' % (v["header_icon"], _e(v["brand_name"].upper())))


def apply_custom_colors(v, colors):
    """Manual palette overrides from the studio editor. Derived fields
    (light tint, header contrast, announcement text) are recomputed so a
    single picker change cannot produce an unreadable page."""
    v = dict(v)
    c = {k: s.strip().lower() for k, s in (colors or {}).items()
         if isinstance(s, str) and HEX_RE.match(s.strip().lower())}
    if "brand" in c:
        v["brand"], v["light"] = c["brand"], _lighten(c["brand"], 0.88)
    if "header_bg" in c:
        v["header_bg"] = c["header_bg"]
        dark = _luminance(c["header_bg"]) < 0.5
        v["header_dark"] = dark
        v["header_icon"] = "#fff" if dark else "#111"
        v["header_border"] = "none" if dark else "1px solid #e8e8e8"
        # No pixel data at re-render time: a dark header always repaints the
        # logo white, a light one drops the filter.
        v["logo_filter"] = "filter:brightness(0) invert(1);" if dark else ""
    if "body_bg" in c:
        v["body_bg"] = c["body_bg"]
        if not _contrasts(v["body_text"], c["body_bg"]):
            v["body_text"] = _readable_on(c["body_bg"])
    if "body_text" in c:
        v["body_text"] = c["body_text"]
    if "ann_bg" in c:
        v["ann_bg"] = c["ann_bg"]
        v["ann_text_col"] = c.get("ann_text") or _readable_on(c["ann_bg"])
    elif "ann_text" in c:
        v["ann_text_col"] = c["ann_text"]
    return v


# Curated dropdown, not free text — a mistyped Google Font name silently
# falls back to the browser default with no error, which reads as a bug.
FONT_PRESETS = {
    "inter": "Inter", "poppins": "Poppins", "roboto": "Roboto",
    "lato": "Lato", "montserrat": "Montserrat", "nunito": "Nunito",
    "work-sans": "Work Sans", "open-sans": "Open Sans",
    "raleway": "Raleway", "playfair": "Playfair Display",
}


def apply_custom_font(v, font_key):
    """Seller-picked typeface overrides whatever the site computed —
    imported straight from Google Fonts by family name, no local @font-face
    needed."""
    fam = FONT_PRESETS.get((font_key or "").strip().lower())
    if not fam:
        return v
    v = dict(v)
    v["body_font"] = "'%s',system-ui,-apple-system,sans-serif" % fam
    v["font_face_css"] = ("@import url('https://fonts.googleapis.com/css2?family=%s"
                          ":wght@400;600;700;900&display=swap');" % quote(fam))
    return v


def _ig_section(v, picks, slug, social, logo_src):
    """Real scraped posts when the social tier delivered; otherwise the
    site-mined image grid; otherwise nothing."""
    heading = '<div class="sec-t">Instagram Feed</div>'
    ig = (social or {}).get("instagram")
    if ig and ig.get("posts"):
        cells = []
        for p in ig["posts"][:6]:
            img = proxy_img(p.get("image"))
            if not img:
                continue
            stats = '<div class="ig-stat"><span>&#9829; %s</span><span>&#128172; %s</span></div>' \
                % (_compact(p.get("likes")) or "0", _compact(p.get("comments")) or "0")
            vid = ('<span class="ig-vid"><svg width="12" height="12" viewBox="0 0 24 24" '
                   'fill="#fff"><path d="M8 5v14l11-7z"/></svg></span>') \
                if p.get("is_video") else ""
            link = _safe_url(p.get("url"))
            open_a = '<a class="ig-cell" href="%s" target="_blank" rel="noopener">' % _e(link) \
                if link else '<div class="ig-cell">'
            close_a = "</a>" if link else "</div>"
            cells.append('%s<img src="%s" alt="" '
                         'onerror="this.style.display=\'none\'">%s%s%s'
                         % (open_a, _e(img), stats, vid, close_a))
        if cells:
            handle = ig.get("handle") or slug
            more = ('<a class="ig-more" href="https://www.instagram.com/%s/" '
                    'target="_blank" rel="noopener">+%s</a>'
                    % (_e(handle), _compact(ig.get("total_posts")) or "MORE"))
            avatar_src = proxy_img(ig.get("profile_pic")) or logo_src
            avatar = ('<img class="ig-av" src="%s" alt="" '
                      'onerror="this.style.display=\'none\'">' % _e(avatar_src)) \
                if avatar_src \
                else '<div class="ig-av" style="background:%s"></div>' % v["brand"]
            followers = ('<div class="ig-followers">%s followers</div>'
                         % _compact(ig.get("followers"))) if ig.get("followers") else ""
            return ('<section data-section="instagram">%s<div class="ig-h">%s'
                    '<div class="ig-hn">@%s</div>%s</div>'
                    '<div class="ig-grid">%s%s</div></section>'
                    % (heading, avatar, _e(handle), followers, "".join(cells), more))

    ig_imgs = [u for u in picks["ig"] if _safe_url(u)][:5]
    if len(ig_imgs) < 3:
        return ""
    ig_cells = "".join(
        '<div class="ig-cell"><img src="%s" alt=""></div>'
        % _e(_safe_url(u)) for u in ig_imgs)
    ig_avatar = ('<img class="ig-av" src="%s" alt="">' % _e(logo_src)) if logo_src \
        else '<div class="ig-av" style="background:%s"></div>' % v["brand"]
    return ('<section data-section="instagram">%s<div class="ig-h">%s<div class="ig-hn">@%s</div></div>'
            '<div class="ig-grid">%s<div class="ig-more">+MORE</div></div></section>'
            % (heading, ig_avatar, _e(slug), ig_cells))


def _fb_section(social, logo_src):
    fb = (social or {}).get("facebook")
    if not fb or not fb.get("name"):
        return ""
    avatar = ('<div class="fb-av"><img src="%s" alt=""></div>' % _e(logo_src)) if logo_src \
        else '<div class="fb-av">%s</div>' % _e((fb["name"][:1] or "f").upper())
    meta_bits = []
    if fb.get("likes"):
        meta_bits.append("%s likes" % _compact(fb["likes"]))
    if fb.get("talking_about"):
        meta_bits.append(fb["talking_about"])
    meta = '<div class="fb-meta">%s</div>' % _e(" · ".join(meta_bits)) if meta_bits else ""
    about = '<div class="fb-about">%s</div>' % _e(fb["about"]) if fb.get("about") else ""
    posts_html = ""
    for p in (fb.get("posts") or [])[:2]:
        stats = []
        if p.get("likes"):
            stats.append("&#128077; %s" % _e(_compact(p["likes"])))
        if p.get("comments"):
            stats.append("&#128172; %s" % _e(_compact(p["comments"])))
        pimg = proxy_img(p.get("image"))
        posts_html += ('<div class="fb-post"><div class="fb-post-t">%s</div>%s%s</div>' % (
            _e(p.get("text") or ""),
            ('<img src="%s" alt="" '
             'onerror="this.style.display=\'none\'">' % _e(pimg)) if pimg else "",
            ('<div class="fb-stats">%s</div>' % "".join(
                '<span>%s</span>' % s for s in stats)) if stats else ""))
    cta = ('<a class="fb-cta" href="%s" target="_blank" rel="noopener">'
           'FOLLOW ON FACEBOOK</a>' % _e(_safe_url(fb.get("url")))) \
        if _safe_url(fb.get("url")) else ""
    return ('<section data-section="facebook"><div class="sec-t">Facebook Feed</div>'
            '<div class="fb-card"><div class="fb-head">%s'
            '<div><div class="fb-name">%s</div>%s</div></div>%s%s%s</div></section>'
            % (avatar, _e(fb["name"]), meta, about, posts_html, cta))


def _banner_html(sid, title, text):
    return ('<div class="ad-bg" data-section="%s"><div class="ad-text"><div class="ab">%s</div>'
            '<div class="ac">%s</div></div></div>'
            % (_e(sid), _e(str(title)[:60]), _e(str(text)[:200])))


def _build_sections(v, data, picks, slug, social, custom):
    """Every renderable section for this kit, keyed by catalog id. Sections
    whose data is missing are simply absent — a grid of grey boxes reads as
    a broken page."""
    logo_src = _safe_url(data.get("logoSrc"))
    logo_html = _logo_html(v, data, custom)
    e = {k: _e(v[k]) for k in ("brand_name", "ann_copy", "domain", "url", "prefix")}

    ann_cta = _e(v.get("ann_cta") or "Shop Now")
    s = {}
    s["ann"] = ('<div class="ann" data-section="ann">%s &nbsp;·&nbsp; '
                '<a href="%s">%s</a></div>'
                % (e["ann_copy"], e["url"], ann_cta))
    s["header"] = (
        '<div class="hdr" data-section="header">'
        '<a class="hbg" href="%s" aria-label="Visit store"><span></span><span></span>'
        '<span></span></a>'
        '<div class="hlogo">%s</div>'
        '<div class="hact"></div></div>'
        % (e["url"], logo_html))
    s["order"] = (
        '<section data-section="order"><div class="card"><div class="g2">'
        '<div><div class="lbl">Order ID</div><div class="val">%s-3928104</div></div>'
        '<div><div class="lbl">Order Placed On</div><div class="val">27th June, 2026</div></div>'
        '</div><div class="div"></div><div class="otp"><span>Verify yourself to see complete '
        'order details and take action.</span><b>Verify &rsaquo;</b></div></div></section>'
        % e["prefix"])
    s["status"] = (
        '<section style="padding-top:0" data-section="status"><div class="card"><div class="g2">'
        '<div><div class="lbl">Estimated Delivery</div><div class="eta">28 Jun</div></div>'
        '<div><div class="lbl">Your Order Is</div><div class="st">In Transit</div></div></div>'
        '<span class="status-badge">On The Way</span><div class="div"></div>'
        '<div class="lbl" style="margin-bottom:8px">Tracking History</div>'
        '<div class="tl"><div class="tl-s">Shipment In Transit</div>'
        '<div class="tl-d">Your package has left the sorting facility</div>'
        '<div class="tl-t">27 Jun 2026, 6:42 PM</div></div></div></section>')

    names = list(v.get("prod_names") or []) + PROD_NAMES
    prices = list(v.get("prod_prices") or []) + PROD_PRICES
    links = list(v.get("prod_links") or [])
    withimg = [(p, names[i], prices[i], _safe_url(links[i]) if i < len(links) else "")
               for i, p in enumerate(picks["prods"][:4]) if _safe_url(p)]
    n = 4 if len(withimg) >= 4 else 2 if len(withimg) >= 2 else 0
    if n:
        def _card(src, name, price, link):
            body = ('%s<div class="pn">%s</div><div class="pp">%s</div>'
                    % (img_tag(src, cls="pimg", alt=name), _e(name), _e(price)))
            return ('<a href="%s" style="display:block;text-decoration:none;color:inherit">%s</a>'
                    % (_e(link), body)) if link else '<div>%s</div>' % body
        cards = "".join(_card(*row) for row in withimg[:n])
        s["products"] = ('<section style="padding-bottom:0" data-section="products">'
                         '<div class="sec-t">You May Also Like</div>'
                         '<div class="pgrid">%s</div></section>' % cards)

    hero_img = img_tag(picks["hero"], style="width:100%;height:auto;display:block", alt="") \
        if picks["hero"] else \
        '<div style="width:100%%;height:220px;background:%s"></div>' % v["brand"]
    s["hero"] = (
        '<section style="padding:0" data-section="hero"><div class="hero">%s'
        '<div class="ov"><h2>%s<br>%s</h2>'
        '<p>%s</p><button>SHOP NOW</button></div></div></section>'
        % (hero_img,
           _e(v.get("hero_l1") or "NEW SEASON"), _e(v.get("hero_l2") or "NEW DROPS"),
           _e(v.get("hero_sub") or "Fresh styles, straight from %s" % v["brand_name"])))

    m = re.search(r"(\d{1,2}\s*%\s*OFF)", v["ann_copy"], re.I)
    ad_l1, ad_l2 = ("FLAT", m.group(1).upper()) if m else ("NEW", "ARRIVALS")
    s["ad"] = (
        '<div class="ad-bg" data-section="ad"><div class="ad-text"><div class="at">%s</div>'
        '<div class="ab">%s<br>%s</div><div class="ac">%s</div></div>'
        '<button class="ad-cta">Shop Now</button></div>'
        % (_e(v.get("ad_eyebrow") or "Limited Offer"),
           _e(v.get("ad_l1") or ad_l1), _e(v.get("ad_l2") or ad_l2),
           _e(v.get("ad_sub") or "On all orders. Use code %s25" % v["prefix"])))

    s["nps"] = (
        '<section data-section="nps"><div class="nps-card"><div class="nps-q">'
        'How likely are you to recommend '
        '<strong>%s</strong> to friends &amp; family?</div><div class="nps-nums" id="npsNums">'
        '</div><div class="nps-track"><div class="nps-handle" id="npsHandle"></div></div>'
        '<div class="nps-lbls"><span>&#128545; Not At All</span>'
        '<span>Very Likely &#128525;</span></div></div></section>' % e["brand_name"])

    # Showcase: a REAL playing video when the brand's Instagram has one
    # (served through the same-origin proxy — the CDN blocks hotlinking);
    # otherwise the static lifestyle image. The old play button was
    # decorative and read as a broken player.
    ig_video = next(
        (p for p in ((social or {}).get("instagram") or {}).get("posts") or []
         if p.get("video_url")), None)
    if ig_video and proxy_img(ig_video["video_url"]):
        s["showcase"] = (
            '<section style="padding:0" data-section="showcase"><video controls playsinline '
            'preload="none" poster="%s" style="width:100%%;display:block" '
            'src="%s"></video></section>'
            % (_e(proxy_img(ig_video.get("image"))),
               _e(proxy_img(ig_video["video_url"]))))
    elif picks["showcase"]:
        s["showcase"] = (
            '<section style="padding:0" data-section="showcase"><div class="show">%s'
            '<div class="play"><svg width="18" height="18" viewBox="0 0 24 24" '
            'fill="#111" style="margin-left:3px"><path d="M8 5v14l11-7z"/></svg>'
            '</div></div></section>'
            % img_tag(picks["showcase"],
                      style="width:100%;height:auto;display:block;filter:brightness(.75)"))

    ig = _ig_section(v, picks, slug, social, logo_src)
    if ig:
        s["instagram"] = ig
    fb = _fb_section(social, logo_src)
    if fb:
        s["facebook"] = fb

    s["experience"] = (
        '<section data-section="experience"><div class="exp-t">How was your delivery '
        'experience?</div><div class="exp-row">'
        + "".join('<div class="exp-item" onclick="selExp(this)"><div class="exp-c">&#%d;</div>'
                  '<div class="exp-l">%s</div></div>' % (cp, lbl)
                  for cp, lbl in ((128544, "Terrible"), (128533, "Bad"), (128528, "Okay"),
                                  (128522, "Good"), (128513, "Excellent")))
        + '</div><button class="exp-sub">SUBMIT FEEDBACK</button></section>')

    s["help"] = (
        '<section style="padding-top:0" data-section="help"><div class="sec-t">Need Help?</div>'
        '<div class="help-row"><div class="help-ic"><svg width="17" height="17" '
        'viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="1.7">'
        '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 6-10 7L2 6"/></svg>'
        '</div><div class="help-v">support@%s</div></div>'
        '<div class="help-row"><div class="help-ic"><svg width="17" height="17" '
        'viewBox="0 0 24 24" fill="none" stroke="#111" stroke-width="1.7">'
        '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 '
        '19.8 0 0 1 2 4.2 2 2 0 0 1 4 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 '
        '2.1L8 9.8a16 16 0 0 0 6 6l1.2-1.2a2 2 0 0 1 2.1-.5c.9.3 1.8.6 2.8.7a2 2 0 0 1 '
        '1.7 2z"/></svg></div><div class="help-v">+91-9876543210</div></div></section>'
        % e["domain"])

    footer_icons = "".join(
        '<a class="ft-i" href="%s" target="_blank" rel="noopener" aria-label="%s"><svg width="15" height="15" viewBox="0 0 24 24">%s</svg></a>'
        % (_e(link), label, path)
        for link, label, path in (
            (_safe_url((data.get("social") or {}).get("facebook")), "Facebook",
             '<path d="M13 22v-9h3l.5-3.5H13V7.4c0-1 .3-1.7 1.7-1.7h1.9V2.6A25 25 0 0 0 14 2.4c-2.6 0-4.4 1.6-4.4 4.6v2.5H6.5V13h3.1v9z"/>'),
            (_safe_url((data.get("social") or {}).get("instagram")), "Instagram",
             '<path d="M12 2.2c3.2 0 3.6 0 4.9.1 3.3.1 4.8 1.7 4.9 4.9.1 1.3.1 1.6.1 4.8s0 3.6-.1 4.9c-.1 3.2-1.6 4.8-4.9 4.9-1.3.1-1.6.1-4.9.1s-3.6 0-4.9-.1c-3.3-.2-4.8-1.7-4.9-4.9-.1-1.3-.1-1.6-.1-4.9s0-3.5.1-4.8C2.3 4 3.8 2.4 7.1 2.3c1.3-.1 1.7-.1 4.9-.1zm0 4.6a5.2 5.2 0 1 0 0 10.4 5.2 5.2 0 0 0 0-10.4zm0 8.6a3.4 3.4 0 1 1 0-6.8 3.4 3.4 0 0 1 0 6.8zm5.4-8.8a1.2 1.2 0 1 0 0-2.4 1.2 1.2 0 0 0 0 2.4z"/>'),
            (_safe_url((data.get("social") or {}).get("twitter") or (data.get("social") or {}).get("x.com")), "X / Twitter",
             '<path d="M18.2 2H21l-6.4 7.3L22 22h-5.9l-4.6-6-5.3 6H3.4l6.9-7.8L2.5 2h6l4.2 5.5zm-1 18h1.6L7.9 3.7H6.2z"/>'),
            (_safe_url((data.get("social") or {}).get("youtube")), "YouTube",
             '<path d="M23 12s0-3.8-.5-5.6a2.9 2.9 0 0 0-2-2C18.7 4 12 4 12 4s-6.7 0-8.5.5a2.9 2.9 0 0 0-2 2C1 8.2 1 12 1 12s0 3.8.5 5.6a2.9 2.9 0 0 0 2 2C5.3 20 12 20 12 20s6.7 0 8.5-.5a2.9 2.9 0 0 0 2-2C23 15.8 23 12 23 12zM9.8 15.4V8.6l5.9 3.4z"/>'),
        ) if link)
    s["footer"] = (
        '<div class="ft" data-section="footer"><div class="ft-l">Follow Us</div>'
        '<div class="ft-r">%s</div></div>' % footer_icons)
    return s


def available_sections(v, data, picks, slug, social=None):
    """Section ids this kit can actually render, in default order — feeds the
    studio editor's add/remove list."""
    snips = _build_sections(v, data, picks, slug, social, None)
    return [k for k in DEFAULT_ORDER if k in snips]


def build_tracking_html(v, data, picks, slug, social=None, custom=None):
    """Assemble the page. `social` is socialkit.fetch_all() output (or None);
    `custom` is the studio editor's overrides: {"colors": {...},
    "logo_data": "data:image/png;base64,...", "sections": [{"id": ...}, ...]}.
    Custom section entries with id "banner:<x>" render a brand-coloured text
    banner from their title/text. custom["copy"] carries per-brand text
    overrides (hero_l1/hero_l2/hero_sub, ad_eyebrow/ad_l1/ad_l2/ad_sub) —
    the AI curation pass uses this to fix copy that doesn't suit the brand,
    e.g. no "NEW ARRIVALS" on a food-delivery page."""
    custom = custom or {}
    v = apply_custom_colors(v, custom.get("colors"))
    v = apply_custom_font(v, custom.get("font"))
    copy = custom.get("copy") or {}
    for k in ("ann_copy", "ann_cta", "hero_l1", "hero_l2", "hero_sub",
              "ad_eyebrow", "ad_l1", "ad_l2", "ad_sub"):
        if isinstance(copy.get(k), str) and copy[k].strip():
            v[k] = copy[k].strip()
    snips = _build_sections(v, data, picks, slug, social, custom)

    wanted = custom.get("sections")
    parts = []
    if isinstance(wanted, list) and wanted:
        for entry in wanted:
            if not isinstance(entry, dict):
                entry = {"id": str(entry)}
            sid = str(entry.get("id") or "")
            if sid.startswith("banner:"):
                parts.append(_banner_html(sid, entry.get("title") or "",
                                          entry.get("text") or ""))
            elif sid in snips:
                parts.append(snips[sid])
    else:
        parts = [snips[k] for k in DEFAULT_ORDER if k in snips]

    # Head/CSS vars are validated colours/fonts; the sections marker is
    # replaced with plain str.replace AFTER substitution so remote text
    # containing "$brand" can never trigger a second template pass.
    sub = dict(v)
    sub["brand_name"] = _e(v["brand_name"])
    return PAGE.safe_substitute(sub).replace("%%SECTIONS%%", "".join(parts))


def kit_to_args(kit):
    """(v, data, picks) reconstructed from a cached kit JSON — lets the studio
    re-render a customized page without re-running the extraction pipeline."""
    c, t = kit["colors"], kit["typography"]
    v = {
        "domain": kit["domain"], "url": kit["url"],
        "brand": c["brand"], "light": c["light"],
        "header_bg": c["header_bg"], "header_icon": c["header_icon"],
        "header_border": c["header_border"], "header_dark": c["header_dark"],
        "logo_filter": kit["logo"].get("filter") or "",
        "cta_radius": kit["cta_radius"],
        "body_bg": c["body_bg"], "body_text": c["body_text"],
        "body_font": t["body_font"], "font_face_css": t["font_face_css"],
        "brand_name": kit["brand_name"], "prefix": kit["prefix"],
        "ann_bg": c["ann_bg"], "ann_text_col": c["ann_text"],
        "ann_copy": kit["announcement"],
        "hero_l1": kit["hero"]["l1"], "hero_l2": kit["hero"]["l2"],
        "hero_sub": kit["hero"]["sub"],
        "ad_eyebrow": kit["ad"]["eyebrow"], "ad_l1": kit["ad"]["l1"],
        "ad_l2": kit["ad"]["l2"], "ad_sub": kit["ad"]["sub"],
        "prod_names": [p["name"] for p in kit["products"]],
        "prod_prices": [p["price"] for p in kit["products"]],
        "prod_links": [p.get("link") or "" for p in kit["products"]],
    }
    data = {"logoSrc": kit["logo"]["src"], "logoSvg": kit["logo"]["svg"],
            "social": kit.get("social") or {}}
    picks = {"hero": kit["hero"]["image"],
             "prods": [p["image"] for p in kit["products"]],
             "showcase": kit["showcase_image"],
             "ig": kit["instagram"]["images"], "usable": 0}
    return v, data, picks


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        sys.exit(1)
    url = args[0]
    slug = args[1] if len(args) > 1 else \
        urlparse(url).netloc.replace("www.", "").split(".")[0]

    print("→ extracting %s ..." % url)
    data = extract(url)

    with open("%s_extract.json" % slug, "w") as f:
        json.dump(data, f, indent=2)

    v = derive(data, url)
    picks = select_images(data)

    # AI enrichment layer. Heuristics above are the floor: a gateway outage
    # degrades the page, it never fails the run.
    if "--no-ai" not in flags:
        import brandai
        print("→ asking the gateway to read the brand ...")
        v, picks, report = brandai.enrich(data, url, v, picks)
        if report["ok"]:
            tok = report.get("tokens") or {}
            print("  ai applied  : %s" % (", ".join(report["applied"]) or "nothing"))
            print("  ai tone     : %s" % (report.get("tone") or "—"))
            print("  ai tokens   : %s in / %s out  (digest %d B)"
                  % (tok.get("input", "?"), tok.get("output", "?"),
                     report.get("digest_bytes", 0)))
            if report.get("notes"):
                print("  ai notes    : %s" % report["notes"])
        else:
            print("  ! ai skipped: %s  (falling back to heuristics)" % report["error"])

    # Print the extraction so you can VERIFY before trusting the page.
    print("\n--- verify against the live site ---")
    print("  brand_name : %s" % v["brand_name"])
    print("  header_bg  : %s  (dark=%s)" % (v["header_bg"], v["header_dark"]))
    print("  brand      : %s" % v["brand"])
    print("  body_bg    : %s" % v["body_bg"])
    print("  cta_radius : %s" % v["cta_radius"])
    print("  font       : %s" % v["body_font"])
    print("  logo       : %s" % (data.get("logoSrc") or
                                 ("<inline svg>" if data.get("logoSvg") else "NONE → wordmark")))
    print("  logo_filter: %s" % (v["logo_filter"] or "none"))
    print("  ann_copy   : %s" % v["ann_copy"])
    print("  images     : %d found / %d usable | hero=%s prods=%d showcase=%s ig=%d"
          % (len(data.get("images") or []), picks["usable"], bool(picks["hero"]),
             len(picks["prods"]), bool(picks["showcase"]), len(picks["ig"])))
    print("-----------------------------------\n")

    html = build_tracking_html(v, data, picks, slug)
    out = "%s_tracking.html" % slug
    with open(out, "w") as f:
        f.write(html)
    print("✓ wrote %s (%.1f KB)" % (out, os.path.getsize(out) / 1024))
    print("  run: python3 server.py  →  http://localhost:8765/%s" % slug)


if __name__ == "__main__":
    # brandai imports brandboost. Without this alias, that import re-executes
    # this file under a second module name and we end up with two copies of
    # every regex and of PAGE.
    sys.modules.setdefault("brandboost", sys.modules["__main__"])
    main()
