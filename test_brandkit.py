"""Regression tests for the browserless extractor (brandkit.py).

Bug: JS-rendered stores (Next/Nuxt/SPA themes) came back with empty image
slots — their images live in embedded JSON, not <img> tags — and pages for
image-poor sites rendered grey placeholder boxes instead of hiding sections.
"""
import brandboost as bb
import brandkit


# ---------------------------------------------------------------------------
# mine_embedded_images
# ---------------------------------------------------------------------------
def test_mines_escaped_urls_from_script_json():
    """__NEXT_DATA__ escapes slashes; the miner must unescape them."""
    html = ('<script id="__NEXT_DATA__">{"img":'
            '"https:\\/\\/cdn.x.in\\/p\\/shampoo_600x600.jpg"}</script>')
    out = brandkit.mine_embedded_images(html, "https://x.in/", set())
    assert out == ["https://cdn.x.in/p/shampoo_600x600.jpg"]


def test_mines_json_ld_image_fields_first():
    html = ('<script type="application/ld+json">'
            '{"@type":"Product","image":["https://cdn.x.in/a.jpg"]}</script>'
            '<script>var x="https://cdn.x.in/b.png"</script>')
    out = brandkit.mine_embedded_images(html, "https://x.in/", set())
    assert out[0] == "https://cdn.x.in/a.jpg"     # JSON-LD outranks the sweep
    assert "https://cdn.x.in/b.png" in out


def test_junk_and_seen_urls_are_dropped():
    html = ('<script>["https://x.in/favicon.png","https://x.in/logo.png",'
            '"https://x.in/sprite.png","https://x.in/product.jpg",'
            '"https://x.in/already.jpg"]</script>')
    out = brandkit.mine_embedded_images(html, "https://x.in/",
                                        {"https://x.in/already.jpg"})
    assert out == ["https://x.in/product.jpg"]


# ---------------------------------------------------------------------------
# _url_dims — free dimensions from CDN filenames
# ---------------------------------------------------------------------------
def test_url_dims_reads_bare_wxh_filename():
    assert brandkit._url_dims("https://c.in/830x360_a196c6.jpg") == (830, 360)


def test_url_dims_ignores_implausible_sizes():
    """Version hashes like 20260101x2 must not be read as dimensions."""
    assert brandkit._url_dims("https://c.in/p_20260101x2.jpg") == (0, 0)


# ---------------------------------------------------------------------------
# imageless sections are dropped, not grey-boxed
# ---------------------------------------------------------------------------
def _v():
    return dict(domain="x.in", url="https://x.in/", brand="#e11b22",
                light="#fbe6e7", header_bg="#ffffff", header_icon="#111",
                header_border="1px solid #e8e8e8", logo_filter="",
                header_dark=False, cta_radius="4px", body_bg="#ffffff",
                body_text="#111111", body_font="sans-serif", font_face_css="",
                brand_name="X", ann_bg="#111111", ann_text_col="#ffffff",
                ann_copy="Free delivery on orders above ₹499", prefix="XXX")


def test_kit_products_empty_when_no_images():
    picks = {"prods": [None, None], "hero": None, "showcase": None, "ig": []}
    kit = brandkit.build_kit(_v(), {}, picks, "x", "https://x.in/", {})
    assert kit["products"] == []
    assert kit["showcase_image"] == ""
    assert kit["instagram"]["images"] == []


def test_page_hides_sections_without_images():
    picks = {"prods": [], "hero": None, "showcase": None, "ig": []}
    page = bb.build_tracking_html(_v(), {}, picks, "x")
    assert "You May Also Like" not in page
    assert '<div class="ig-grid">' not in page
    assert '<div class="show">' not in page
    assert "$" not in page                      # no unfilled placeholders


def test_page_keeps_sections_with_images():
    picks = {"prods": ["https://x.in/a.jpg", "https://x.in/b.jpg"],
             "hero": "https://x.in/h.jpg", "showcase": "https://x.in/s.jpg",
             "ig": ["https://x.in/1.jpg", "https://x.in/2.jpg",
                    "https://x.in/3.jpg"]}
    page = bb.build_tracking_html(_v(), {}, picks, "x")
    assert "You May Also Like" in page
    assert '<div class="ig-grid">' in page
    assert '<div class="show">' in page


# ---------------------------------------------------------------------------
# _bump_quality — empty width param must be filled, not left malformed
# ---------------------------------------------------------------------------
def test_bump_quality_fills_empty_width_value():
    u = bb._bump_quality("https://c.in/x.gif?width=&quality=50", 800)
    assert u == "https://c.in/x.gif?width=800&quality=50"


def test_bump_quality_still_replaces_numeric_width():
    u = bb._bump_quality("https://c.in/x.jpg?width=120", 800)
    assert u == "https://c.in/x.jpg?width=800"


# ---------------------------------------------------------------------------
# Tier 0b — proxy fallback decisions (no network in these tests)
# ---------------------------------------------------------------------------
def test_extraction_poor_flags_thin_parses():
    assert brandkit._extraction_poor({"images": [], "logoSrc": "",
                                      "accentCandidates": []})
    assert brandkit._extraction_poor({"images": [{}] * 20, "logoSrc": "",
                                      "logoSvg": "", "accentCandidates": ["#e11b22"]})
    assert not brandkit._extraction_poor({"images": [{}] * 8,
                                          "logoSrc": "https://x.in/l.png",
                                          "accentCandidates": ["#e11b22"]})


def test_richer_parse_wins():
    thin = {"images": [{}] * 2, "logoSrc": "", "fonts": [],
            "accentCandidates": [], "headerBg": ""}
    rich = {"images": [{}] * 12, "logoSrc": "https://x.in/l.png",
            "fonts": [{}], "accentCandidates": ["#e11b22"], "headerBg": "#fff"}
    assert brandkit._richness(rich) > brandkit._richness(thin)


def test_proxy_disabled_without_key(monkeypatch):
    monkeypatch.delenv("SCRAPINGDOG_API_KEY", raising=False)
    assert brandkit.fetch_via_proxy("https://x.in/") == ""


# ---------------------------------------------------------------------------
# brand name — domain-matching title span (The Souled Store bug)
# ---------------------------------------------------------------------------
def test_name_from_title_finds_brand_span():
    t = "Online Shopping for Men & Women Clothing, Accessories at The Souled Store"
    assert bb._name_from_title(t, "thesouledstore.com") == "The Souled Store"


def test_name_from_title_no_match_returns_empty():
    assert bb._name_from_title("Best Deals Online", "thesouledstore.com") == ""


def test_derive_prefers_domain_matched_name():
    data = {"ogSiteName": "Online Shopping at The Souled Store",
            "accentCandidates": [], "fonts": []}
    v = bb.derive(data, "https://www.thesouledstore.com/")
    assert v["brand_name"] == "The Souled Store"


# ---------------------------------------------------------------------------
# library/framework colours must never be var-boosted (Swiper blue bug)
# ---------------------------------------------------------------------------
def test_swiper_theme_var_is_not_brand_evidence():
    data = {"accentCandidates": [], "ctaBg": "", "ctaColor": "",
            "ctaRadius": "", "headerBg": "", "bodyBg": "", "bodyColor": "",
            "bodyFont": ""}
    brandkit.mine_colors({"swiper-theme-color": "#007aff",
                          "brand-primary": "#e11b22"}, [], data)
    assert "#007aff" not in data["accentCandidates"]
    assert "#e11b22" in data["accentCandidates"]


def test_framework_hex_not_boosted_even_from_brand_var():
    data = {"accentCandidates": [], "ctaBg": "", "ctaColor": "",
            "ctaRadius": "", "headerBg": "", "bodyBg": "", "bodyColor": "",
            "bodyFont": ""}
    brandkit.mine_colors({"color-primary": "#007bff"}, [], data)
    assert data["accentCandidates"] == []
