"""
Tests for bot/news_signal.py — the regulatory/legislative news heuristic.
No network: takes a plain headline list.

Run directly (`python tests/test_news_signal.py`) or under pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.news_signal import regulatory_news_signal


def _h(title: str) -> dict:
    return {"title": title, "link": "https://example.com"}


def test_no_headlines_is_neutral():
    result = regulatory_news_signal(None)
    assert result.signal == "NEUTRAL"
    assert result.relevant_headlines == []
    result_empty = regulatory_news_signal([])
    assert result_empty.signal == "NEUTRAL"


def test_irrelevant_headlines_are_ignored():
    headlines = [_h("Bitcoin price eyes $66K as stocks rise"), _h("Kraken acquires wallet business")]
    result = regulatory_news_signal(headlines)
    assert result.signal == "NEUTRAL"
    assert result.relevant_headlines == []


def test_bullish_verb_on_relevant_topic_is_buy():
    headlines = [_h("SEC approves spot Bitcoin ETF")]
    result = regulatory_news_signal(headlines)
    assert result.signal == "BUY"
    assert result.bullish_count == 1
    assert result.relevant_headlines[0]["tilt"] == "bullish"


def test_bearish_verb_on_relevant_topic_is_sell():
    headlines = [_h("SEC sues major exchange over unregistered securities")]
    result = regulatory_news_signal(headlines)
    assert result.signal == "SELL"
    assert result.bearish_count == 1


def test_topic_mention_with_no_directional_verb_is_neutral_not_guessed():
    # A real headline found while building this: relevant topic (CLARITY
    # Act), no bullish OR bearish verb from the curated list -- must not be
    # forced into a direction.
    headlines = [_h("Senators discuss the CLARITY Act framework")]
    result = regulatory_news_signal(headlines)
    assert result.signal == "NEUTRAL"
    assert result.relevant_headlines[0]["tilt"] == "neutral"


def test_real_example_ny_ag_warns_clarity_act_classifies_bearish_tilted():
    # The actual top CoinTelegraph headline seen while building this. A
    # naive "mentions CLARITY Act -> bullish" rule would get this backwards
    # -- it's a warning, not a positive development. "warns" is a curated
    # bearish-tilted verb, so this must classify as bearish-tilted.
    headlines = [_h("New York AG warns CLARITY Act could weaken state crypto enforcement")]
    result = regulatory_news_signal(headlines)
    assert result.relevant_headlines[0]["tilt"] == "bearish"
    assert result.signal == "SELL"


def test_mixed_verbs_in_one_headline_is_neutral_not_guessed():
    headlines = [_h("Congress bill passed then blocked by committee over crypto regulation")]
    result = regulatory_news_signal(headlines)
    assert result.relevant_headlines[0]["tilt"] == "neutral"


def test_majority_vote_across_multiple_headlines():
    headlines = [
        _h("SEC approves new crypto ETF listing"),
        _h("Lawmakers pass crypto market structure bill"),
        _h("Regulator sues token issuer for fraud"),
    ]
    result = regulatory_news_signal(headlines)
    assert result.bullish_count == 2
    assert result.bearish_count == 1
    assert result.signal == "BUY"


def test_tie_is_neutral():
    headlines = [_h("SEC approves ETF"), _h("Regulator sues exchange")]
    result = regulatory_news_signal(headlines)
    assert result.signal == "NEUTRAL"


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
