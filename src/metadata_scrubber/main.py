"""main.py — Orchestrateur du pipeline : extraire codelist_duplicates.json depuis un XML DDI.

Pipeline :
  1. Lecture + parsing XML
  2. Extraction CodeLists + Catégories
  3. Extraction références Variables (rattachement aux CodeLists)
  4. Calcul signatures
  5. Détection exacte
  6. Détection floue
  7. Signaux d'usage + croisement
  8. Détection sémantique (embeddings + juge LLM) — optionnelle
  9. Construction candidats
  10. Génération de codelist_duplicates.json

Output unique : codelist_duplicates.json  (regroupe toutes les
CodeLists identifiées avec leur liste de doublons par paire.)
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ajouter src/ au PYTHONPATH pour l'exécution directe
_src = Path(__file__).resolve().parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from lxml import etree  # noqa: E402

from .extractor import (  # noqa: E402
    extract_codelists,
    extract_variables,
    parse_xml,
    read_bytes,
)
from .funnel import (  # noqa: E402
    MAX_CODES_FOR_FUZZY,
    detect_business_duplicates,
    detect_exact_duplicates,
    detect_fuzzy_duplicates,
    detect_label_duplicates,
    detect_lineage,
    prepare_signatures,
)
from .normalize import normalize  # noqa: E402
from .reporting.duplicates_registry import (  # noqa: E402
    build_duplicates_registry,
)
from .signals import (  # noqa: E402
    cross_check as compute_cross,
    find_usage_groups,
)
from .semantic import (  # noqa: E402
    _VarRecord,
    detect_semantic_codelists,
    detect_semantic_via_variables,
    llm_judge,
)
from .decisions import apply_decisions, decisions_path, load_decisions  # noqa: E402
from .registry_io import write_json_registry  # noqa: E402
from .run_output import RunLogCapture, make_run_dir  # noqa: E402
from .corpus import apply_provenance  # noqa: E402
from .levels import OFFICIELLE, master_rank, suggest_official  # noqa: E402
from .quality import PLACEHOLDER  # noqa: E402
from .types import CandidateFusion, CodeList  # noqa: E402

# La confiance 1.0 est réservée à la signature exacte
MAX_NON_EXACT_CONFIDENCE = 0.99

DEFAULT_XML_SOURCE = "s3://projet-metadonnees-rmes/BTS.xml"


def _id_list(ids: list[str], limit: int = 10) -> str:
    """« s1, s2, … et 5 autres » pour les avertissements."""
    more = f" … et {len(ids) - limit} autres" if len(ids) > limit else ""
    return ", ".join(ids[:limit]) + more


def _name_list(codelists: list, limit: int = 10) -> str:
    """Noms des CodeLists avec leur nombre de codes, pour les avertissements."""
    sizes = [f"{len(cl.codes):,}".replace(",", "\u202f") for cl in codelists]
    return _id_list([f"{cl.name or cl.id} ({n} codes)" for cl, n in zip(codelists, sizes)], limit)


def describe_api_scope(api_scope: dict | None) -> str:
    """Libellé de la source API (run_summary, journal) : « api:toutes » ou « api:s1998,s2219 »."""
    if not api_scope:
        return ""
    if api_scope.get("all_operations"):
        return "api:toutes"
    ids = list(api_scope.get("series") or []) + list(api_scope.get("operations") or [])
    return "api:" + ",".join(ids)


# ────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ────────────────────────────────────────────────────────────────────────


def run_pipeline(
    xml_source: str | None = DEFAULT_XML_SOURCE,
    audit_dir: str = "s3://projet-metadonnees-rmes/scrubber_output",
    run_llm: bool = True,
    verbose: bool = False,
    registry_path: str | None = None,
    api_scope: dict | None = None,
) -> dict:
    """
    Pipeline complet : extraction → détection → registre des doublons.

    Chaque exécution écrit dans son propre dossier ``<audit_dir>/<AAAAMMJJ-HHMMSS>/`` :
    `codelist_duplicates.json`, `run_summary.json` et `pipeline.log` (le journal
    est écrit même en cas d'erreur).

    Args:
        xml_source:    URL S3 ou chemin local du fichier DDI.
        audit_dir:     Racine des sorties (le dossier daté est créé dessous).
        run_llm:       Si True, phases sémantiques (embeddings + juge LLM).
        verbose:       Si True, détails LLM/embeddings.
        registry_path: Registre nettoyé (cleaned_codelists.json) à injecter :
                       ses listes sont prioritaires (masters) et les listes
                       déjà remplacées sont exclues de la détection.
        api_scope:     Source API Insee à la place du XML :
                       {"operations": [...], "series": [...], "all_operations": bool}.

    Returns:
        {"run_id", "run_dir", "output_files": {nom: chemin}, "warnings": [...]}
    """
    started = datetime.now(timezone.utc)
    run_id, run_dir = make_run_dir(audit_dir, started)
    if api_scope:
        xml_source = describe_api_scope(api_scope)
    summary: dict = {
        "run_id": run_id,
        "xml_source": xml_source,
        "started_at": started.isoformat(),
        "run_llm": run_llm,
        "registry_path": registry_path,
        "status": "error",
        "warnings": [],
        "counts": {},
    }

    with RunLogCapture(f"{run_dir}/pipeline.log"):
        try:
            _run_pipeline_body(
                xml_source, run_dir, run_llm, verbose, registry_path, summary, api_scope,
                audit_dir=audit_dir,
            )
            summary["status"] = "success"
        finally:
            summary["duration_seconds"] = round(
                (datetime.now(timezone.utc) - started).total_seconds(), 1
            )
            # Écrit même en cas d'erreur : le diagnostic reste consultable
            try:
                write_json_registry(summary, f"{run_dir}/run_summary.json")
            except Exception as exc:  # ne pas masquer l'erreur d'origine
                print(f"[scrubber] run_summary.json non écrit : {exc}")

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "output_files": {
            "codelist_duplicates.json": f"{run_dir}/codelist_duplicates.json",
            "run_summary.json": f"{run_dir}/run_summary.json",
            "pipeline.log": f"{run_dir}/pipeline.log",
        },
        "warnings": summary["warnings"],
    }


def _run_pipeline_body(
    xml_source: str,
    run_dir: str,
    run_llm: bool,
    verbose: bool,
    registry_path: str | None,
    summary: dict,
    api_scope: dict | None = None,
    audit_dir: str | None = None,
) -> None:
    """Corps du pipeline ; alimente `summary` (avertissements, compteurs)."""
    print("=" * 60)
    print(f"  Dédoublonnage DDI — source : {xml_source}")
    print(f"  Dossier de sortie du run : {run_dir}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. Lecture + parsing
    # ------------------------------------------------------------------
    corpus = None
    if api_scope:
        print("\n[1/9] Téléchargement depuis l'API Métadonnées de l'Insee...")
        corpus = _load_api_corpus(api_scope, audit_dir or run_dir.rsplit("/", 1)[0], summary)
        objects = corpus.objects
    else:
        print("\n[1/9] Lecture et parsing XML...")
        raw = read_bytes(xml_source)
        objects = parse_xml(raw)

    # ------------------------------------------------------------------
    # 2. Extraction CodeLists + Catégories
    # ------------------------------------------------------------------
    print("[2/9] Extraction des CodeLists et Catégories...")
    codelists = extract_codelists(objects)
    print(f"  {len(codelists)} CodeLists ({sum(len(cl.codes) for cl in codelists)} codes).")
    n_old_versions = sum(1 for o in objects if etree.QName(o).localname == "CodeList") - len(codelists)
    if n_old_versions:
        print(f"  {n_old_versions} version(s) antérieure(s) de CodeList ignorée(s).")
    summary["counts"]["older_versions_ignored"] = n_old_versions
    if corpus is not None:
        apply_provenance(codelists, corpus)

    # ------------------------------------------------------------------
    # 2 bis. Injection du registre nettoyé (priorité aux entrées du registre)
    # ------------------------------------------------------------------
    if registry_path:
        from .cleaned_registry import load_cleaned_codelists

        registry_cls, replaced_ids = load_cleaned_codelists(registry_path)
        registry_ids = {cl.id for cl in registry_cls}
        avant = len(codelists)
        codelists = [
            cl for cl in codelists
            if cl.id not in registry_ids and cl.id not in replaced_ids
        ]
        print(
            f"  [registre] {len(registry_cls)} listes injectées, "
            f"{avant - len(codelists)} listes XML exclues (déjà traitées)."
        )
        # Prépend : les entrées du registre deviennent masters des groupes
        codelists = registry_cls + codelists

    # ------------------------------------------------------------------
    # 3. Extraction références Variables
    # ------------------------------------------------------------------
    print("[3/9] Extraction des références Variables...")
    linked = _link_variables(codelists, objects)
    print(f"  {linked}/{len(codelists)} CodeLists ont des variables référentes.")
    unreferenced = [cl for cl in codelists if not cl.vars and cl.origin == "xml"]
    summary["counts"]["unreferenced"] = len(unreferenced)
    if unreferenced and corpus is None:
        # Une source API ne contient que des listes référencées : export pour le XML seul
        unref_path = f"{run_dir}/unreferenced_codelists.json"
        write_json_registry(
            {"count": len(unreferenced), "codelists": [_unreferenced_entry(cl) for cl in unreferenced]},
            unref_path,
        )
        print(f"  {len(unreferenced)} CodeList(s) sans variable référente → {unref_path}")

    # ------------------------------------------------------------------
    # 4. Signatures de contenu
    # ------------------------------------------------------------------
    # Décisions, écarts et maîtres choisies des runs précédents
    # (decisions.json, racine des sorties)
    out_path = f"{run_dir}/codelist_duplicates.json"
    decisions = load_decisions(decisions_path(out_path))

    # La première liste d'un groupe de doublons en devient la maître : registre
    # nettoyé, puis niveau (officielle d'abord), maître choisie, réutilisation.
    codelists.sort(key=lambda cl: master_rank(cl, set(decisions["masters"])))

    print("[4/9] Calcul des signatures de contenu...")
    n_placeholder = prepare_signatures(codelists)
    if n_placeholder:
        print(f"  {n_placeholder} CodeList(s) « placeholder » comparées sur leur nom.")
    summary["counts"]["placeholder"] = n_placeholder

    # ------------------------------------------------------------------
    # 5. Détection exacte
    # ------------------------------------------------------------------
    print("[5/9] Détection exacte...")
    exact_groups = detect_exact_duplicates(codelists)
    n_exact_dups = sum(len(g) - 1 for g in exact_groups.values())
    print(f"  [exact] {len(exact_groups)} groupes, {n_exact_dups} redondantes.")
    business_groups = detect_business_duplicates(codelists)
    label_groups = detect_label_duplicates(codelists)
    lineage_pairs = detect_lineage(codelists)
    print(
        f"  [hors échappement] {len(business_groups)} groupes ; "
        f"[libellés] {len(label_groups)} groupes ; "
        f"[lignée BasedOnObject] {len(lineage_pairs)} paires."
    )

    # ------------------------------------------------------------------
    # 6. Détection floue
    # ------------------------------------------------------------------
    print("[6/9] Détection floue...")
    detected, all_pairs_dict, _, excluded = detect_fuzzy_duplicates(codelists)
    n_detected = len(detected)
    n_inspect = len(all_pairs_dict)
    print(f"  [flou]  {n_detected} pairs détectées, {n_inspect} d'inspection.")
    summary["counts"]["codelists"] = len(codelists)
    summary["counts"]["excluded_over_max_codes"] = len(excluded)
    if excluded:
        noms = ", ".join(f"{cl.name or cl.id[:8]} ({len(cl.codes)} codes)" for cl in excluded)
        print(f"  [flou]  {len(excluded)} CodeList(s) exclue(s) (> {MAX_CODES_FOR_FUZZY} codes) : {noms}")
        summary["warnings"].append({
            "phase": "fuzzy",
            "status": "partial",
            "reason": (
                f"{len(excluded)} CodeList(s) de plus de {MAX_CODES_FOR_FUZZY} codes non comparée(s) "
                f"en détection floue (calcul trop coûteux) : {_name_list(excluded)}. "
                "Elles restent comparées en détection exacte."
            ),
        })

    # ------------------------------------------------------------------
    # 7. Signaux d'usage
    # ------------------------------------------------------------------
    print("[7/9] Signaux d'usage...")
    for cl in codelists:
        cl.var_sig = tuple(sorted(set(cl.vars)))

    usage_groups = find_usage_groups(codelists)
    shared = {sig: g for sig, g in usage_groups.items() if len(g) >= 2}
    print(f"  [usage] {len(shared)} groupes de même usage.")

    # Croisement flou × usage (top 5)
    print("\n  Croisement flou × usage (top 5) :")
    for pair in detected[:5]:
        sig = compute_cross(pair["a"], pair["b"])
        marker = sig.usage_type
        shared_count = len(sig.shared_vars)
        print(
            f"    {pair['score']:.3f} {pair['a_name']} ↔ {pair['b_name']} "
            f"[{marker}, {shared_count} partagées]"
        )
    if len(detected) > 5:
        print(f"    ... et {len(detected) - 5} autres.")

    # ------------------------------------------------------------------
    # 8. Détection sémantique (embeddings + juge LLM)
    # ------------------------------------------------------------------
    pairs_semantic: list[tuple] = []
    if run_llm:
        print("\n[8/9] Détection sémantique (embeddings LLM)...")
        try:
            # Phase 1 — embeddings directs des CodeLists
            pairs_cl = detect_semantic_codelists(codelists, verbose=verbose)
            print(f"  [semantic CL] {len(pairs_cl)} paires ≥ 0.90.")

            pairs_semantic.extend((p.cl_a, p.cl_b, p.score, p.phase) for p in pairs_cl)

            # Phase 2 — embeddings via variables
            if codelists and any(c.vars for c in codelists):
                var_recs: list[_VarRecord] = []
                for cl in codelists:
                    if cl.vars:
                        text = normalize(f"{cl.name} {' '.join(cl.vars)}")
                        var_recs.append(
                            _VarRecord(
                                var_name=cl.name or cl.id[:12],
                                var_label=cl.label,
                                cl_id=cl.id,
                                cl_name=cl.name or cl.id[:12],
                                text=text,
                            )
                        )
                if len(var_recs) > 1:
                    pairs_var = detect_semantic_via_variables(codelists, var_recs, verbose=verbose)
                    print(
                        f"  [semantic var] {len(pairs_var)} paires ≥ 0.92 (via variables)."
                    )
                    pairs_semantic.extend((p.cl_a, p.cl_b, p.score, p.phase) for p in pairs_var)

            # Juge LLM sur les meilleures paires
            if pairs_semantic:
                seen: set[tuple[str, str]] = set()
                unique_pairs: list[tuple] = []
                for cl_a, cl_b, score, phase in pairs_semantic:
                    key = (min(cl_a.id, cl_b.id), max(cl_a.id, cl_b.id))
                    if key not in seen:
                        seen.add(key)
                        unique_pairs.append((cl_a, cl_b, score, phase))
                unique_pairs.sort(key=lambda x: x[2], reverse=True)

                max_llm = min(len(unique_pairs), 20)
                if max_llm > 0:
                    from .semantic import _get_openai_client

                    client = _get_openai_client()
                    for cl_a, cl_b, *_ in unique_pairs[:max_llm]:
                        llm_judge(client, cl_a, cl_b, verbose=verbose)

        except RuntimeError as exc:
            print(f"  [semantic] Sauté — {exc}")
            summary["warnings"].append(
                {"phase": "semantic", "status": "skipped", "reason": str(exc)}
            )
    else:
        print("\n[8/9] Détection sémantique — sautée (run_llm=False).")
        summary["warnings"].append({
            "phase": "semantic",
            "status": "disabled",
            "reason": "Étape sémantique désactivée (--no-llm).",
        })

    print(f"  [semantic] {len(pairs_semantic)} paires sémantiques collectées.")

    # ------------------------------------------------------------------
    # 9. Construire candidats
    # ------------------------------------------------------------------
    print("\n[9/9] Génération du registre des doublons...")
    candidates = _build_candidates(
        exact_groups, detected, codelists, usage_groups, pairs_semantic,
        business_groups=business_groups, label_groups=label_groups,
        lineage_pairs=lineage_pairs,
    )
    print(f"  {len(candidates)} candidats totaux.")
    summary["counts"]["candidates"] = len(candidates)

    registry = build_duplicates_registry(
        candidates, codelists, preferred_parents=set(decisions["masters"])
    )
    n_suggested = _suggest_officials(registry, codelists)
    if n_suggested:
        print(f"  {n_suggested} liste(s) « placeholder » rattachée(s) à une nomenclature officielle.")
    carried = apply_decisions(registry, decisions)
    summary["counts"]["decisions_carried"] = carried
    if carried:
        print(f"  {carried} décision(s) reprise(s) des runs précédents.")

    write_json_registry(registry, out_path)
    print(f"  → {out_path}")

    print("\n" + "=" * 60)
    print("  Pipeline terminé !")
    print("=" * 60)


def _load_api_corpus(api_scope: dict, audit_dir: str, summary: dict):
    """Corpus DDI des opérations demandées (cache API sous ``<audit_dir>/api_cache``)."""
    from .corpus import build_corpus, resolve_operations
    from .insee_api import InseeMetadataClient

    client = InseeMetadataClient(cache_dir=f"{audit_dir.rstrip('/')}/api_cache")
    operations = resolve_operations(
        client,
        operations=api_scope.get("operations"),
        series=api_scope.get("series"),
        all_operations=bool(api_scope.get("all_operations")),
    )
    print(f"  {len(operations)} opération(s) à charger depuis {client.base_url}.")
    corpus = build_corpus(client, operations)
    summary["api"] = {
        "base_url": client.base_url,
        "operations": corpus.operations,
        "skipped_operations": corpus.skipped,
        "restricted_objects": corpus.restricted,
        "calls": client.calls,
        "cache_hits": client.cache_hits,
    }
    if corpus.skipped:
        no_ddi = sorted(op for op, why in corpus.skipped.items() if why == "pas de description DDI")
        no_file = sorted(op for op, why in corpus.skipped.items() if why != "pas de description DDI")
        n_total = len(corpus.operations) + len(corpus.skipped)
        parts = [
            f"{len(corpus.skipped)} opération(s) sur {n_total} ignorée(s), sans effet sur le reste du run ; "
            f"les {len(corpus.operations)} autres ont été analysées."
        ]
        if no_ddi:
            parts.append(
                f"{len(no_ddi)} n'ont pas de description DDI publiée dans l'API (variables, listes de "
                f"codes), elles n'apportent donc aucune CodeList : {_id_list(no_ddi)}."
            )
        if no_file:
            parts.append(f"{len(no_file)} n'ont aucun fichier décrit : {_id_list(no_file)}.")
        summary["warnings"].append({"phase": "api", "status": "partial", "reason": " ".join(parts)})
    if corpus.restricted:
        summary["warnings"].append({
            "phase": "api",
            "status": "partial",
            "reason": f"{len(corpus.restricted)} objet(s) DDI non diffusé(s) par l'API (accès refusé), ignoré(s).",
        })
    return corpus


def _suggest_officials(registry: dict, codelists: list[CodeList]) -> int:
    """Pour chaque liste « Voir dictionnaire… » du registre des doublons, la
    nomenclature officielle (registre nettoyé) qu'elle désigne : `suggested_official`."""
    officials = [cl for cl in codelists if cl.level == OFFICIELLE]
    if not officials:
        return 0
    by_id = {cl.id: cl for cl in codelists}
    n = 0
    for entry in registry.values():
        for item in [entry, *entry.get("duplicates", [])]:
            cl = by_id.get(item.get("id"))
            if cl is None or PLACEHOLDER not in cl.quality_flags:
                continue
            official = suggest_official(cl, officials)
            if official is not None:
                item["suggested_official"] = {"id": official.id, "name": official.name}
                n += 1
    return n


def _unreferenced_entry(cl: CodeList) -> dict:
    return {
        "id": cl.id,
        "urn": cl.urn,
        "name": cl.name,
        "label": cl.label,
        "codes_count": len(cl.codes),
        "based_on": cl.based_on,
    }


def _link_variables(codelists: list[CodeList], objects: list[etree._Element]) -> int:
    """Renseigne `cl.vars` (noms des variables qui référencent chaque CodeList).

    S'appuie sur `extract_variables`, qui lit la référence sous
    `VariableRepresentation` (DDI 3.3) comme directement sous la variable.

    Returns:
        Nombre de CodeLists ayant au moins une variable référente.
    """
    variable_map: dict[str, list[str]] = {}
    for var in extract_variables(objects):
        if var.cl_id:
            variable_map.setdefault(var.cl_id, []).append(var.name)

    linked = 0
    for cl in codelists:
        if cl.id in variable_map:
            cl.vars = variable_map[cl.id]
            linked += 1
    return linked


# ────────────────────────────────────────────────────────────────────────
# Construction des candidats
# ────────────────────────────────────────────────────────────────────────


def _build_candidates(
    exact_groups: dict,
    fuzzy_pairs: list,
    codelists: list[CodeList],
    usage_groups: dict,
    semantic_pairs: list[tuple[CodeList, CodeList, float, str]] | None = None,
    *,
    business_groups: list[list[CodeList]] | None = None,
    label_groups: list[list[CodeList]] | None = None,
    lineage_pairs: list[tuple[CodeList, CodeList, float]] | None = None,
) -> list[CandidateFusion]:
    """
    Assemble un unique list de CandidateFusion depuis tous
    les résultats de détection. Pas de doublons entre phases.

    Returns:
        Liste de CandidateFusion triée par confiance décroissante.
    """
    results: list[CandidateFusion] = []
    used: set[str] = set()

    # Phase 1 — exact
    for _sig, group in exact_groups.items():
        master_cl = group[0]
        for slave in group[1:]:
            if slave.id not in used:
                results.append(
                    CandidateFusion(
                        fusion_id=f"exact-{master_cl.id[:8]}",
                        detection_type="exact",
                        master_cl=master_cl,
                        slave_cls=[slave],
                        confidence=1.0,
                        evidence={"codes_count": len(master_cl.codes)},
                    )
                )
                used.add(slave.id)

    # Phase 1 — flou
    # Les esclaves flous ne sont marqués « utilisés » qu'après la boucle : toutes
    # les paires floues sont conservées (chaînes a→b→c), mais usage et
    # sémantique ne les re-signalent pas.
    fuzzy_slaves: set[str] = set()
    for pair in fuzzy_pairs:
        if pair["a"].id in used:
            continue
        master_cl = pair["a"]
        slave = pair["b"]
        results.append(
            CandidateFusion(
                fusion_id=f"fuzzy-{pair['score']:.3f}-{master_cl.id[:8]}-{slave.id[:8]}",
                detection_type="fuzzy",
                master_cl=master_cl,
                slave_cls=[slave],
                # 1.0 est réservé à la signature exacte
                confidence=min(pair["score"], MAX_NON_EXACT_CONFIDENCE),
                evidence={
                    "text_similarity": pair["score"],
                    "a_codes": pair.get("a_codes"),
                    "b_codes": pair.get("b_codes"),
                },
            )
        )
        fuzzy_slaves.add(slave.id)
    used.update(fuzzy_slaves)

    # Phase 1 ter — mêmes codes métier (hors échappement), puis mêmes libellés
    for det_type, groups, confidence in (
        ("exact_business", business_groups or [], 0.98),
        ("labels", label_groups or [], 0.95),
    ):
        for group in groups:
            master_cl = group[0]
            for slave in group[1:]:
                if slave.id in used or slave.id == master_cl.id:
                    continue
                results.append(
                    CandidateFusion(
                        fusion_id=f"{det_type}-{master_cl.id[:8]}-{slave.id[:8]}",
                        detection_type=det_type,
                        master_cl=master_cl,
                        slave_cls=[slave],
                        confidence=confidence,
                        evidence={"a_codes": len(master_cl.codes), "b_codes": len(slave.codes)},
                    )
                )
                used.add(slave.id)

    # Filiation DDI (BasedOnObject) : liste copiée d'une autre, contenu éventuellement modifié
    for parent, child, code_sim in lineage_pairs or []:
        if child.id in used or parent.id in used:
            continue
        results.append(
            CandidateFusion(
                fusion_id=f"lineage-{parent.id[:8]}-{child.id[:8]}",
                detection_type="lineage",
                master_cl=parent,
                slave_cls=[child],
                confidence=min(round(code_sim, 4), MAX_NON_EXACT_CONFIDENCE),
                evidence={
                    "based_on": parent.id,
                    "code_similarity": round(code_sim, 4),
                    "a_codes": len(parent.codes),
                    "b_codes": len(child.codes),
                },
            )
        )
        used.add(child.id)

    # Phase 1 bis — usage
    for _sig, group in usage_groups.items():
        if len(group) < 2:
            continue
        master_cl = group[0]
        for slave in group[1:]:
            if slave.id in used:
                continue
            results.append(
                CandidateFusion(
                    fusion_id=f"usage-{master_cl.id[:8]}-{slave.id[:8]}",
                    detection_type="usage",
                    master_cl=master_cl,
                    slave_cls=[slave],
                    confidence=0.85,
                    evidence={"shared_vars": list(_sig)},
                )
            )
            used.add(slave.id)

    # Phase 2 — sémantique
    if semantic_pairs:
        seen_semantic: set[str] = set()
        for cl_a, cl_b, score, phase in semantic_pairs:
            if cl_b.id in used:
                continue
            key = (cl_a.id, cl_b.id)
            if key in seen_semantic:
                continue
            seen_semantic.add(key)

            det_type = "semantic_list" if phase == "direct" else "semantic_var"
            results.append(
                CandidateFusion(
                    fusion_id=f"semantic-{det_type[0]}-{cl_a.id[:8]}-{cl_b.id[:8]}",
                    detection_type=det_type,
                    master_cl=cl_a,
                    slave_cls=[cl_b],
                    confidence=min(round(score, 4), MAX_NON_EXACT_CONFIDENCE),
                    evidence={
                        "cosine_score": round(score, 4),
                        "phase": phase,
                        "a_codes": len(cl_a.codes),
                        "b_codes": len(cl_b.codes),
                    },
                )
            )
            used.add(cl_b.id)

    results.sort(key=lambda c: c.confidence, reverse=True)
    return results


# ────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────


# Nomenclatures géographiques amorcées par défaut dans le registre nettoyé : les
# communes (~35 000 codes) sont exclues, l'écran d'édition du registre ne les
# afficherait pas bien (--geo communes pour les ajouter).
DEFAULT_SEED_GEO = [
    "regions", "departements", "arrondissements", "collectivitesDOutreMer", "pays",
    "zonesDEmploi2020", "unitesUrbaines2020", "airesDAttractionDesVilles2020",
    "bassinsDeVie2022", "quartiersPrioritairesDeLaPolitiqueDeLaVille2024",
]


def seed_officielles(
    registry_path: str,
    *,
    geo: list[str] | None = None,
    date: str | None = None,
    codelists: list[str] | None = None,
) -> dict[str, list[str]]:
    """Amorce le registre nettoyé avec des nomenclatures officielles (niveau « officielle »).

    Args:
        registry_path: registre nettoyé (cleaned_codelists.json), créé s'il n'existe pas.
        geo: listes du COG (chemins /geo/{kind}) ; [] pour aucune.
        date: date du COG (AAAA-MM-JJ, défaut : aujourd'hui).
        codelists: CodeLists officielles déjà dans Colectica, « agence/uuid » (dernière version).
    """
    from .check.index import geo_codelists
    from .cleaned_registry import add_official_entries, empty_cleaned_doc, read_cleaned_registry
    from .insee_api import InseeMetadataClient

    client = InseeMetadataClient()
    geo_lists = geo_codelists(client, date, DEFAULT_SEED_GEO if geo is None else geo)
    ddi_lists = []
    for spec in codelists or []:
        agency, _, cl_id = spec.partition("/")
        found = extract_codelists(client.latest_codelist_fragments(agency, cl_id))
        if not found:
            raise ValueError(f"CodeList introuvable dans l'API : {spec}")
        ddi_lists += found

    try:
        cleaned = read_cleaned_registry(registry_path)
    except FileNotFoundError:
        cleaned = empty_cleaned_doc()
    result = add_official_entries(cleaned, geo_lists, "api-geo")
    for key, ids in add_official_entries(cleaned, ddi_lists, "api-structures").items():
        result[key] += ids
    write_json_registry(cleaned, registry_path)
    return result


def _seed_main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="scrubber seed-officielles",
        description="Ajoute des nomenclatures officielles (API Insee) au registre nettoyé.",
    )
    parser.add_argument("--registry", required=True, help="Registre nettoyé (cleaned_codelists.json).")
    parser.add_argument("--geo", help=f"Listes du COG, séparées par des virgules (défaut : {','.join(DEFAULT_SEED_GEO)}) ; « aucune » pour ne pas en ajouter.")
    parser.add_argument("--date", help="Date du COG (AAAA-MM-JJ, défaut : aujourd'hui).")
    parser.add_argument("--codelist", action="append", default=[], metavar="AGENCE/UUID",
                        help="CodeList officielle déjà dans Colectica (répétable).")
    args = parser.parse_args(argv)
    geo = None if args.geo is None else ([] if args.geo == "aucune" else args.geo.split(","))
    result = seed_officielles(args.registry, geo=geo, date=args.date, codelists=args.codelist)
    print(f"{len(result['added'])} nomenclature(s) ajoutée(s), {len(result['updated'])} mise(s) à jour "
          f"→ {args.registry}")


def main() -> None:
    if sys.argv[1:2] == ["seed-officielles"]:
        _seed_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(
        description="Pipeline de dédoublonnage CodeLists DDI 3.3.",
    )
    parser.add_argument(
        "xml_source",
        nargs="?",
        default=None,
        help="Fichier XML source (S3 ou local). Par défaut BTS.xml, sauf source API.",
    )
    api = parser.add_argument_group(
        "source API Insee", "Télécharger les CodeLists depuis l'API Métadonnées au lieu d'un XML."
    )
    api.add_argument(
        "--operation", action="append", default=[], metavar="ID",
        help="Opération statistique (ex. s2219) ; répétable.",
    )
    api.add_argument(
        "--serie", action="append", default=[], metavar="ID",
        help="Toutes les opérations d'une série (ex. s1998) ; répétable.",
    )
    api.add_argument(
        "--all-operations", action="store_true",
        help="Toutes les opérations de toutes les séries (base complète).",
    )
    parser.add_argument(
        "--audit-dir",
        default="s3://projet-metadonnees-rmes/scrubber_output",
        help=(
            "Racine des sorties : chaque exécution écrit dans un sous-dossier daté "
            "(codelist_duplicates.json, run_summary.json, pipeline.log)."
        ),
    )
    parser.add_argument(
        "--run-llm",
        default=True,
        action="store_true",
        dest="run_llm",
        help="Exécuter les phases sémantiques (embeddings + juge LLM).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_false",
        dest="run_llm",
        help="Sauter les phases sémantiques (plus rapide).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        dest="verbose",
        default=False,
        help="Afficher les détails LLM/embeddings.",
    )
    parser.add_argument(
        "--registry",
        default=None,
        dest="registry_path",
        help="Registre nettoyé (cleaned_codelists.json) à injecter dans la détection.",
    )
    args = parser.parse_args()
    api_scope = None
    if args.operation or args.serie or args.all_operations:
        if args.xml_source:
            parser.error("choisir un fichier XML ou une source API, pas les deux")
        api_scope = {
            "operations": args.operation,
            "series": args.serie,
            "all_operations": args.all_operations,
        }
    result = run_pipeline(
        xml_source=args.xml_source or DEFAULT_XML_SOURCE,
        api_scope=api_scope,
        audit_dir=args.audit_dir,
        run_llm=args.run_llm,
        verbose=args.verbose,
        registry_path=args.registry_path,
    )
    print(f"Sorties du run : {result['run_dir']}")


if __name__ == "__main__":
    main()
