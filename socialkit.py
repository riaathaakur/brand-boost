#!/usr/bin/env python3
"""socialkit.py — real Facebook / Instagram posts via ScrapingDog.

Verified against the live API (July 2026):

    instagram/profile?username=<handle>   → profile incl. numeric profile_id
    instagram/posts?id=<profile_id>       → posts_data[] (thumbnail, caption,
                                            likes, comment, shortcode, …)
    facebook/posts?username=<page>        → posts_data[] — UNSTABLE upstream:
                                            returns 400 for most pages, so it
                                            is best-effort only
    facebook/profile?username=<page>      → title, likes, info[], url — solid

The page section degrades gracefully: IG grid → FB page card → nothing.
All calls need SCRAPINGDOG_API_KEY; without it fetch_all() is a no-op.

Pricing (pulled from scrapingdog.com/pricing, July 2026): dedicated social
scrapers bill ~15 credits/request; the Lite plan is $40 / 200k credits
(= $0.0002 per credit). Both are env-tunable.
"""
import os
import re
import time
from urllib.parse import urlparse

import requests

IG_PROFILE_URL = "https://api.scrapingdog.com/instagram/profile"
IG_POSTS_URL = "https://api.scrapingdog.com/instagram/posts"
FB_POSTS_URL = "https://api.scrapingdog.com/facebook/posts"
FB_PROFILE_URL = "https://api.scrapingdog.com/facebook/profile"
TIMEOUT = 90

CREDITS_PER_SOCIAL_REQ = int(os.environ.get("SCRAPINGDOG_SOCIAL_CREDITS", "15"))
USD_PER_CREDIT = float(os.environ.get("SCRAPINGDOG_USD_PER_CREDIT", "0.0002"))

MAX_IG_POSTS = 6
MAX_FB_POSTS = 3

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{2,60}$")
# Path segments that are site plumbing, never a profile handle.
_IG_SKIP = {"p", "reel", "reels", "explore", "stories", "accounts", "share",
            "tv", "direct", "about", "legal"}
_FB_SKIP = {"sharer.php", "sharer", "share.php", "share", "profile.php",
            "pages", "groups", "watch", "hashtag", "dialog", "plugins",
            "login", "people", "story.php", "events", "marketplace"}


def instagram_handle(url):
    """'https://www.instagram.com/mamaearth.in/?hl=en' → 'mamaearth.in'.
    '' when the link is a post/share link rather than a profile."""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    if "instagram" not in (p.hostname or ""):
        return ""
    seg = [s for s in p.path.split("/") if s]
    if not seg or seg[0].lower() in _IG_SKIP:
        return ""
    h = seg[0].strip("@")
    return h if _HANDLE_RE.match(h) else ""


def facebook_handle(url):
    """'https://www.facebook.com/mamaearthindia' → 'mamaearthindia'.
    profile.php / sharer / pages URLs carry no usable username → ''."""
    if not url:
        return ""
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    host = p.hostname or ""
    if "facebook" not in host and "fb.com" not in host:
        return ""
    seg = [s for s in p.path.split("/") if s]
    if not seg or seg[0].lower() in _FB_SKIP:
        return ""
    h = seg[0].strip("@")
    return h if _HANDLE_RE.match(h) else ""


# Set when the API answers "you have reached your account limit" — a quota
# failure is transient (top-up fixes it) and must never be cached as
# "this brand has no socials".
_QUOTA = {"hit": False}


def _get(url, params):
    """GET → parsed JSON dict, or None on any transport/HTTP/shape failure.
    The social tier must never fail a kit build."""
    key = os.environ.get("SCRAPINGDOG_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(url, timeout=TIMEOUT,
                         params=dict(params, api_key=key))
    except requests.RequestException:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if "account limit" in str(body.get("message") or "").lower():
        _QUOTA["hit"] = True
        return None
    return body if r.status_code == 200 else None


def _compact(n):
    """1591735 → '1.6M', 481294 → '481K'. Display-side thousands formatting."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%dK" % (n // 1_000)
    return str(n)


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return ""


def fetch_instagram(handle):
    """→ (normalized dict | None, api_requests_made)."""
    prof = _get(IG_PROFILE_URL, {"username": handle})
    calls = 1
    if not prof or not prof.get("profile_id"):
        return None, calls
    raw = _get(IG_POSTS_URL, {"id": prof["profile_id"]})
    calls += 1
    posts = []
    for p in (raw or {}).get("posts_data") or []:
        if not isinstance(p, dict):
            continue
        img = p.get("thumbnail") or p.get("display_url") or ""
        if not img.startswith("http"):
            continue
        sc = p.get("shortcode") or ""
        vurl = p.get("video_url") or ""
        posts.append({
            "image": img,
            "caption": (p.get("caption") or "")[:220],
            "likes": p.get("likes") or 0,
            "comments": p.get("comment") or 0,
            "is_video": bool(p.get("is_video")),
            "video_url": vurl if vurl.startswith("http") else "",
            "url": "https://www.instagram.com/p/%s/" % sc if sc else "",
            "timestamp": p.get("timestamp") or 0,
        })
        if len(posts) >= MAX_IG_POSTS:
            break
    return {
        "handle": prof.get("username") or handle,
        "full_name": (prof.get("full_name") or "")[:80],
        "followers": prof.get("followers_count") or 0,
        "followers_compact": _compact(prof.get("followers_count")),
        "profile_pic": prof.get("profile_pic_url") or "",
        "bio": (prof.get("bio") or "")[:160],
        "total_posts": (raw or {}).get("total_posts") or 0,
        "posts": posts,
    }, calls


def fetch_facebook(handle):
    """→ (normalized dict | None, api_requests_made).
    Posts endpoint is best-effort (often 400 upstream); the profile call is
    the reliable backbone of the page card."""
    calls = 0
    posts = []
    raw = _get(FB_POSTS_URL, {"username": handle})
    calls += 1
    for p in (raw or {}).get("posts_data") or []:
        if not isinstance(p, dict):
            continue
        text = _first(p, "post_text", "message", "caption", "text", "content")
        img = _first(p, "image", "photo_url", "picture", "thumbnail")
        posts.append({
            "text": str(text)[:280],
            "image": img if str(img).startswith("http") else "",
            "likes": _first(p, "likes", "reactions_count", "reactions") or 0,
            "comments": _first(p, "comments", "comments_count") or 0,
            "url": _first(p, "post_url", "url", "link"),
        })
        if len(posts) >= MAX_FB_POSTS:
            break

    prof = _get(FB_PROFILE_URL, {"username": handle})
    calls += 1
    if not prof or not prof.get("title"):
        if not posts:
            return None, calls
        prof = {}

    info = [s for s in (prof.get("info") or []) if isinstance(s, str)]
    talking = next((s for s in info if "talking about" in s), "")
    about = next(
        (s for s in info
         if len(s) > 30 and "likes" not in s and "talking about" not in s), "")
    url = prof.get("url") or "https://www.facebook.com/%s" % handle
    return {
        "name": prof.get("title") or handle,
        "handle": handle,
        "likes": prof.get("likes") or 0,
        "likes_compact": _compact(prof.get("likes")),
        "talking_about": talking,
        "about": about[:200],
        "url": url.split("?")[0],
        "cover": prof.get("coverPhoto") or "",
        "posts": posts,
    }, calls


_NORM_RE = re.compile(r"[^a-z0-9]")
MIN_PROBED_FOLLOWERS = 1_000     # probed handles below this smell like squatters


def _norm(s):
    return _NORM_RE.sub("", (s or "").lower())


def _handle_candidates(brand_name, domain):
    """Guesses for a brand whose site exposes no social links (SPA storefronts
    like bewakoof.com render them client-side). Domain base first, then the
    '<base>official' variant D2C brands commonly use (bewakoofofficial),
    then the brand name."""
    cands = []
    base = _norm((domain or "").split(".")[0])
    if len(base) >= 3:
        cands += [base, base + "official"]
    b = _norm(brand_name)
    if len(b) >= 3 and b not in cands:
        cands.append(b)
    return cands[:3]


def _is_brand(profile_text, brand_name, domain):
    """A probed handle is only trusted when the profile itself names the
    brand or its domain — 'bewakoof' matching bio 'Bewakoof.com' passes, a
    squatter account does not."""
    hay = _norm(profile_text)
    base = _norm((domain or "").split(".")[0])
    b = _norm(brand_name)
    return (len(base) >= 3 and base in hay) or (len(b) >= 3 and b in hay)


def fetch_all(social_links, brand_name="", domain=""):
    """social_links is kit['social'] ({'instagram': url, 'facebook': url, …}).
    When a link is missing, probe guessed handles and keep them only if the
    returned profile verifiably belongs to the brand. Never raises."""
    out = {"instagram": None, "facebook": None, "requests": 0, "credits": 0,
           "cost_usd": 0.0, "fetched_at": int(time.time())}
    if not os.environ.get("SCRAPINGDOG_API_KEY"):
        out["error"] = "SCRAPINGDOG_API_KEY not set"
        return out
    _QUOTA["hit"] = False
    links = social_links or {}

    ig_h = instagram_handle(links.get("instagram") or "")
    if ig_h:
        ig, n = fetch_instagram(ig_h)
        out["instagram"], out["requests"] = ig, out["requests"] + n
    else:
        for cand in _handle_candidates(brand_name, domain):
            ig, n = fetch_instagram(cand)
            out["requests"] += n
            if ig and ig.get("followers", 0) >= MIN_PROBED_FOLLOWERS \
                    and _is_brand(" ".join((ig.get("handle", ""),
                                            ig.get("full_name", ""),
                                            ig.get("bio", ""))),
                                  brand_name, domain):
                ig["probed"] = True
                out["instagram"] = ig
                break

    fb_h = facebook_handle(links.get("facebook") or "")
    if fb_h:
        fb, n = fetch_facebook(fb_h)
        out["facebook"], out["requests"] = fb, out["requests"] + n
    else:
        for cand in _handle_candidates(brand_name, domain):
            fb, n = fetch_facebook(cand)
            out["requests"] += n
            if fb and fb.get("likes", 0) >= MIN_PROBED_FOLLOWERS \
                    and _is_brand(" ".join((fb.get("name", ""),
                                            fb.get("about", ""))),
                                  brand_name, domain):
                fb["probed"] = True
                out["facebook"] = fb
                break

    out["credits"] = out["requests"] * CREDITS_PER_SOCIAL_REQ
    out["cost_usd"] = round(out["credits"] * USD_PER_CREDIT, 4)
    if _QUOTA["hit"]:
        out["error"] = "scrapingdog quota exhausted — top up credits and re-extract"
    return out


def apply_cost(meta, social):
    """Fold the social-scrape spend into the meta/cost block the studio UI
    already renders. Idempotent per kit: callers only invoke on fresh fetch."""
    if not social or not social.get("requests"):
        return
    tiers = meta.setdefault("tiers", [])
    if "social_scrape" not in tiers:
        tiers.append("social_scrape")
    meta["social_requests"] = social["requests"]
    meta["social_credits"] = social["credits"]
    cost = meta.setdefault("cost", {"per_tier_usd": {}, "total_usd": 0.0})
    per = cost.setdefault("per_tier_usd", {})
    per["social_scrape"] = social["cost_usd"]
    cost["total_usd"] = round((cost.get("total_usd") or 0.0)
                              + social["cost_usd"], 4)
    rates = cost.setdefault("rates", {})
    rates["usd_per_credit"] = USD_PER_CREDIT
    rates["credits_per_social_req"] = CREDITS_PER_SOCIAL_REQ
