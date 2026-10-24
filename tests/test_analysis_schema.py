from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.analysis.prompt import build_messages, load_prompt, load_prompt_file
from app.analysis.schema import PostAnalysis, normalize

FIELDS = list(PostAnalysis.model_fields)


def _analysis(**overrides) -> dict:
    data = {
        "celkozonseg": "maganszemely",
        "ceges_szegmens": "nincs",
        "lakossagi_szegmens": "panel",
        "use_case": "Nincs hol tartani a téli gumit szezonon kívül.",
        "fajdalompont": "A panel tárolója tele van.",
        "szezonalis_horog": "gumicsere",
        "kontener_meretek": [],
        "ar_megjelenik": False,
        "arak": [],
        "ar_ertek": None,
        "ar_egyseg": None,
        "arhorgony": "nincs",
        "ajanlat_mechanika": ["nincs"],
        "surgosseg": "nincs",
        "bizalmi_elemek": ["nincs"],
        "cta_tipus": "nincs",
        "cta_szoveg": "",
        "kulcsszavak": ["téli gumi", "panel", "tárhely"],
        "konfidencia": 4,
    }
    data.update(overrides)
    return data


def test_schema_rejects_unknown_enum():
    with pytest.raises(ValidationError):
        PostAnalysis(**_analysis(celkozonseg="ceg"))


def test_normalize_derives_price_from_list():
    a = PostAnalysis(
        **_analysis(
            arak=[
                {"meret": "20 láb", "ertek": 45000, "egyseg": "ft_ho"},
                {"meret": "9 láb", "ertek": 25000, "egyseg": "ft_ho"},
            ],
            ar_ertek=None,
            ar_megjelenik=False,
        )
    )
    n = normalize(a)
    assert n.ar_megjelenik is True
    assert n.ar_ertek == 25000
    assert n.ar_egyseg == "ft_ho"


def test_normalize_drops_nincs_when_other_values():
    n = normalize(PostAnalysis(**_analysis(bizalmi_elemek=["nincs", "kamera"])))
    assert n.bizalmi_elemek == ["kamera"]


def test_build_messages_has_text_then_images():
    messages = build_messages(
        page_name="Kontigo",
        posted_at=datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc),
        text="Őszi akció!",
        image_urls=["https://example.com/a.jpg", "https://example.com/b.jpg"],
    )
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    blocks = messages[0]["content"]
    assert len(blocks) == 3
    assert blocks[0]["type"] == "text"
    assert "Kontigo" in blocks[0]["text"]
    assert "2026-09-15" in blocks[0]["text"]
    for block in blocks[1:]:
        assert block["type"] == "image"
        assert block["source"]["type"] == "url"
    assert blocks[1]["source"]["url"] == "https://example.com/a.jpg"


def test_prompt_v1_loads_and_mentions_fields():
    prompt = load_prompt("v1")
    for name in ("celkozonseg", "szezonalis_horog", "konfidencia"):
        assert name in prompt
    assert len(FIELDS) == 19
    missing = [f for f in FIELDS if f not in prompt]
    assert missing == []


def test_load_prompt_file_missing_raises():
    with pytest.raises(FileNotFoundError):
        load_prompt_file("does_not_exist")


def test_normalize_ignores_non_positive_prices():
    a = PostAnalysis(
        **_analysis(
            arak=[
                {"meret": None, "ertek": 0, "egyseg": "ft_ho"},
                {"meret": "20 láb", "ertek": 45000, "egyseg": "ft_m2_ho"},
            ]
        )
    )
    n = normalize(a)
    assert (n.ar_megjelenik, n.ar_ertek, n.ar_egyseg) == (True, 45000, "ft_m2_ho")

    only_free = normalize(PostAnalysis(**_analysis(arak=[{"meret": None, "ertek": 0, "egyseg": "ft_ho"}], ar_ertek=0)))
    assert (only_free.ar_megjelenik, only_free.ar_ertek, only_free.ar_egyseg) == (False, None, None)
