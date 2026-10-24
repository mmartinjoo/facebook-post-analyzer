from typing import Literal

from pydantic import BaseModel, Field

Celkozonseg = Literal["maganszemely", "vallalkozas", "mindketto", "nem_megallapithato"]
CegesSzegmens = Literal[
    "vendeglatas", "logisztika", "epitoipar", "kereskedelem", "szolgaltato", "webshop", "egyeb", "nincs"
]
LakossagiSzegmens = Literal[
    "csaladi_haz", "panel", "koltozo", "felujito", "motoros", "sportolo", "hobbi_gyujto", "egyeb", "nincs"
]
SzezonalisHorog = Literal["gumicsere", "unnep", "koltozesi_szezon", "nyar_vege", "felujitas", "tanev", "nincs"]
ArEgyseg = Literal["ft_ho", "ft_m2_ho", "ft_nap", "egyeb"]
Arhorgony = Literal["garazs", "raktar", "masik_szolgaltato", "nincs"]
AjanlatMechanika = Literal["kedvezmeny", "elso_honap_ingyen", "nincs_kaucio", "rovid_minimum_ido", "azonnal", "nincs"]
Surgosseg = Literal["utolso_darabok", "foglaltsag_szazalek", "bovites", "hataridos_akcio", "nincs"]
BizalmiElem = Literal["kamera", "kerites", "24h_belepes", "sajat_lakat", "orzott", "szaraz", "nincs"]
CtaTipus = Literal["hivj", "irj", "kattints", "foglalj", "nincs"]


class Ar(BaseModel):
    meret: str | None = Field(description="A konténer mérete, ahogy a posztban szerepel (pl. \"20 láb\", \"6 m2\"), vagy null.")
    ertek: int = Field(description="Az ár forintban, egész számként (pl. 45000).")
    egyseg: ArEgyseg = Field(description="Az ár egysége: ft_ho, ft_m2_ho, ft_nap vagy egyeb.")


class PostAnalysis(BaseModel):
    celkozonseg: Celkozonseg = Field(description="Kinek szól a poszt: magánszemélynek, vállalkozásnak vagy mindkettőnek.")
    ceges_szegmens: CegesSzegmens = Field(description="A megcélzott céges szegmens; nincs, ha nem cégeknek szól; egyeb, ha cégeknek szól, de egyik sem illik.")
    lakossagi_szegmens: LakossagiSzegmens = Field(
        description="A megcélzott lakossági szegmens; nincs, ha nem magánszemélyeknek szól; egyeb, ha nekik szól, de egyik sem illik."
    )
    use_case: str = Field(description="Egy magyar mondat: a konkrét tárolási helyzet, nem kategória; üres string, ha nincs.")
    fajdalompont: str = Field(description="Egy magyar mondat: a probléma, amire a poszt megoldást kínál; üres string, ha nincs.")
    szezonalis_horog: SzezonalisHorog = Field(description="Szezonhoz vagy alkalomhoz kötött horog; nincs, ha nincs.")
    kontener_meretek: list[str] = Field(description="A posztban szereplő konténerméretek az eredeti alakjukban.")
    ar_megjelenik: bool = Field(description="Szerepel-e bármilyen ár a posztban vagy a képeken.")
    arak: list[Ar] = Field(description="Minden ár külön elemként, mérettel és egységgel; az ingyenes/0 Ft ajánlat nem ár; üres, ha nincs ár.")
    ar_ertek: int | None = Field(description="A legalacsonyabb ár az arak listából (\"ártól\"), vagy null.")
    ar_egyseg: ArEgyseg | None = Field(description="Az ar_ertek egysége, vagy null.")
    arhorgony: Arhorgony = Field(description="Mihez hasonlítja az árat a poszt (garázs, raktár, másik szolgáltató).")
    ajanlat_mechanika: list[AjanlatMechanika] = Field(description="Az ajánlat elemei; [\"nincs\"], ha nincs ajánlat.")
    surgosseg: Surgosseg = Field(description="A sürgetés módja; nincs, ha nincs.")
    bizalmi_elemek: list[BizalmiElem] = Field(description="Biztonsági/bizalmi elemek; [\"nincs\"], ha nincs.")
    cta_tipus: CtaTipus = Field(description="A cselekvésre ösztönzés típusa; nincs, ha nincs.")
    cta_szoveg: str = Field(description="A cselekvésre ösztönző szöveg szó szerint a posztból; üres, ha nincs.")
    kulcsszavak: list[str] = Field(description="3–5 kisbetűs magyar kulcsszó.")
    konfidencia: int = Field(ge=1, le=5, description="1–5: mennyire biztos a célközönség-besorolás.")


def _clean_enum_list(values: list[str]) -> list[str]:
    deduped = list(dict.fromkeys(values))
    if len(deduped) > 1 and "nincs" in deduped:
        deduped = [v for v in deduped if v != "nincs"]
    return deduped or ["nincs"]


def normalize(a: PostAnalysis) -> PostAnalysis:
    update: dict = {
        "ajanlat_mechanika": _clean_enum_list(a.ajanlat_mechanika),
        "bizalmi_elemek": _clean_enum_list(a.bizalmi_elemek),
    }
    priced = [ar for ar in a.arak if ar.ertek > 0]
    if priced:
        lowest = min(priced, key=lambda ar: ar.ertek)
        update |= {"ar_megjelenik": True, "ar_ertek": lowest.ertek, "ar_egyseg": lowest.egyseg}
    else:
        update |= {"ar_megjelenik": False, "ar_ertek": None, "ar_egyseg": None}
    return a.model_copy(update=update)
