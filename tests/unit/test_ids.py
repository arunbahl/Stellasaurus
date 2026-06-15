from stellasaurus.common.ids import normalize_text, slugify, terms_fingerprint


def test_fingerprint_stable_under_key_order_and_whitespace():
    a = terms_fingerprint({"title": "CPI  YoY > 3.0%", "source": "BLS"})
    b = terms_fingerprint({"source": "BLS", "title": "CPI YoY > 3.0%"})
    assert a == b


def test_fingerprint_sensitive_to_threshold_change():
    a = terms_fingerprint({"proposition": "CPI YoY > 3.0%"})
    b = terms_fingerprint({"proposition": "CPI YoY > 3%"})
    assert a != b


def test_fingerprint_sensitive_to_settlement_source():
    base = {"title": "Temp > 75F", "settlement_source": "Station A"}
    changed = {"title": "Temp > 75F", "settlement_source": "Station B"}
    assert terms_fingerprint(base) != terms_fingerprint(changed)


def test_fingerprint_ignores_irrelevant_case():
    # casefold means case-only differences in text don't change the fingerprint
    assert terms_fingerprint({"t": "Lakers Win"}) == terms_fingerprint({"t": "lakers win"})


def test_normalize_text_collapses_whitespace():
    assert normalize_text("  a\t b\n c ") == "a b c"


def test_slugify():
    assert slugify("Lakers Win Game 1!") == "lakers-win-game-1"
