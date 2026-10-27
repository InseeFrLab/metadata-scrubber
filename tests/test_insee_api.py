"""Client de l'API Métadonnées, corpus d'opérations et lignée BasedOnObject."""

import pytest
from lxml import etree

from api_fixtures import make_client
from metadata_scrubber.corpus import apply_provenance, build_corpus, resolve_operations
from metadata_scrubber.extractor import extract_codelists, extract_variables, parse_xml
from metadata_scrubber.funnel import detect_lineage, prepare_signatures


def test_resolve_operations_from_serie_and_operation():
    client, _ = make_client()
    assert resolve_operations(client, series=["s1998"]) == {
        "s2022": "s1998", "s2023": "s1998", "s2024": "s1998",
    }
    assert resolve_operations(client, operations=["s2024"]) == {"s2024": "s1998"}
    assert resolve_operations(client, all_operations=True) == {
        "s2022": "s1998", "s2023": "s1998", "s2024": "s1998",
    }
    with pytest.raises(ValueError, match="s9999"):
        resolve_operations(client, operations=["s9999"])


def test_operations_restrict_their_serie():
    client, _ = make_client()
    # Une opération de la série demandée la restreint…
    assert resolve_operations(client, series=["s1998"], operations=["s2024"]) == {"s2024": "s1998"}
    # …mais pas « toute la base »
    assert len(resolve_operations(client, operations=["s2024"], all_operations=True)) == 3


def test_build_corpus_provenance_and_skipped_operations():
    client, _ = make_client()
    corpus = build_corpus(client, resolve_operations(client, series=["s1998"]), log=lambda _: None)

    assert corpus.skipped == {"s2022": "pas de description DDI"}
    assert set(corpus.operations) == {"s2023", "s2024"}
    # La liste partagée est rattachée aux deux millésimes, et téléchargée une seule fois
    assert corpus.provenance["pays"]["operations"] == {"s2023", "s2024"}
    assert corpus.provenance["sexe-24"] == {"operations": {"s2024"}, "series": {"s1998"}}

    codelists = extract_codelists(corpus.objects)
    assert sorted(cl.id for cl in codelists) == sorted(
        ["sexe-23", "sexe-24", "contrat-23", "contrat-24", "pays"])
    by_id = {cl.id: cl for cl in codelists}
    assert by_id["contrat-24"].codes[2] == ("APP", "Apprentissage")
    assert by_id["sexe-24"].based_on == ["sexe-23"]
    assert by_id["sexe-24"].urn == "urn:ddi:fr.insee:sexe-24:1"

    apply_provenance(codelists, corpus)
    assert by_id["pays"].operations == {"s2023", "s2024"}
    assert {v.cl_id for v in extract_variables(corpus.objects)} == set(by_id)


def test_codelists_are_downloaded_once_and_cached(tmp_path):
    client, fake = make_client(cache_dir=str(tmp_path / "cache"))
    build_corpus(client, {"s2023": "s1998", "s2024": "s1998"}, log=lambda _: None)
    assert fake.requests.count("structures/codelist/fr.insee/pays/1") == 1
    assert (tmp_path / "cache" / "codelist" / "fr.insee" / "pays" / "1.xml").exists()

    # Deuxième construction : fichiers et CodeLists versionnés lus dans le cache
    client2, fake2 = make_client(cache_dir=str(tmp_path / "cache"))
    build_corpus(client2, {"s2023": "s1998", "s2024": "s1998"}, log=lambda _: None)
    assert not [p for p in fake2.requests if p.startswith(("structures/codelist", "structures/fichier"))]
    assert client2.cache_hits == 7   # 2 fichiers + 5 CodeLists


def test_detect_lineage():
    client, _ = make_client()
    corpus = build_corpus(client, {"s2023": "s1998", "s2024": "s1998"}, log=lambda _: None)
    codelists = extract_codelists(corpus.objects)
    prepare_signatures(codelists)

    pairs = {(p.id, c.id): round(sim, 3) for p, c, sim in detect_lineage(codelists)}
    assert pairs == {("sexe-23", "sexe-24"): 1.0, ("contrat-23", "contrat-24"): 0.667}


def test_geo_list_and_labels():
    client, _ = make_client()
    assert client.geo_list("regions")[0] == {"code": "11", "intitule": "Île-de-France"}
    assert client.serie("s1998")["famille"] == {"id": "s12", "label": "Emploi"}
    assert client.serie("s0000") is None


def test_parse_xml_does_not_resolve_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    raw = (f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file://{secret}">]>'
           f'<FragmentInstance><Fragment><CodeList><ID>&xxe;</ID></CodeList></Fragment>'
           f'</FragmentInstance>').encode()
    objects = parse_xml(raw)
    assert "SECRET" not in etree.tostring(objects[0]).decode()


def test_restricted_objects_are_skipped():
    from metadata_scrubber.insee_api import DdiRef

    client, _ = make_client()
    assert client.codelist_fragments(DdiRef("fr.insee", "restreinte", "1")) == []
    assert client.restricted == ["structures/codelist/fr.insee/restreinte/1"]
