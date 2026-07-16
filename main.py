"""main.py — Orchestrateur du pipeline : extraire codelist_duplicates.json depuis un XML DDI.

Pipeline :
  1. Lecture + parsing XML
  2. Extraction CodeLists + Catégories
  3. Extraction références Variables
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
from pathlib import Path

# Ajouter src/ au PYTHONPATH pour l'exécution directe
_src = Path(__file__).resolve().parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from lxml import etree  # noqa: E402

from scrubber.extractor import (  # noqa: E402
    extract_codelists,
    parse_xml,
    read_bytes,
)
from scrubber.funnel import (  # noqa: E402
    detect_exact_duplicates,
    detect_fuzzy_duplicates,
)
from scrubber.normalize import normalize, signature_from_codes  # noqa: E402
from scrubber.reporting.duplicates_registry import (  # noqa: E402
    write_duplicates_registry,
)
from scrubber.signals import (  # noqa: E402
    cross_check as compute_cross,
    find_usage_groups,
)
from scrubber.semantic import (  # noqa: E402
    _VarRecord,
    detect_semantic_codelists,
    detect_semantic_via_variables,
    llm_judge,
)
from scrubber.types import CandidateFusion, CodeList  # noqa: E402


# ────────────────────────────────────────────────────────────────────────
# Utilitaires XPath
# ────────────────────────────────────────────────────────────────────────


def _local(el: etree._Element) -> str:
    """Nom de balise sans namespace."""
    return etree.QName(el).localname


def _child(el: etree._Element, name: str) -> etree._Element | None:
    """Premier enfant direct dont le nom local est `name`."""
    for c in el:
        if _local(c) == name:
            return c
    return None


def _text_of(el: etree._Element, name: str) -> str:
    """Texte d'un enfant direct."""
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def _localized(el: etree._Element, name: str) -> str:
    """Texte d'un conteneur (CodeListName, r:Label...) → son r:String."""
    c = _child(el, name)
    if c is None:
        return ""
    kids = list(c)
    return (kids[0].text or "").strip() if kids else (c.text or "").strip()


# ────────────────────────────────────────────────────────────────────────
# Pipeline principal
# ────────────────────────────────────────────────────────────────────────


def run_pipeline(
    xml_source: str = "s3://projet-metadonnees-rmes/BTS.xml",
    audit_dir: str = "s3://projet-metadonnees-rmes/scrubber_output",
    run_llm: bool = True,
    verbose: bool = False,
    registry_path: str | None = None,
) -> None:
    """
    Pipeline complet : extraction → détection → registre des doublons.

    Output unique : `codelist_duplicates.json` dans `audit_dir`.

    Args:
        xml_source:    URL S3 ou chemin local du fichier DDI.
        audit_dir:     Répertoire de sortie.
        run_llm:       Si True, phases sémantiques (embeddings + juge LLM).
        verbose:       Si True, détails LLM/embeddings.
        registry_path: Registre nettoyé (cleaned_codelists.json) à injecter :
                       ses listes sont prioritaires (masters) et les listes
                       déjà remplacées sont exclues de la détection.
    """
    print("=" * 60)
    print(f"  Dédoublonnage DDI — source : {xml_source}")
    print("=" * 60)

    # ------------------------------------------------------------------
    # 1. Lecture + parsing
    # ------------------------------------------------------------------
    print("\n[1/9] Lecture et parsing XML...")
    raw = read_bytes(xml_source)
    objects = parse_xml(raw)
    root = etree.fromstring(raw)

    # ------------------------------------------------------------------
    # 2. Extraction CodeLists + Catégories
    # ------------------------------------------------------------------
    print("[2/9] Extraction des CodeLists et Catégories...")
    codelists = extract_codelists(objects)
    print(f"  {len(codelists)} CodeLists ({sum(len(cl.codes) for cl in codelists)} codes).")

    # ------------------------------------------------------------------
    # 2 bis. Injection du registre nettoyé (priorité aux entrées du registre)
    # ------------------------------------------------------------------
    if registry_path:
        from scrubber.cleaned_registry import load_cleaned_codelists

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
    variable_map: dict[str, list[str]] = {}
    for frag in root.iter():
        if _local(frag) != "Fragment":
            continue
        for obj in frag:
            tag = _local(obj)
            if tag not in ("Variable", "RepresentedVariable"):
                continue
            var_name = (
                _localized(obj, "VariableName")
                or _localized(obj, "RepresentedVariableName")
                or _text_of(obj, "ID")
            )
            code_repr = _child(obj, "CodeRepresentation")
            if code_repr is None:
                continue
            cl_ref = _child(code_repr, "CodeListReference")
            if cl_ref is None:
                continue
            cl_id = _text_of(cl_ref, "ID")
            if cl_id:
                variable_map.setdefault(cl_id, []).append(var_name)

    linked = 0
    for cl in codelists:
        if cl.id in variable_map:
            cl.vars = variable_map[cl.id]
            linked += 1
    print(f"  {linked}/{len(codelists)} CodeLists ont des variables référentes.")

    # ------------------------------------------------------------------
    # 4. Signatures de contenu
    # ------------------------------------------------------------------
    print("[4/9] Calcul des signatures de contenu...")
    for cl in codelists:
        cl.sig = signature_from_codes(cl.codes)

    # ------------------------------------------------------------------
    # 5. Détection exacte
    # ------------------------------------------------------------------
    exact_groups = detect_exact_duplicates(codelists)
    n_exact_dups = sum(len(g) - 1 for g in exact_groups.values())
    print(f"  [exact] {len(exact_groups)} groupes, {n_exact_dups} redondantes.")

    # ------------------------------------------------------------------
    # 6. Détection floue
    # ------------------------------------------------------------------
    detected, all_pairs_dict, _ = detect_fuzzy_duplicates(codelists)
    n_detected = len(detected)
    n_inspect = len(all_pairs_dict)
    print(f"  [flou]  {n_detected} pairs détectées, {n_inspect} d'inspection.")

    # ------------------------------------------------------------------
    # 7. Signaux d'usage
    # ------------------------------------------------------------------
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
                    from scrubber.semantic import _get_openai_client

                    client = _get_openai_client()
                    for cl_a, cl_b, *_ in unique_pairs[:max_llm]:
                        llm_judge(client, cl_a, cl_b, verbose=verbose)

        except RuntimeError as exc:
            print(f"  [semantic] Sauté — {exc}")
    else:
        print("\n[8/9] Détection sémantique — sautée (run_llm=False).")

    print(f"  [semantic] {len(pairs_semantic)} paires sémantiques collectées.")

    # ------------------------------------------------------------------
    # 9. Construire candidats
    # ------------------------------------------------------------------
    print("\n[9/9] Génération du registre des doublons...")
    candidates = _build_candidates(
        exact_groups, detected, codelists, usage_groups, pairs_semantic,
    )
    print(f"  {len(candidates)} candidats totaux.")

    if audit_dir.startswith("s3://"):
        out_path = f"{audit_dir.rstrip('/')}/codelist_duplicates.json"
    else:
        out_path = os.path.join(audit_dir, "codelist_duplicates.json")
        os.makedirs(audit_dir, exist_ok=True)
    write_duplicates_registry(candidates, codelists, out_path)
    print(f"  → {out_path}")

    print("\n" + "=" * 60)
    print("  Pipeline terminé !")
    print("=" * 60)


# ────────────────────────────────────────────────────────────────────────
# Construction des candidats
# ────────────────────────────────────────────────────────────────────────


def _build_candidates(
    exact_groups: dict,
    fuzzy_pairs: list,
    codelists: list[CodeList],
    usage_groups: dict,
    semantic_pairs: list[tuple[CodeList, CodeList, float, str]] | None = None,
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
                confidence=pair["score"],
                evidence={
                    "text_similarity": pair["score"],
                    "a_codes": pair.get("a_codes"),
                    "b_codes": pair.get("b_codes"),
                },
            )
        )
        used.update(master_cl.id, slave.id)

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
                    confidence=round(score, 4),
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline de dédoublonnage CodeLists DDI 3.3.",
    )
    parser.add_argument(
        "xml_source",
        nargs="?",
        default="s3://projet-metadonnees-rmes/BTS.xml",
        help="Fichier XML source (S3 ou local). Par défaut BTS.xml.",
    )
    parser.add_argument(
        "--audit-dir",
        default="s3://projet-metadonnees-rmes/scrubber_output",
        help="Répertoire de sortie (codelist_duplicates.json).",
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
    run_pipeline(
        xml_source=args.xml_source,
        audit_dir=args.audit_dir,
        run_llm=args.run_llm,
        verbose=args.verbose,
        registry_path=args.registry_path,
    )


if __name__ == "__main__":
    main()
