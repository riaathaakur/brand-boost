"""Regression tests for the extraction/derivation bugs found against zudio.com."""
import brandboost as bb


# --- _rgb_to_hex: transparent is absent, not black -------------------------
def test_fully_transparent_is_absent_not_black():
    assert bb._rgb_to_hex("rgba(0, 0, 0, 0)") == ""


def test_opaque_black_still_parses():
    assert bb._rgb_to_hex("rgb(0, 0, 0)") == "#000000"


def test_modern_slash_syntax_alpha_respected():
    assert bb._rgb_to_hex("rgb(255 232 0 / 20%)") == ""
    assert bb._rgb_to_hex("rgb(255 232 0 / 90%)") == "#ffe800"


def test_hex_passthrough_and_garbage():
    assert bb._rgb_to_hex("#AABBCC") == "#aabbcc"
    assert bb._rgb_to_hex("not-a-color") == ""


# --- _pick_accent: repetition beats a lone saturated outlier ---------------
def test_repeated_colour_beats_saturated_outlier():
    candidates = ["rgb(85, 123, 151)"] * 6 + ["rgb(56, 96, 190)"]
    assert bb._pick_accent(candidates) == "#557b97"


def test_no_usable_candidates_returns_empty():
    assert bb._pick_accent(["rgb(250, 250, 250)", "rgb(2, 2, 2)"]) == ""


# --- announcement bar ------------------------------------------------------
def _data(**over):
    base = {"headerBg": "rgb(0, 0, 0)", "bodyBg": "rgb(255,255,255)",
            "bodyColor": "rgb(61,66,70)", "ogSiteName": "Zudio",
            "accentCandidates": [], "fonts": []}
    base.update(over)
    return base


def test_cookie_notice_never_becomes_the_announcement_bar():
    v = bb.derive(_data(annBg="rgba(0, 0, 0, 0)", annColor="rgb(0, 0, 0)",
                        annText="Cookie Policy"), "https://www.zudio.com/")
    assert v["ann_copy"] == bb.DEFAULT_ANN[0]
    assert (v["ann_bg"], v["ann_text_col"]) == bb.DEFAULT_ANN[1:]


def test_announcement_text_always_contrasts_its_background():
    v = bb.derive(_data(annBg="rgb(0, 0, 0)", annColor="rgb(0, 0, 0)",
                        annText="Flat 30% OFF sitewide"), "https://x.com/")
    assert v["ann_bg"] == "#000000"
    assert v["ann_text_col"] == "#ffffff"
    assert bb._contrasts(v["ann_text_col"], v["ann_bg"])


def test_real_promo_bar_is_kept():
    v = bb.derive(_data(annBg="rgb(200, 30, 30)", annColor="rgb(255,255,255)",
                        annText="Free shipping over 999"), "https://x.com/")
    assert v["ann_copy"] == "Free shipping over 999"
    assert v["ann_bg"] == "#c81e1e"


# --- brand fallback --------------------------------------------------------
def test_dark_header_supplies_brand_when_no_accent_exists():
    v = bb.derive(_data(ctaBg="rgba(0, 0, 0, 0)"), "https://www.zudio.com/")
    assert v["brand"] == "#000000"


# --- CSS/HTML injection ----------------------------------------------------
def test_font_family_cannot_break_out_of_css():
    v = bb.derive(_data(fonts=[{"family": "Evil'}body{display:none}", "weight": "700"}]),
                  "https://x.com/")
    assert "display:none" not in v["font_face_css"]
    assert "Evilbodydisplaynone" in v["font_face_css"]
    assert v["font_face_css"].count("{") == v["font_face_css"].count("}") == 1


def test_sanitize_svg_drops_active_content():
    assert bb._sanitize_svg('<svg><script>alert(1)</script><path d="M0"/></svg>') \
        == '<svg><path d="M0"/></svg>'
    assert bb._sanitize_svg('<svg onload="alert(1)"><path/></svg>') == '<svg><path/></svg>'
    assert bb._sanitize_svg('<svg><a href="javascript:alert(1)">x</a></svg>') \
        == '<svg><a >x</a></svg>'


def test_safe_url_rejects_javascript_scheme():
    assert bb._safe_url("javascript:alert(1)") == ""
    assert bb._safe_url("https://cdn.example/a.png") == "https://cdn.example/a.png"


# --- template --------------------------------------------------------------
def _picks():
    return {"hero": None, "prods": [], "showcase": None, "ig": [], "usable": 0}


def test_promo_code_placeholder_is_substituted():
    v = bb.derive(_data(), "https://www.zudio.com/")
    out = bb.build_tracking_html(v, {}, _picks(), "zudio")
    assert "$prefix25" not in out
    assert "Use code ZUD25" in out


def test_remote_brand_name_is_html_escaped():
    v = bb.derive(_data(ogSiteName='"><script>alert(1)</script>'), "https://x.com/")
    out = bb.build_tracking_html(v, {}, _picks(), "zudio")
    assert "<script>alert(1)</script>" not in out


def test_malicious_logo_svg_is_not_emitted():
    v = bb.derive(_data(), "https://x.com/")
    out = bb.build_tracking_html(v, {"logoSvg": '<svg><script>alert(1)</script></svg>'},
                                 _picks(), "zudio")
    assert "alert(1)" not in out


# --- icon fonts never become body text ------------------------------------
def test_icon_font_is_not_chosen_as_body_font():
    v = bb.derive(_data(fonts=[{"family": "Font Awesome 6 Brands", "weight": "400"},
                               {"family": "icomoon", "weight": "400"},
                               {"family": "Inter", "weight": "400"}]),
                  "https://x.com/")
    assert v["body_font"].startswith("'Inter'")
    assert "Awesome" not in v["font_face_css"] and "icomoon" not in v["font_face_css"]


def test_all_icon_fonts_falls_back_to_system_stack():
    v = bb.derive(_data(fonts=[{"family": "swiper-icons", "weight": "400"},
                               {"family": "JudgemeStar", "weight": "400"}]),
                  "https://x.com/")
    assert v["body_font"].startswith("system-ui")
    assert v["font_face_css"] == ""


def test_computed_body_font_wins_over_font_face_order():
    # icomoon/Cookie declared first, but <body> computes Open Sans.
    v = bb.derive(_data(bodyFont='"Open Sans", sans-serif',
                        fonts=[{"family": "icomoon", "weight": "400"},
                               {"family": "Cookie", "weight": "400"},
                               {"family": "Open Sans", "weight": "400"}]),
                  "https://mamaearth.in/")
    assert v["body_font"].startswith("'Open Sans'")


def test_first_font_family_skips_generics_and_icons():
    assert bb._first_font_family('"Font Awesome 6 Brands", "Open Sans", sans-serif') == "Open Sans"
    assert bb._first_font_family("sans-serif") == ""
    assert bb._first_font_family("SF-Pro-Display-Regular") == "SF-Pro-Display-Regular"


# --- hyphenated brand names are not truncated -----------------------------
def test_in_word_hyphen_is_kept_in_brand_name():
    v = bb.derive(_data(ogSiteName="Fire-Boltt"), "https://www.fireboltt.com/")
    assert v["brand_name"] == "Fire-Boltt"


def test_spaced_dash_tagline_is_stripped():
    v = bb.derive(_data(ogSiteName="Zudio - Fashion Store"), "https://x.com/")
    assert v["brand_name"] == "Zudio"


def test_pipe_tagline_is_stripped():
    v = bb.derive(_data(ogSiteName="Mamaearth | Official Website"), "https://x.com/")
    assert v["brand_name"] == "Mamaearth"


# --- slideshow controls never become the announcement bar -----------------
def test_slideshow_control_never_becomes_announcement():
    v = bb.derive(_data(annBg="rgb(255,255,255)", annColor="rgb(0,0,0)",
                        annText="Pause slideshow"), "https://x.com/")
    assert v["ann_copy"] == bb.DEFAULT_ANN[0]


# --- white logo on a light header gets repainted so it stays visible ------
def test_white_logo_on_light_header_is_darkened():
    v = bb.derive(_data(headerBg="rgb(255,255,255)", logoAlt="Hammer White logo"),
                  "https://x.com/")
    v_html = bb.build_tracking_html(v, {"logoSrc": "https://x/white_logo.png",
                                        "logoAlt": "Hammer White logo"}, _picks(), "hammer")
    assert v["logo_filter"] == "filter:brightness(0);"
    assert "brightness(0)" in v_html


def test_measured_white_logo_lum_darkens_on_light_header():
    # No filename hint, but pixels are near-white → must still be darkened.
    v = bb.derive(_data(headerBg="rgb(255,255,255)", logoLum=255.0), "https://x.com/")
    assert v["logo_filter"] == "filter:brightness(0);"


def test_measured_dark_logo_lum_untouched_on_light_header():
    v = bb.derive(_data(headerBg="rgb(255,255,255)", logoLum=32.0,
                        logoAlt="Brand white logo"), "https://x.com/")
    # Measured luminance (dark) overrides the misleading 'white' in the alt text.
    assert v["logo_filter"] == ""


def test_light_transparent_wordmark_is_darkened():
    v = bb.derive(_data(headerBg="rgb(255,255,255)", logoLum=255.0, logoOpaque=0.5),
                  "https://x.com/")
    assert v["logo_filter"] == "filter:brightness(0);"


def test_light_but_solid_block_logo_is_not_blobbed():
    # 98% opaque near-white square → darkening would make a black blob; skip it.
    v = bb.derive(_data(headerBg="rgb(255,255,255)", logoLum=222.0, logoOpaque=0.98),
                  "https://x.com/")
    assert v["logo_filter"] == ""


def test_dark_header_still_inverts_and_color_logo_untouched():
    dark = bb.derive(_data(headerBg="rgb(0,0,0)", logoAlt="Brand white logo"), "https://x.com/")
    assert dark["logo_filter"] == "filter:brightness(0) invert(1);"
    plain = bb.derive(_data(headerBg="rgb(255,255,255)", logoAlt="Brand color logo"),
                      "https://x.com/")
    assert plain["logo_filter"] == ""
