from datetime import datetime, timezone
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest

from app.analysis.extractor import HAIKU, analyze_post
from app.analysis.schema import Ar, PostAnalysis


def make_analysis(**overrides) -> PostAnalysis:
    data = dict(
        celkozonseg="maganszemely", ceges_szegmens="nincs", lakossagi_szegmens="csaladi_haz",
        use_case="Kerti bútorok tárolása télen.", fajdalompont="Nincs hely a garázsban.",
        szezonalis_horog="nincs", kontener_meretek=["6 m2"], ar_megjelenik=False, arak=[], ar_ertek=None,
        ar_egyseg=None, arhorgony="nincs", ajanlat_mechanika=["nincs"], surgosseg="nincs",
        bizalmi_elemek=["nincs"], cta_tipus="hivj", cta_szoveg="Hívj most!",
        kulcsszavak=["tárolás", "konténer", "kert"], konfidencia=4,
    )
    return PostAnalysis(**{**data, **overrides})


def make_response(analysis):
    return MagicMock(
        parsed_output=analysis,
        usage=MagicMock(input_tokens=100, output_tokens=50),
        model_dump=lambda: {"id": "msg"},
    )


def test_analyze_post_parses_and_normalizes():
    client = MagicMock()
    client.messages.parse.return_value = make_response(
        make_analysis(arak=[Ar(meret="6 m2", ertek=25000, egyseg="ft_ho")], ar_ertek=None)
    )
    res = analyze_post(client, system_prompt="sys", page_name="P", posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc), text="t", image_urls=[])
    assert res.analysis.ar_ertek == 25000
    assert res.model == HAIKU == "claude-haiku-5-5"
    assert res.input_tokens == 100
    kw = client.messages.parse.call_args.kwargs
    assert kw["output_format"] is PostAnalysis
    assert kw["output_config"] == {"effort": "low"}


def test_image_error_falls_back_to_text_only():
    r = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.BadRequestError(
        "Could not fetch image url", response=r, body={"error": {"message": "Could not fetch image url"}}
    )
    client = MagicMock()
    client.messages.parse.side_effect = [err, make_response(make_analysis())]
    res = analyze_post(
        client, system_prompt="sys", page_name="P", posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc), text="t", image_urls=["https://x/a.jpg"]
    )
    assert res.notes.startswith("kép nélkül elemezve")
    second = client.messages.parse.call_args_list[1].kwargs["messages"]
    assert all(b["type"] != "image" for m in second for b in m["content"])
    first = client.messages.parse.call_args_list[0].kwargs["messages"]
    assert any(b["type"] == "image" for m in first for b in m["content"])


def test_other_bad_request_reraised():
    r = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    err = anthropic.BadRequestError("bad", response=r, body=None)
    client = MagicMock()
    client.messages.parse.side_effect = err
    with pytest.raises(anthropic.BadRequestError):
        analyze_post(client, system_prompt="s", page_name="P", posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc), text="t", image_urls=["u"])
