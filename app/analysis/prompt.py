from datetime import datetime
from pathlib import Path

from app.timeutil import BUDAPEST

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def load_prompt_file(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def load_prompt(version: str) -> str:
    return load_prompt_file(f"post_extraction_{version}")


def build_messages(*, page_name: str, posted_at: datetime, text: str | None, image_urls: list[str]) -> list[dict]:
    day = posted_at.astimezone(BUDAPEST).strftime("%Y-%m-%d")
    body = text if text and text.strip() else "(nincs szöveg)"
    content: list[dict] = [
        {"type": "text", "text": f"Oldal: {page_name}\nDátum: {day}\n\nPoszt szövege:\n{body}"}
    ]
    content += [{"type": "image", "source": {"type": "url", "url": u}} for u in image_urls]
    return [{"role": "user", "content": content}]
