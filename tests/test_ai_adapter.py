from tools.ai_adapter import summarize, rewrite


def test_summarize_mock_short():
    text = "This is a sentence. This is another sentence. More details follow."
    s = summarize(text, length="short")
    assert isinstance(s, str)
    assert len(s) > 0


def test_rewrite_mock_professional():
    text = "yo check this out, our product is like super cool and stuff"
    r = rewrite(text, style={"tone": "professional"})
    assert "professional" in r or "edited" in r.lower()
