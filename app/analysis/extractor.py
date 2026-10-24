from dataclasses import dataclass

import anthropic
from fastapi import Depends

from app.analysis.prompt import build_messages
from app.analysis.schema import PostAnalysis, normalize
from app.config import Settings, get_settings

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


@dataclass
class ExtractionResult:
    analysis: PostAnalysis
    model: str
    input_tokens: int
    output_tokens: int
    notes: str | None
    raw: dict


def get_anthropic_client(settings: Settings = Depends(get_settings)) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _call(client, *, system_prompt, page_name, posted_at, text, image_urls, notes) -> ExtractionResult:
    response = client.messages.parse(
        model=HAIKU,
        max_tokens=4096,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=build_messages(page_name=page_name, posted_at=posted_at, text=text, image_urls=image_urls),
        output_format=PostAnalysis,
        output_config={"effort": "low"},
    )
    parsed = response.parsed_output
    if parsed is None:
        raise ValueError("structured output missing")
    try:
        raw = response.model_dump()
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    return ExtractionResult(
        analysis=normalize(parsed),
        model=HAIKU,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        notes=notes,
        raw=raw,
    )


def analyze_post(
    client: anthropic.Anthropic, *, system_prompt: str, page_name, posted_at, text, image_urls
) -> ExtractionResult:
    kwargs = dict(system_prompt=system_prompt, page_name=page_name, posted_at=posted_at, text=text)
    try:
        return _call(client, image_urls=image_urls, notes=None, **kwargs)
    except anthropic.BadRequestError as e:
        msg = str(e).lower()
        if image_urls and ("image" in msg or "url" in msg):
            return _call(client, image_urls=[], notes=f"kép nélkül elemezve: {str(e)[:200]}", **kwargs)
        raise
