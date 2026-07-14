"""Tests for the AI enrichment layer.

The theme: a model is an untrusted remote input. Every test here asks "what
happens when the model returns something wrong, malicious, or overconfident?"
"""
import json

import pytest

import brandai as ai
import brandboost as bb


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def _data(**over):
    base = {
        "headerBg": "rgb(255, 255, 255)", "bodyBg": "rgb(255,255,255)",
        "bodyColor": "rgb(17,17,17)", "ogSiteName": "Zudio",
        "accentCandidates": ["rgb(225, 27, 34)"] * 5,
        "fonts": [{"family": "Poppins", "weight": "400"}],
        "bodyFont": "Poppins, sans-serif",
        "images": [
            {"src": "https://x.com/a.jpg", "w": 1200, "h": 600, "alt": "hero shot"},
            {"src": "https://x.com/b.jpg", "w": 400, "h": 800, "alt": "red dress"},
            {"src": "https://x.com/c.jpg", "w": 400, "h": 800, "alt": "blue shirt"},
        ],
        "logoLum": 240.0, "logoOpaque": 0.4,
    }
    base.update(over)
    return base


def _derived(**over):
    v = bb.derive(_data(), "https://www.zudio.com/")
    v.update(over)
    return v


def _picks():
    return {"hero": "https://x.com/orig-hero.jpg", "prods": [], "showcase": None,
            "ig": [], "usable": 3}


def _merge(ai_out, data=None, v=None, picks=None):
    data = data or _data()
    pool, _ = ai.build_digest(data, "https://www.zudio.com/")
    return ai.merge(v or _derived(), data, picks or _picks(), ai_out, pool)


# ---------------------------------------------------------------------------
# digest: the model must never see a URL it can echo back
# ---------------------------------------------------------------------------
def test_digest_addresses_images_by_index_not_url():
    _, digest = ai.build_digest(_data(), "https://www.zudio.com/")
    blob = json.dumps(digest)
    assert "https://x.com/a.jpg" not in blob
    assert [r["i"] for r in digest["images"]] == [0, 1, 2]


def test_digest_reports_alt_coverage_so_the_model_can_abstain():
    data = _data(images=[{"src": "https://x.com/%d.jpg" % i, "w": 900, "h": 900,
                          "alt": ""} for i in range(5)])
    _, digest = ai.build_digest(data, "https://x.com/")
    assert digest["images_total"] == 5
    assert digest["images_with_alt"] == 0


def test_digest_caps_image_count():
    data = _data(images=[{"src": "https://x.com/%d.jpg" % i, "w": 900, "h": 900,
                          "alt": "a"} for i in range(200)])
    _, digest = ai.build_digest(data, "https://x.com/")
    assert len(digest["images"]) == ai.MAX_IMAGES


def test_digest_collapses_accent_candidates_with_counts():
    data = _data(accentCandidates=["rgb(225, 27, 34)"] * 9 + ["rgb(0, 90, 200)"])
    _, digest = ai.build_digest(data, "https://x.com/")
    assert digest["accent_candidates"][0] == {
        "hex": "#e11b22", "count": 9, "sat": pytest.approx(0.88, abs=0.02),
        "lum": pytest.approx(0.35, abs=0.02),
    }


def test_digest_drops_icon_fonts():
    data = _data(fonts=[{"family": "Font Awesome 5 Free", "weight": "400"},
                        {"family": "Poppins", "weight": "400"}])
    _, digest = ai.build_digest(data, "https://x.com/")
    assert [f["family"] for f in digest["fonts"]] == ["Poppins"]


# ---------------------------------------------------------------------------
# lenient JSON parsing
# ---------------------------------------------------------------------------
def test_fenced_json_is_parsed():
    assert ai._loads_lenient('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_with_leading_prose_is_recovered():
    assert ai._loads_lenient('Here you go:\n{"a": 1}\nHope that helps!') == {"a": 1}


def test_empty_completion_raises_gateway_error():
    with pytest.raises(ai.AIGatewayError):
        ai._loads_lenient("   ")


def test_unparseable_completion_raises_gateway_error():
    with pytest.raises(ai.AIGatewayError):
        ai._loads_lenient("no json here at all")


# ---------------------------------------------------------------------------
# colour: the <style> block is not a place to trust a model
# ---------------------------------------------------------------------------
def test_css_injection_via_brand_colour_is_rejected():
    v, _, applied = _merge({"brand_color": "#fff;} body{background:url(x)",
                            "confidence": {"brand_color": 1.0}})
    assert "brand_color" not in applied
    assert bb.HEX_RE.match(v["brand"])


def test_grey_brand_colour_is_rejected_even_at_full_confidence():
    v0 = _derived()
    v, _, applied = _merge({"brand_color": "#888888",
                            "confidence": {"brand_color": 1.0}})
    assert "brand_color" not in applied
    assert v["brand"] == v0["brand"]


def test_near_white_brand_colour_is_rejected():
    _, _, applied = _merge({"brand_color": "#fefefe",
                            "confidence": {"brand_color": 1.0}})
    assert "brand_color" not in applied


def test_valid_brand_colour_is_applied_and_light_shade_recomputed():
    v, _, applied = _merge({"brand_color": "#e11b22",
                            "confidence": {"brand_color": 0.9}})
    assert "brand_color" in applied
    assert v["brand"] == "#e11b22"
    assert v["light"] == bb._lighten("#e11b22", 0.88)


def test_default_link_blue_cannot_come_back_through_the_model():
    """boult: #0000ee is the browser's unstyled-link colour, on 68 links.
    derive() rejects it (lum 0.11 < 0.12). The AI layer must not readmit it."""
    data = _data(accentCandidates=["rgb(0, 0, 238)"] * 68, ctaBg="rgb(26,26,26)")
    v0 = bb.derive(data, "https://x.com/")
    assert v0["brand"] == "#1a1a1a"          # the CTA fill, not the link blue

    _, digest = ai.build_digest(data, "https://x.com/")
    assert digest["accent_candidates"] == []  # never even offered to the model

    v, _, applied = _merge({"brand_color": "#0000ee",
                            "confidence": {"brand_color": 1.0}}, data=data, v=v0)
    assert "brand_color" not in applied
    assert v["brand"] == "#1a1a1a"


def test_a_colour_seen_four_times_is_not_a_brand_colour():
    """hammer: a stray olive at count=4 was the only eligible hex, so the model
    elected it. A brand colour has to actually recur."""
    data = _data(accentCandidates=["rgb(130, 148, 96)"] * 4)
    _, digest = ai.build_digest(data, "https://x.com/")
    assert digest["accent_candidates"] == []

    v, _, applied = _merge({"brand_color": "#829460",
                            "confidence": {"brand_color": 1.0}}, data=data)
    assert "brand_color" not in applied


def test_colour_never_observed_on_the_page_is_rejected():
    _, _, applied = _merge({"brand_color": "#ff00ff",
                            "confidence": {"brand_color": 1.0}})
    assert "brand_color" not in applied


def test_cta_fill_is_an_acceptable_brand_colour():
    data = _data(accentCandidates=[], ctaBg="rgb(225, 27, 34)")
    v, _, applied = _merge({"brand_color": "#e11b22",
                            "confidence": {"brand_color": 0.9}}, data=data,
                           v=bb.derive(data, "https://x.com/"))
    assert "brand_color" in applied
    assert v["brand"] == "#e11b22"


def test_digest_and_derive_agree_on_eligibility():
    """The invariant the two bugs above both violated."""
    for hx in ("#0000ee", "#829460", "#e11b22", "#888888", "#fefefe", "#ffdb4c"):
        rgb = "rgb(%d, %d, %d)" % bb._rgb_triplet(hx)
        offered = ai._accent_digest([rgb] * 40)
        picked = bb._pick_accent([rgb] * 40)
        assert bool(offered) == bool(picked), hx


def test_low_confidence_colour_is_ignored():
    _, _, applied = _merge({"brand_color": "#e11b22",
                            "confidence": {"brand_color": 0.49}})
    assert "brand_color" not in applied


def test_missing_confidence_entry_is_treated_as_zero():
    _, _, applied = _merge({"brand_color": "#e11b22", "confidence": {}})
    assert "brand_color" not in applied


def test_confidence_true_is_not_a_number():
    """`True >= 0.5` is True in Python. It must not count as a score."""
    _, _, applied = _merge({"brand_color": "#e11b22",
                            "confidence": {"brand_color": True}})
    assert "brand_color" not in applied


# ---------------------------------------------------------------------------
# fonts: a family the page never loaded renders as the system stack
# ---------------------------------------------------------------------------
def test_font_not_present_on_the_page_is_rejected():
    v0 = _derived()
    v, _, applied = _merge({"body_font": "Helvetica Neue",
                            "confidence": {"body_font": 1.0}})
    assert "body_font" not in applied
    assert v["body_font"] == v0["body_font"]


def test_font_present_on_the_page_is_applied():
    v, _, applied = _merge({"body_font": "Poppins", "confidence": {"body_font": 0.8}})
    assert "body_font" in applied
    assert v["body_font"].startswith("'Poppins'")


def test_font_name_with_css_metacharacters_is_stripped_then_rejected():
    _, _, applied = _merge({"body_font": "Poppins';} body{x:y",
                            "confidence": {"body_font": 1.0}})
    assert "body_font" not in applied


# ---------------------------------------------------------------------------
# announcement: the cookie-notice bug this whole layer exists to fix
# ---------------------------------------------------------------------------
def test_model_rejecting_the_strip_restores_the_default_announcement():
    data = _data(annBg="rgb(200, 30, 30)", annColor="rgb(255,255,255)",
                 annText="We use cookies to improve your experience")
    v = bb.derive(data, "https://x.com/")
    v, _, applied = _merge({"announcement": {"is_real_promo": False},
                            "confidence": {"announcement": 0.9}}, data=data, v=v)
    assert (v["ann_copy"], v["ann_bg"], v["ann_text_col"]) == bb.DEFAULT_ANN
    assert "announcement(rejected)" in applied


def test_real_promo_keeps_measured_colours_and_forces_contrast():
    data = _data(annBg="rgb(0, 0, 0)", annColor="rgb(0, 0, 0)",
                 annText="Flat 30% off sitewide")
    v = bb.derive(data, "https://x.com/")
    v, _, _ = _merge({"announcement": {"is_real_promo": True,
                                       "copy": "Flat 30% off sitewide"},
                      "confidence": {"announcement": 0.9}}, data=data, v=v)
    assert v["ann_bg"] == "#000000"
    assert v["ann_text_col"] == "#ffffff"
    assert bb._contrasts(v["ann_text_col"], v["ann_bg"])


def test_promo_without_a_measured_background_is_not_applied():
    """A transparent strip has no colour. Painting model copy on derive()'s
    default background is how you get black text on black."""
    data = _data(annBg="rgba(0,0,0,0)", annText="Free shipping")
    v = bb.derive(data, "https://x.com/")
    v, _, applied = _merge({"announcement": {"is_real_promo": True,
                                             "copy": "Free shipping over 999"},
                            "confidence": {"announcement": 1.0}}, data=data, v=v)
    assert "announcement" not in applied
    assert v["ann_copy"] == bb.DEFAULT_ANN[0]


# ---------------------------------------------------------------------------
# logo: measurement beats inference
# ---------------------------------------------------------------------------
def test_logo_treatment_ignored_when_pixels_were_measured():
    v0 = _derived()
    v, _, applied = _merge({"logo_treatment": "invert"})
    assert "logo_treatment" not in applied
    assert v["logo_filter"] == v0["logo_filter"]


def test_logo_treatment_used_when_canvas_probe_was_blocked():
    data = _data(logoLum=None, logoOpaque=None)
    v = bb.derive(data, "https://x.com/")
    v, _, applied = _merge({"logo_treatment": "darken"}, data=data, v=v)
    assert "logo_treatment" in applied
    assert v["logo_filter"] == "filter:brightness(0);"


def test_unknown_logo_treatment_is_ignored():
    data = _data(logoLum=None)
    v = bb.derive(data, "https://x.com/")
    _, _, applied = _merge({"logo_treatment": "rm -rf"}, data=data, v=v)
    assert "logo_treatment" not in applied


# ---------------------------------------------------------------------------
# images: index-only, and slots move as a unit
# ---------------------------------------------------------------------------
def test_out_of_range_index_is_ignored():
    _, picks, applied = _merge({"hero": {"image_index": 99},
                                "confidence": {"images": 0.9}})
    assert "hero_image" not in applied
    assert picks["hero"] == "https://x.com/orig-hero.jpg"


def test_a_url_returned_instead_of_an_index_is_ignored():
    """The model cannot smuggle in a hallucinated URL by ignoring the schema."""
    _, picks, applied = _merge({"hero": {"image_index": "https://evil.com/x.jpg"},
                                "confidence": {"images": 0.9}})
    assert "hero_image" not in applied
    assert picks["hero"] == "https://x.com/orig-hero.jpg"


def test_boolean_index_is_not_an_index():
    _, _, applied = _merge({"hero": {"image_index": True},
                            "confidence": {"images": 0.9}})
    assert "hero_image" not in applied


def test_valid_hero_index_maps_back_to_the_scraped_url():
    _, picks, applied = _merge({"hero": {"image_index": 0},
                                "confidence": {"images": 0.9}})
    assert "hero_image" in applied
    assert picks["hero"].startswith("https://x.com/a.jpg")


def test_low_image_confidence_abstains_and_keeps_the_heuristic():
    """The zudio case: every alt empty, model says so, geometry wins."""
    _, picks, applied = _merge({"hero": {"image_index": 0},
                                "products": [{"image_index": 1, "name": "X",
                                              "price": "Rs 99"}],
                                "confidence": {"images": 0.3}})
    assert applied == []
    assert picks["hero"] == "https://x.com/orig-hero.jpg"


def test_product_names_only_ship_when_product_images_did():
    """Fewer than two usable indices: no names either, or we label a random
    heuristic image 'Rose Water Toner'."""
    v, picks, applied = _merge({
        "products": [{"image_index": 1, "name": "Red Dress", "price": "Rs 799"},
                     {"image_index": 77, "name": "Ghost", "price": "Rs 1"}],
        "confidence": {"images": 0.9}})
    assert "product_images" not in applied
    assert "prod_names" not in v


def test_two_valid_products_ship_names_and_prices():
    v, picks, applied = _merge({
        "products": [{"image_index": 1, "name": "Red Dress", "price": "Rs 799"},
                     {"image_index": 2, "name": "Blue Shirt", "price": "Rs 599"}],
        "confidence": {"images": 0.9}})
    assert "product_images" in applied
    assert v["prod_names"] == ["Red Dress", "Blue Shirt"]
    assert len(picks["prods"]) == 2


def test_duplicate_instagram_indices_are_deduped():
    data = _data(images=[{"src": "https://x.com/%d.jpg" % i, "w": 500, "h": 500,
                          "alt": "a"} for i in range(6)])
    _, picks, applied = _merge({"instagram_indices": [0, 0, 1, 1, 2],
                                "confidence": {"images": 0.9}}, data=data)
    assert "instagram_images" in applied
    assert len(picks["ig"]) == len(set(picks["ig"])) == 3


# ---------------------------------------------------------------------------
# copy caps
# ---------------------------------------------------------------------------
def test_overlong_hero_headline_is_truncated_not_dropped():
    v, _, _ = _merge({"hero": {"headline_l1": "A" * 60, "headline_l2": "B" * 60},
                      "confidence": {"hero": 0.9}})
    assert len(v["hero_l1"]) == ai.CAP_HERO_LINE


def test_overlong_prose_is_cut_at_a_word_boundary():
    """'...built for everyday li' shipped to a real page before this existed."""
    v, _, _ = _merge({"ad": {"line1": "Plug In", "line2": "Your Beat",
                             "sub": "Wireless audio, wearables and speakers "
                                    "built for everyday listening"},
                      "confidence": {"ad": 0.9}})
    assert len(v["ad_sub"]) <= ai.CAP_AD_SUB
    assert not v["ad_sub"].endswith(" ")
    assert v["ad_sub"].split()[-1] in "Wireless audio, wearables and speakers " \
                                      "built for everyday listening".split()


def test_a_single_unbreakable_word_still_gets_hard_cut():
    assert len(ai._fit("A" * 200, 10)) == 10


def test_fit_leaves_short_copy_alone():
    assert ai._fit("  Hear   More ", 20) == "Hear More"


def test_brand_name_drives_the_order_prefix():
    v, _, applied = _merge({"brand_name": "Fire-Boltt",
                            "confidence": {"brand_name": 0.9}})
    assert "brand_name" in applied
    assert v["brand_name"] == "Fire-Boltt"
    assert v["prefix"] == "FIR"


def test_control_characters_are_stripped_from_copy():
    v, _, _ = _merge({"brand_name": "Zud\x00io\x07", "confidence": {"brand_name": 0.9}})
    assert v["brand_name"] == "Zudio"


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------
def test_gateway_failure_returns_heuristics_untouched(monkeypatch):
    def boom(*_, **__):
        raise ai.AIGatewayError("connection reset")
    monkeypatch.setattr(ai, "call_gateway", boom)

    v0, picks0 = _derived(), _picks()
    v, picks, report = ai.enrich(_data(), "https://x.com/", v0, picks0)
    assert report["ok"] is False
    assert "connection reset" in report["error"]
    assert v == v0 and picks == picks0


def test_missing_api_key_raises_rather_than_degrading(monkeypatch):
    """An outage should degrade. A misconfiguration should be loud."""
    monkeypatch.delenv("SR_AI_GATEWAY_KEY", raising=False)
    with pytest.raises(ai.AIConfigError):
        ai.call_gateway("sys", "user")


def test_non_object_model_output_is_rejected(monkeypatch):
    monkeypatch.setattr(ai, "call_gateway", lambda *_, **__: (["not", "a", "dict"], {}))
    v0 = _derived()
    v, _, report = ai.enrich(_data(), "https://x.com/", v0, _picks())
    assert report["ok"] is False
    assert v == v0


def test_enrich_never_mutates_the_caller_dicts(monkeypatch):
    monkeypatch.setattr(ai, "call_gateway",
                        lambda *_, **__: ({"brand_color": "#e11b22",
                                           "confidence": {"brand_color": 0.9}}, {}))
    v0, picks0 = _derived(), _picks()
    before = dict(v0)
    ai.enrich(_data(), "https://x.com/", v0, picks0)
    assert v0 == before
