"""Listes suspectes : placeholders, représentations numériques, code unique."""

import json

import pytest

from metadata_scrubber.quality import (
    NUMERIC_RANGE,
    PLACEHOLDER,
    SINGLE_CODE,
    name_key,
    quality_flags,
)


class TestQualityFlags:
    @pytest.mark.parametrize("codes,expected", [
        # BTS : renvoi vers le dictionnaire des nomenclatures
        ([("cf", "Voir dictionnaire des nomenclatures")], [PLACEHOLDER, SINGLE_CODE]),
        ([("Vide", "Non renseigné"), ("cf", "Voir dictionnaire des nomenclatures")],
         [PLACEHOLDER, SINGLE_CODE]),
        # BTS : QVA
        ([("xxxxxxx", "Code quartier du QVA"), ("CSZ", "Commune Sans Zonage"),
          ("HZ", "Hors zonage"), ("_Z", "Sans objet")], [PLACEHOLDER]),
        # EAR (abd7ae72) : la valeur décrit un format au lieu d'un code
        ([("", "Hors communauté"), ("Code sur 6 positions", "Identifiant de la communauté")],
         [PLACEHOLDER]),
        # EAR / BPE : représentations numériques
        ([("00 à 23", "Rang de l'individu")], [NUMERIC_RANGE, SINGLE_CODE]),
        ([("1", "0 à 7")], [NUMERIC_RANGE, SINGLE_CODE]),
        ([("de 0 à N", "Capacité"), ("NA", "Capacité indéterminée ou non concernée")],
         [NUMERIC_RANGE]),
    ])
    def test_suspect(self, codes, expected):
        assert quality_flags(codes) == expected

    @pytest.mark.parametrize("codes", [
        [("1", "De 15 à 24 ans"), ("2", "De 25 à 39 ans"), ("3", "De 40 à 59 ans")],
        [("1", "Contrat à durée indéterminée"), ("2", "Contrat à durée déterminée")],
        [("1", "Présence"), ("0", "Absence"), ("_Z", "Sans objet")],
        # EAR (CL-EAR2024-CABPROV) : libellés commençant par « Code » dans une vraie liste
        [("0", "Commune non flashée"), ("1", "Code à barres présent dans le fichier pilote"),
         ("2", "Code à barres trouvé mais absent du fichier pilote"), ("3", "Autre")],
    ])
    def test_regular_lists(self, codes):
        assert quality_flags(codes) == []


def test_name_key():
    assert name_key("N-categorie_juridique") == name_key("N-CATEGORIE-JURIDIQUE")
    assert name_key("", "Libellé") == "libellé"


def _xml(lists):
    frags = []
    for cl_id, name in lists:
        frags.append(
            f"<Fragment><CodeList><ID>{cl_id}</ID><CodeListName><String>{name}</String>"
            f"</CodeListName><Code><Value>cf</Value><CategoryReference><ID>cat-cf</ID>"
            f"</CategoryReference></Code></CodeList></Fragment>"
        )
    frags.append(
        "<Fragment><Category><ID>cat-cf</ID><Label><Content>Voir dictionnaire des "
        "nomenclatures</Content></Label></Category></Fragment>"
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><MetaDataDocument>{"".join(frags)}</MetaDataDocument>'


def test_placeholder_lists_are_grouped_by_name_only(tmp_path):
    from metadata_scrubber import main

    xml = tmp_path / "in.xml"
    xml.write_text(_xml([
        ("cl-region", "N-region"),
        ("cl-cj", "N-categorie_juridique"),
        ("cl-REGION", "N-REGION"),
        ("cl-conv", "N-conv_coll"),
    ]), encoding="utf-8")

    result = main.run_pipeline(xml_source=str(xml), audit_dir=str(tmp_path / "out"), run_llm=False)

    run_dir = tmp_path / "out" / result["run_id"]
    registry = json.loads((run_dir / "codelist_duplicates.json").read_text(encoding="utf-8"))
    # Seules les deux listes de régions restent doublons
    assert [d["id"] for d in registry["cl-region"]["duplicates"]] == ["cl-REGION"]
    assert registry["cl-cj"]["duplicates"] == []
    assert registry["cl-conv"]["duplicates"] == []
    assert registry["cl-cj"]["quality_flags"] == [PLACEHOLDER, SINGLE_CODE]

    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["placeholder"] == 4
