"""Tests for socialkit handle parsing / cost folding and the server's
customize-payload validation. Pure functions only — no network."""
import socialkit as sk
import server


# ---------------------------------------------------------------- handles
def test_instagram_handle_profile_url():
    assert sk.instagram_handle("https://www.instagram.com/mamaearth.in/?hl=en") \
        == "mamaearth.in"


def test_instagram_handle_rejects_post_and_foreign_urls():
    assert sk.instagram_handle("https://www.instagram.com/p/DIa38U7/") == ""
    assert sk.instagram_handle("https://example.com/mamaearth.in") == ""
    assert sk.instagram_handle("") == ""


def test_facebook_handle_page_url():
    assert sk.facebook_handle("https://www.facebook.com/mamaearthindia") \
        == "mamaearthindia"


def test_facebook_handle_rejects_plumbing_urls():
    assert sk.facebook_handle(
        "https://www.facebook.com/sharer.php?u=https://x.com") == ""
    assert sk.facebook_handle(
        "https://www.facebook.com/profile.php?id=1000") == ""


def test_compact_formatting():
    assert sk._compact(1591735) == "1.6M"
    assert sk._compact(481294) == "481K"
    assert sk._compact(42) == "42"
    assert sk._compact(None) == ""


# ---------------------------------------------------------------- cost fold
def test_apply_cost_folds_social_spend_into_meta():
    meta = {"tiers": ["static"],
            "cost": {"per_tier_usd": {"static": 0.0}, "total_usd": 0.05}}
    social = {"requests": 4, "credits": 60, "cost_usd": 0.012}
    sk.apply_cost(meta, social)
    assert "social_scrape" in meta["tiers"]
    assert meta["cost"]["per_tier_usd"]["social_scrape"] == 0.012
    assert meta["cost"]["total_usd"] == 0.062
    assert meta["social_credits"] == 60


def test_apply_cost_noop_without_requests():
    meta = {"tiers": ["static"]}
    sk.apply_cost(meta, {"requests": 0, "credits": 0, "cost_usd": 0.0})
    assert meta["tiers"] == ["static"]


# ---------------------------------------------------------------- customize
def test_validate_custom_accepts_good_payload():
    clean, err = server._validate_custom({
        "colors": {"brand": "#E91E63", "nope": "#000000", "body_bg": "red"},
        "sections": [{"id": "ann"}, {"id": "banner:x",
                                     "title": "T" * 100, "text": "x"}],
        "logo_data": "data:image/png;base64," + "A" * 100,
    })
    assert err == ""
    assert clean["colors"] == {"brand": "#e91e63"}          # bad keys/vals dropped
    assert clean["sections"][1]["title"] == "T" * 60        # clamped
    assert clean["logo_data"].startswith("data:image/png")


def test_validate_custom_rejects_unknown_section_and_svg_logo():
    _, err = server._validate_custom({"sections": [{"id": "evil"}]})
    assert "unknown section" in err
    _, err = server._validate_custom(
        {"logo_data": "data:image/svg+xml;base64," + "A" * 100})
    assert "logo" in err


def test_validate_custom_empty_means_reset():
    assert server._validate_custom(None) == (None, "")
    assert server._validate_custom({}) == (None, "")
