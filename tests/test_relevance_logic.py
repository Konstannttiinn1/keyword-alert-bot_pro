from filters.relevance import normalize_text
from bot_app import decision_from_score


def test_decision_thresholds():
    assert decision_from_score(0.9, 0.45, 0.15) == "ALERT"
    assert decision_from_score(0.1, 0.45, 0.15) == "DROP"
    assert decision_from_score(0.3, 0.45, 0.15) == "UNCERTAIN"


def test_normalize_text_replaces_tokens():
    text = "VPN 123   https://example.com"
    normalized = normalize_text(text)
    assert normalized == "vpn <NUM> <URL>"
