import json
from datetime import date
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.summary.comments_llm import CommentInsights, Insight, build_comment_message, cluster_comments
from app.summary.sentence import summary_sentence
from app.summary.stats import SummaryStats

M = date(2026, 9, 1)


def _insight(n=1):
    return Insight(text=f"Mennyibe kerül? {n}", count=2, example="Mennyi az ár?")


def test_cluster_comments_marks_page_replies_and_limits_three():
    client = MagicMock()
    client.messages.parse.return_value = MagicMock(
        parsed_output=CommentInsights(top_questions=[_insight()], top_objections=[]),
        usage=MagicMock(input_tokens=100, output_tokens=50),
    )
    comments = [
        {"id": 1, "text": "Mennyibe kerül egy konténer?", "author_is_page": False},
        {"id": 2, "text": "Írtunk privátban!", "author_is_page": True},
        {"id": 3, "text": "Túl drága", "author_is_page": False},
    ]
    insights, usage = cluster_comments(client, comments=comments, page_name="Kontigo")
    assert insights.top_questions[0].count == 2
    assert usage == {"input_tokens": 100, "output_tokens": 50}
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["max_tokens"] == 4096
    assert kwargs["output_format"] is CommentInsights
    assert kwargs["output_config"] == {"effort": "medium"}
    content = kwargs["messages"][0]["content"]
    assert "Oldal: Kontigo" in content
    assert "2. [OLDAL] Írtunk privátban!" in content
    assert "1. Mennyibe kerül egy konténer?" in content
    with pytest.raises(ValidationError):
        CommentInsights(top_questions=[_insight(i) for i in range(4)], top_objections=[])


def test_cluster_requires_three_comments():
    client = MagicMock()
    with pytest.raises(ValueError):
        cluster_comments(client, comments=[{"id": 1, "text": "a", "author_is_page": False}] * 2, page_name="X")
    client.messages.parse.assert_not_called()


def test_sentence_zero_posts_no_llm():
    client = MagicMock()
    sentence, usage = summary_sentence(client, page_name="Kontigo", month=M, stats=SummaryStats(), insights=None)
    client.messages.create.assert_not_called()
    assert "Kontigo" in sentence and "2026 szeptember" in sentence
    assert usage == {"input_tokens": 0, "output_tokens": 0}
    sentence, _ = summary_sentence(client, page_name=None, month=M, stats=SummaryStats(), insights=None)
    assert sentence.startswith("A piac")


def test_sentence_calls_haiku_low_effort():
    client = MagicMock()
    client.messages.create.return_value = MagicMock(
        content=[MagicMock(text="  Kontigo 2026 szeptember hónapban a magánszemélyeket célozta.  ")],
        usage=MagicMock(input_tokens=200, output_tokens=30),
    )
    stats = SummaryStats(post_count=4, lakossagi_szegmens_breakdown={"csaladi_haz": 4})
    insights = CommentInsights(top_questions=[_insight()], top_objections=[])
    sentence, usage = summary_sentence(client, page_name="Kontigo", month=M, stats=stats, insights=insights)
    assert sentence == "Kontigo 2026 szeptember hónapban a magánszemélyeket célozta."
    assert usage == {"input_tokens": 200, "output_tokens": 30}
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert kwargs["max_tokens"] == 300
    assert kwargs["output_config"] == {"effort": "low"}
    payload = json.loads(kwargs["messages"][0]["content"])
    assert payload["alany"] == "Kontigo" and payload["honap"] == "szeptember" and payload["ev"] == 2026
    assert payload["stats"]["post_count"] == 4
    assert payload["insights"]["top_questions"][0]["count"] == 2


def test_comment_message_header_states_total_when_capped():
    comments = [{"text": f"k{i}", "author_is_page": False} for i in range(300)]
    capped = build_comment_message(comments, "Kontigo", total_count=412)
    assert "Kommentek száma: 412 (a 300 legfrissebb szerepel)" in capped
    full = build_comment_message(comments[:3], None, total_count=3)
    assert full.startswith("Oldal: összes oldal\nKommentek száma: 3\n")
