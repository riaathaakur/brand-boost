#!/usr/bin/env python3
"""server.py — Brand Boost studio.

    python3 server.py           →  http://localhost:8765

Paste a store URL, get its branded tracking page. The server runs the tiered
brandkit pipeline (static → AI digest → AI web-search, each tier only when the
previous left gaps) and caches per-domain results in .cache/ — a repeat render
never re-extracts.

Endpoints:
    GET  /                        the studio UI (frontend.html)
    GET  /api/generate?url=...    run pipeline, JSON report   (&force=1 to redo)
    GET  /api/kit?slug=...        kit + editor state for the customize panel
    POST /api/customize           save section/colour/logo overrides, re-render
    GET  /page/<domain>           the generated tracking page (iframe target)
"""
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import requests

import brandboost as bb
import brandkit
import socialkit

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, ".cache")
os.makedirs(CACHE, exist_ok=True)
IMG_CACHE = os.path.join(CACHE, "img")
os.makedirs(IMG_CACHE, exist_ok=True)
PORT = int(os.environ.get("PORT", 8765))

# Only Meta's CDNs may be proxied — they send
# Cross-Origin-Resource-Policy: same-origin, so the browser cannot hotlink
# them and the studio must relay the bytes (images AND the reel mp4s the
# showcase player uses). Everything else stays direct; an open relay would
# be an SSRF hole.
IMG_HOST_RE = re.compile(r"\.(fbcdn\.net|cdninstagram\.com)$", re.I)
MAX_IMG_BYTES = 3_000_000
MAX_VIDEO_BYTES = 25_000_000

# One extraction at a time per domain; concurrent submits of the same store
# must not run the pipeline twice.
_locks = {}
_locks_guard = threading.Lock()

DOMAIN_RE = re.compile(r"^[a-z0-9.-]+$")


def _domain_lock(domain):
    with _locks_guard:
        return _locks.setdefault(domain, threading.Lock())


def _check_target(url):
    """Only public http(s) hosts. The server fetches whatever is typed in, so
    private ranges and localhost must be rejected (SSRF)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return "URL must start with http:// or https://"
    host = p.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "could not resolve %s" % host
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return "refusing to fetch a private/internal address"
    return ""


def _slug(url):
    d = urlparse(url).netloc.replace("www.", "").lower()
    return d if DOMAIN_RE.match(d) else ""


def _ui_stamp():
    """mtime of frontend.html — lets an open tab detect it is outdated."""
    try:
        return str(int(os.path.getmtime(os.path.join(ROOT, "frontend.html"))))
    except OSError:
        return "0"


def _paths(slug):
    return {k: os.path.join(CACHE, slug + ext) for k, ext in
            (("html", ".html"), ("kit", ".json"),
             ("social", ".social.json"), ("custom", ".custom.json"),
             ("curated", ".curated.json"), ("pool", ".pool.json"))}


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _fetch_social(slug, kit):
    """Real FB/IG posts for this brand, cached per domain — the dedicated
    ScrapingDog endpoints bill ~15 credits a call, so never re-fetch on a
    plain re-render. Brand name/domain feed the verified handle-probing
    fallback for storefronts that render social links client-side."""
    p = _paths(slug)
    social = _read_json(p["social"])
    if social is not None:
        return social, False
    social = socialkit.fetch_all(kit.get("social") or {},
                                 brand_name=kit.get("brand_name") or "",
                                 domain=kit.get("domain") or "")
    # A quota failure is transient — caching it would freeze the brand on
    # "no socials" forever. Leave no file so the next render retries.
    if "quota" in (social.get("error") or ""):
        print("  ! social skipped: %s" % social["error"])
        return social, False
    _write_json(p["social"], social)
    return social, True


def _vet_images(slug, kit, data):
    """AI re-pick of hero/product/showcase images from the mined pool —
    the heuristic ranks by pixel area, which elects decorative background
    gradients on sites like bewakoof. Loops pick→judge→re-pick up to
    SR_AI_MAX_LOOPS. Shopify catalogue product shots are authoritative and
    never overwritten; only the picks that came from mining are."""
    pool = [{"src": i.get("src") or "", "w": i.get("w") or 0,
             "h": i.get("h") or 0, "alt": (i.get("alt") or "")[:80]}
            for i in (data.get("images") or [])
            if (i.get("w") or 0) >= 200 and (i.get("h") or 0) >= 200
            and (i.get("src") or "").startswith("http")][:100]
    p = _paths(slug)
    _write_json(p["pool"], pool)
    if not pool:
        return
    facts = {"brand_name": kit["brand_name"], "domain": kit["domain"],
             "tone": kit["meta"].get("tone") or "",
             "business": kit["meta"].get("platform") or "generic"}
    try:
        import brandai
        picks, tokens, vmeta = brandai.vet_images(facts, pool)
    except Exception as exc:
        print("  ! image vet skipped: %s" % exc)
        return
    meta = kit["meta"]
    meta.setdefault("tiers", []).append("ai_image_vet")
    meta["tokens_vet"] = tokens
    meta["vet_loops"] = vmeta.get("calls", 0)
    meta["vet_approved"] = vmeta.get("approved", False)
    usd = round(brandkit._tier_usd(tokens), 4)
    cost = meta.setdefault("cost", {"per_tier_usd": {}, "total_usd": 0.0})
    cost.setdefault("per_tier_usd", {})["ai_image_vet"] = usd
    cost["total_usd"] = round((cost.get("total_usd") or 0.0) + usd, 4)
    if not picks:
        return

    if picks.get("hero"):
        kit["hero"]["image"] = bb._bump_quality(picks["hero"], 800)
    elif vmeta.get("approved"):
        # The judge approved an EMPTY hero pick — every candidate was a
        # designed banner or background. The copy renders on a solid brand
        # panel instead, which always contrasts.
        kit["hero"]["image"] = ""
    is_shopify = "shopify_products" in meta.get("tiers", [])
    prods = [u for u in (picks.get("prods") or []) if u]
    if not is_shopify:
        if vmeta.get("approved") and len(prods) >= 2:
            names = [q["name"] for q in kit["products"]] + bb.PROD_NAMES
            prices = [q["price"] for q in kit["products"]] + bb.PROD_PRICES
            n = 4 if len(prods) >= 4 else 2
            kit["products"] = [
                {"image": bb._bump_quality(u, 400), "name": names[i],
                 "price": prices[i]}
                for i, u in enumerate(prods[:n])]
        else:
            # The judge never signed off — this pool has no real product
            # shots (category tiles, promo banners). A wrong product grid is
            # worse than no grid; the section hides.
            kit["products"] = []
            meta["vet_dropped_products"] = True
    if picks.get("showcase"):
        kit["showcase_image"] = bb._bump_quality(picks["showcase"], 800)


def _curate(slug, kit):
    """One AI pass deciding WHICH sections this brand's page should carry
    (a food-delivery brand gets no "NEW ARRIVALS" strip) plus copy fixes.
    Cached per domain — including failures, so a broken gateway can't bill
    a retry on every render. Returns (curated, freshly_fetched)."""
    p = _paths(slug)
    cur = _read_json(p["curated"])
    if cur is not None:
        return cur, False
    social = _read_json(p["social"]) or {}
    ig = social.get("instagram") or {}
    fb = social.get("facebook") or {}
    v, data, picks = bb.kit_to_args(kit)
    facts = {
        "brand_name": kit["brand_name"], "domain": kit["domain"],
        "platform": kit["meta"].get("platform") or "generic",
        "product_count": len(kit["products"]),
        "product_names": [q["name"] for q in kit["products"]][:4],
        "announcement": kit["announcement"],
        "hero_copy": {"l1": kit["hero"]["l1"], "l2": kit["hero"]["l2"],
                      "sub": kit["hero"]["sub"]},
        "ad_copy": kit["ad"],
        "available_sections": bb.available_sections(
            v, data, picks, slug.split(".")[0], social=social),
        "brand_tone": kit["meta"].get("tone") or "",
        "instagram": {"available": bool(ig.get("posts")),
                      "followers": ig.get("followers") or 0,
                      "bio": ig.get("bio") or ""},
        "facebook": {"available": bool(fb),
                     "about": fb.get("about") or "",
                     "likes": fb.get("likes") or 0},
    }
    try:
        import brandai
        clean, tokens, cmeta = brandai.curate(facts)
    except Exception as exc:                       # gateway down ≠ broken page
        clean, tokens, cmeta = None, {}, {}
        print("  ! curate skipped: %s" % exc)
    cur = dict(clean or {}, ok=bool(clean), tokens=tokens,
               loops=cmeta.get("calls", 0), approved=cmeta.get("approved", False))
    _write_json(p["curated"], cur)
    return cur, True


def _apply_curate_cost(meta, cur):
    if not cur or not cur.get("tokens"):
        return
    tiers = meta.setdefault("tiers", [])
    if "ai_curate" not in tiers:
        tiers.append("ai_curate")
    meta["tokens_curate"] = cur["tokens"]
    usd = round(brandkit._tier_usd(cur["tokens"]), 4)
    cost = meta.setdefault("cost", {"per_tier_usd": {}, "total_usd": 0.0})
    cost.setdefault("per_tier_usd", {})["ai_curate"] = usd
    cost["total_usd"] = round((cost.get("total_usd") or 0.0) + usd, 4)


def _effective_custom(slug):
    """The user's manual edits win over the AI curation, field by field:
    sections/colors/logo from the editor when present, curated section list
    otherwise, curated copy fixes as the base with the editor's own copy
    fields (announcement/hero/ad text) overriding per-key — a seller typing
    a correction must never get silently clobbered by the next re-render."""
    p = _paths(slug)
    user = _read_json(p["custom"]) or {}
    cur = _read_json(p["curated"]) or {}
    eff = dict(user)
    if not eff.get("sections") and cur.get("ok") and cur.get("sections"):
        eff["sections"] = [{"id": s} for s in cur["sections"]]
    if cur.get("ok") and cur.get("copy"):
        merged = dict(cur["copy"])
        merged.update(user.get("copy") or {})
        eff["copy"] = merged
    elif user.get("copy"):
        eff["copy"] = user["copy"]
    return eff or None


def _render(slug, kit, social):
    """Kit JSON → tracking page HTML, honouring curation + customization."""
    p = _paths(slug)
    v, data, picks = bb.kit_to_args(kit)
    page = bb.build_tracking_html(v, data, picks, slug.split(".")[0],
                                  social=social,
                                  custom=_effective_custom(slug))
    with open(p["html"], "w") as f:
        f.write(page)


def generate(url, force=False):
    slug = _slug(url)
    if not slug:
        raise ValueError("that does not look like a valid store URL")
    p = _paths(slug)

    with _domain_lock(slug):
        if not force and os.path.exists(p["html"]) and os.path.exists(p["kit"]):
            kit = _read_json(p["kit"]) or {}
            # Kits cached before the social/curate tiers existed gain them
            # lazily — run each once, fold the spend into the cached meta,
            # re-render.
            changed = False
            if not os.path.exists(p["social"]):
                social, fresh = _fetch_social(slug, kit)
                if fresh and social.get("requests"):
                    socialkit.apply_cost(kit["meta"], social)
                    # New social data invalidates a plan curated without it —
                    # the feeds must get their prominent slot.
                    if (social.get("instagram") or social.get("facebook")) \
                            and os.path.exists(p["curated"]):
                        os.remove(p["curated"])
                changed = True
            if not os.path.exists(p["curated"]):
                cur, fresh = _curate(slug, kit)
                if fresh:
                    _apply_curate_cost(kit["meta"], cur)
                changed = True
            if changed:
                _write_json(p["kit"], kit)
                _render(slug, kit, _read_json(p["social"]))
            kit["meta"]["cached"] = True
            return slug, kit

        # The loop spends tokens only where static confidence was low:
        # AI digest unless everything resolved, web search only for weak fields.
        kit, v, data, picks = brandkit.run(url, slug.split(".")[0], use_ai=True)
        for stale in ("social", "curated"):        # force refetches these too
            if force and os.path.exists(p[stale]):
                os.remove(p[stale])
        _vet_images(slug, kit, data)               # fix decorative mis-picks
        social, fresh = _fetch_social(slug, kit)
        if fresh and social.get("requests"):
            socialkit.apply_cost(kit["meta"], social)
        cur, fresh = _curate(slug, kit)
        if fresh:
            _apply_curate_cost(kit["meta"], cur)

        _write_json(p["kit"], kit)
        _render(slug, kit, social)
        kit["meta"]["cached"] = False
        return slug, kit


def _social_summary(slug):
    social = _read_json(_paths(slug)["social"]) or {}
    ig, fb = social.get("instagram"), social.get("facebook")
    return {
        "ig_posts": len((ig or {}).get("posts") or []),
        "ig_followers": (ig or {}).get("followers_compact") or "",
        "fb": bool(fb),
        "fb_likes": (fb or {}).get("likes_compact") or "",
        "fb_posts": len((fb or {}).get("posts") or []),
        "credits": social.get("credits") or 0,
    }


# What the customize endpoint accepts. Colours are validated as #rrggbb,
# sections against the catalog, the logo as a raster data URI.
COLOR_KEYS = {"brand", "header_bg", "body_bg", "body_text", "ann_bg", "ann_text"}
HEX_OK = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_LOGO_CHARS = 800_000          # ~600 KB decoded
MAX_SECTIONS = 30
# Per-field length caps — generous enough for real copy, tight enough that a
# pasted essay can't blow out the template's fixed layout.
COPY_KEYS = {"ann_copy": 160, "ann_cta": 30, "hero_l1": 40, "hero_l2": 40,
             "hero_sub": 100, "ad_eyebrow": 30, "ad_l1": 30, "ad_l2": 30,
             "ad_sub": 80}


def _validate_custom(custom):
    """→ (cleaned_custom_or_None, error). None custom means 'reset'."""
    if custom in (None, {}, []):
        return None, ""
    if not isinstance(custom, dict):
        return None, "custom must be an object"
    clean = {}

    colors = custom.get("colors") or {}
    if not isinstance(colors, dict):
        return None, "colors must be an object"
    keep = {k: s.strip().lower() for k, s in colors.items()
            if k in COLOR_KEYS and isinstance(s, str) and HEX_OK.match(s.strip())}
    if keep:
        clean["colors"] = keep

    logo = custom.get("logo_data") or ""
    if logo:
        if not isinstance(logo, str) or len(logo) > MAX_LOGO_CHARS \
                or not bb.DATA_IMG_RE.match(logo):
            return None, "logo must be a png/jpeg/webp data URI under 600KB"
        clean["logo_data"] = logo

    sections = custom.get("sections")
    if sections is not None:
        if not isinstance(sections, list) or len(sections) > MAX_SECTIONS:
            return None, "sections must be a list of at most %d" % MAX_SECTIONS
        rows = []
        for entry in sections:
            if not isinstance(entry, dict):
                return None, "each section must be an object"
            sid = str(entry.get("id") or "")
            if sid in bb.DEFAULT_ORDER:
                rows.append({"id": sid})
            elif sid.startswith("banner:") and len(sid) <= 40:
                rows.append({"id": sid,
                             "title": str(entry.get("title") or "")[:60],
                             "text": str(entry.get("text") or "")[:200]})
            else:
                return None, "unknown section id: %s" % sid[:40]
        clean["sections"] = rows

    copy = custom.get("copy") or {}
    if not isinstance(copy, dict):
        return None, "copy must be an object"
    keep_copy = {}
    for k, limit in COPY_KEYS.items():
        val = copy.get(k)
        if isinstance(val, str) and val.strip():
            keep_copy[k] = val.strip()[:limit]
    if keep_copy:
        clean["copy"] = keep_copy

    font = custom.get("font")
    if font:
        if not isinstance(font, str) or font not in bb.FONT_PRESETS:
            return None, "unknown font: %s" % str(font)[:40]
        clean["font"] = font

    return (clean or None), ""


def customize(slug, custom):
    p = _paths(slug)
    with _domain_lock(slug):
        kit = _read_json(p["kit"])
        if not kit:
            raise ValueError("no kit cached for %s — generate it first" % slug)
        clean, err = _validate_custom(custom)
        if err:
            raise ValueError(err)
        if clean is None:
            if os.path.exists(p["custom"]):
                os.remove(p["custom"])
        else:
            _write_json(p["custom"], clean)
        _render(slug, kit, _read_json(p["social"]))
        return clean


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _proxy_img(self, u):
        """Relay one Meta-CDN image. Host allowlist + https only + no
        redirects: this must never become a generic fetch-anything relay.
        Bytes are disk-cached — the signed CDN URLs expire in weeks, the
        page shouldn't."""
        pu = urlparse(u)
        host = (pu.hostname or "").lower()
        if pu.scheme != "https" or not IMG_HOST_RE.search(host):
            self._send(403, "host not allowed", "text/plain")
            return
        key = hashlib.sha1(u.encode("utf-8")).hexdigest()
        body_path = os.path.join(IMG_CACHE, key)
        ct_path = body_path + ".ct"
        if os.path.exists(body_path) and os.path.exists(ct_path):
            with open(ct_path) as f:
                ct = f.read().strip() or "image/jpeg"
            with open(body_path, "rb") as f:
                self._send(200, f.read(), ct,
                           extra={"Cache-Control": "public, max-age=604800"})
            return
        try:
            r = requests.get(u, timeout=60, allow_redirects=False)
        except requests.RequestException:
            self._send(502, "upstream fetch failed", "text/plain")
            return
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        cap = MAX_VIDEO_BYTES if ct.startswith("video/") else MAX_IMG_BYTES
        if r.status_code != 200 \
                or not (ct.startswith("image/") or ct.startswith("video/")) \
                or len(r.content) > cap:
            self._send(404, "not a proxyable asset", "text/plain")
            return
        with open(body_path, "wb") as f:
            f.write(r.content)
        with open(ct_path, "w") as f:
            f.write(ct)
        self._send(200, r.content, ct,
                   extra={"Cache-Control": "public, max-age=604800"})

    def do_GET(self):
        p = urlparse(self.path)

        if p.path == "/img":
            q = parse_qs(p.query)
            self._proxy_img((q.get("u") or [""])[0])
            return

        if p.path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "frontend.html"), encoding="utf-8") as f:
                # no-store: the studio UI iterates fast; a browser-cached copy
                # silently hides new panels (this bit a real user).
                body = f.read().replace("__UI_STAMP__", _ui_stamp())
                self._send(200, body, extra={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy":
                        "default-src 'self'; style-src 'unsafe-inline'; "
                        "script-src 'unsafe-inline'; img-src 'self' data:; "
                        "frame-src 'self'"})
            return

        if p.path == "/api/generate":
            q = parse_qs(p.query)
            url = (q.get("url") or [""])[0].strip()
            if url and not urlparse(url).scheme:
                url = "https://" + url
            err = url and _check_target(url) or (not url and "no url given") or ""
            if err:
                self._json(400, {"ok": False, "error": err})
                return
            try:
                slug, kit = generate(url, force=(q.get("force") or ["0"])[0] == "1")
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:                      # extraction died: say so
                traceback.print_exc()
                self._json(502, {"ok": False,
                                 "error": "extraction failed: %s" % exc})
                return
            social = _social_summary(slug)
            self._json(200, {
                "ok": True, "page": "/page/" + quote(slug), "slug": slug,
                "ui_stamp": _ui_stamp(),
                "brand_name": kit["brand_name"],
                "colors": kit["colors"], "typography": kit["typography"],
                "logo": kit["logo"]["mode"], "products": len(kit["products"]),
                "hero": bool(kit["hero"]["image"]),
                "instagram": social["ig_posts"] or len(kit["instagram"]["images"]),
                "social": social,
                "meta": kit["meta"],
            })
            return

        if p.path == "/api/kit":
            q = parse_qs(p.query)
            slug = (q.get("slug") or [""])[0].strip()
            if not DOMAIN_RE.match(slug):
                self._json(400, {"ok": False, "error": "bad slug"})
                return
            paths = _paths(slug)
            kit = _read_json(paths["kit"])
            if not kit:
                self._json(404, {"ok": False, "error": "no kit for %s" % slug})
                return
            social = _read_json(paths["social"])
            v, data, picks = bb.kit_to_args(kit)
            self._json(200, {
                "ok": True, "slug": slug,
                "colors": kit["colors"],
                "typography": kit["typography"],
                "available": bb.available_sections(
                    v, data, picks, slug.split(".")[0], social=social),
                "labels": dict(bb.SECTION_CATALOG),
                "custom": _read_json(paths["custom"]),
                "curated": _read_json(paths["curated"]),
                "social": _social_summary(slug),
                "font_options": [{"key": k, "label": fam}
                                 for k, fam in bb.FONT_PRESETS.items()],
                "defaults": {
                    "ann_copy": kit["announcement"], "ann_cta": "Shop Now",
                    "hero_l1": kit["hero"]["l1"], "hero_l2": kit["hero"]["l2"],
                    "hero_sub": kit["hero"]["sub"],
                    "ad_eyebrow": kit["ad"]["eyebrow"], "ad_l1": kit["ad"]["l1"],
                    "ad_l2": kit["ad"]["l2"], "ad_sub": kit["ad"]["sub"],
                },
            })
            return

        m = re.match(r"^/page/([a-z0-9.-]+)$", p.path)
        if m and DOMAIN_RE.match(m.group(1)):
            path = os.path.join(CACHE, m.group(1) + ".html")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self._send(200, f.read())
                return
        self._send(404, "not found", "text/plain")

    MAX_BODY = 1_500_000          # logo data URI + sections, with headroom

    def do_POST(self):
        p = urlparse(self.path)
        if p.path != "/api/customize":
            self._send(404, "not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= self.MAX_BODY:
            self._json(413, {"ok": False, "error": "body missing or too large"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        slug = str(body.get("slug") or "")
        if not DOMAIN_RE.match(slug):
            self._json(400, {"ok": False, "error": "bad slug"})
            return
        try:
            clean = customize(slug, body.get("custom"))
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"ok": False, "error": "re-render failed: %s" % exc})
            return
        self._json(200, {"ok": True, "custom": clean,
                         "page": "/page/" + quote(slug)})

    def log_message(self, fmt, *args):                    # quieter console
        if "/api/" in (args[0] if args else ""):
            print("  %s" % (fmt % args))


if __name__ == "__main__":
    print("Brand Boost studio → http://localhost:%d" % PORT)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
