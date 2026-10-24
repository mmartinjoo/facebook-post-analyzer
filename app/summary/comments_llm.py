from pydantic import BaseModel, Field

from app.analysis.extractor import SONNET
from app.analysis.prompt import load_prompt_file

MAX_COMMENT_CHARS = 500


class Insight(BaseModel):
    text: str
    count: int
    example: str


class CommentInsights(BaseModel):
    top_questions: list[Insight] = Field(max_length=3)
    top_objections: list[Insight] = Field(max_length=3)


def build_comment_message(comments: list[dict], page_name: str | None) -> str:
    lines = [f"Oldal: {page_name or 'összes oldal'}", f"Kommentek száma: {len(comments)}", ""]
    n = 0
    for c in comments:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        n += 1
        prefix = "[OLDAL] " if c.get("author_is_page") else ""
        lines.append(f"{n}. {prefix}{text[:MAX_COMMENT_CHARS]}")
    return "\n".join(lines)


def cluster_comments(client, *, comments: list[dict], page_name: str) -> tuple[CommentInsights, dict]:
    if len(comments) < 3:
        raise ValueError("comment clustering needs at least 3 comments")
    response = client.messages.parse(
        model=SONNET,
        max_tokens=4096,
        system=load_prompt_file("comment_clustering_v1"),
        messages=[{"role": "user", "content": build_comment_message(comments, page_name)}],
        output_format=CommentInsights,
        output_config={"effort": "medium"},
    )
    parsed = response.parsed_output
    if parsed is None:
        raise ValueError("structured output missing")
    usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return parsed, usage
