import json
from dataclasses import asdict
from datetime import date

from app.analysis.extractor import HAIKU
from app.analysis.prompt import load_prompt_file
from app.summary.comments_llm import CommentInsights
from app.summary.stats import SummaryStats

HUN_MONTHS = [
    "január", "február", "március", "április", "május", "június",
    "július", "augusztus", "szeptember", "október", "november", "december",
]


def summary_sentence(
    client, *, page_name: str | None, month: date, stats: SummaryStats, insights: CommentInsights | None
) -> tuple[str, dict]:
    subject = page_name or "A piac"
    month_name = HUN_MONTHS[month.month - 1]
    if stats.post_count == 0:
        return (
            f"{subject}: nem volt poszt {month.year} {month_name} hónapban.",
            {"input_tokens": 0, "output_tokens": 0},
        )
    payload = {
        "alany": subject,
        "ev": month.year,
        "honap": month_name,
        "stats": asdict(stats),
        "insights": insights.model_dump() if insights else None,
    }
    response = client.messages.create(
        model=HAIKU,
        max_tokens=300,
        system=load_prompt_file("monthly_sentence_v1"),
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        output_config={"effort": "low"},
    )
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return response.content[0].text.strip(), usage
