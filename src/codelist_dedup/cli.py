"""Interface en ligne de commande du dédoublonnage des listes de codes.

Exemple :

    uv run codelist-dedup ingest --operation BPE \\
        --source s3://projet-metadonnees-rmes/BPE.xml \\
        --registry ./registry.sqlite --output-dir ./output
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

from . import report
from .extract import parse_operation
from .registry import Registry, pair_key
from .signature import compute_signatures
from .source import open_source


def _print_rows(rows: list[dict], columns: list[str], fmt: str) -> None:
    """Affiche des lignes en table (défaut), csv ou json."""
    if fmt == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if fmt == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return
    if not rows:
        print("(aucun résultat)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def _cmd_init(args: argparse.Namespace) -> int:
    Registry(args.registry).close()
    print(f"Registre initialisé : {args.registry}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    print(f"Lecture {args.source} ...", file=sys.stderr)
    with open_source(args.source) as stream:
        records, stats = parse_operation(stream)

    for rec in records:
        compute_signatures(rec)

    print(
        f"Extrait : {stats.n_codelists} listes, {stats.n_categories} catégories, "
        f"{stats.n_codes} codes, {stats.n_unresolved} codes non résolus",
        file=sys.stderr,
    )

    if args.dry_run:
        n_distinct = len({r.sig_pairs for r in records})
        print(
            f"[dry-run] {len(records)} listes → {n_distinct} signatures distinctes "
            f"({len(records) - n_distinct} doublons parfaits intra-opération). "
            "Rien n'a été écrit dans le registre.",
        )
        return 0

    with Registry(args.registry) as reg:
        result = reg.fold_in(args.operation, args.source, records)
        out_dir = Path(args.output_dir) / args.operation
        paths = report.write_all(
            reg, args.operation, out_dir,
            emit_rml=args.emit_rml, near_threshold=args.near_threshold,
        )

    print(
        f"Intégré '{args.operation}' : {result.n_codelists} listes → "
        f"{result.n_new} nouveaux canons, {result.n_merged} rattachées (inter-op), "
        f"{result.n_intra_dup} doublons intra-op."
    )
    print("Artefacts :")
    for p in paths:
        print(f"  - {p}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    with Registry(args.registry) as reg:
        out_dir = Path(args.output_dir) / args.operation
        paths = report.write_all(
            reg, args.operation, out_dir,
            emit_rml=args.emit_rml, near_threshold=args.near_threshold,
        )
    for p in paths:
        print(p)
    return 0


def _cmd_semantic(args: argparse.Namespace) -> int:
    from . import rml, semantic

    embed_model = args.embed_model or semantic.EMBED_MODEL
    chat_model = args.chat_model or semantic.CHAT_MODEL
    with Registry(args.registry) as reg:
        candidates = semantic.run_semantic(
            reg,
            embed_model=embed_model,
            chat_model=chat_model,
            min_cosine=args.min_cosine,
            top_k=args.top_k,
            max_judgements=args.max_judgements,
            use_llm=not args.no_llm,
        )
        out_dir = Path(args.output_dir) / args.operation
        paths = [
            report.write_semantic_candidates(
                reg, args.operation, out_dir, candidates, chat_model
            )
        ]
        summary = report.write_semantic_summary(
            reg, args.operation, out_dir, candidates, chat_model
        )
        paths.append(out_dir / f"semantic_summary_{args.operation}.md")
        if args.emit_rml:
            paths.append(
                rml.write_rml(
                    reg, args.operation, out_dir,
                    threshold=args.near_threshold, semantic_model=chat_model,
                )
            )

    print(
        f"Doublons sémantiques confirmés : {summary['confirmed']} paires "
        f"/ {summary['codelists_involved']} listes "
        f"(candidats {summary['candidates']}, jugés {summary['judged']}, "
        f"non jugés {summary['not_judged']})."
    )
    if summary["cosine"]:
        msg = f"  cosinus médian {summary['cosine'][1]:.3f}"
        if summary["confidence"]:
            msg += f", confiance médiane {summary['confidence'][1]:.2f}"
        print(msg)
    print("Artefacts :")
    for p in paths:
        print(f"  - {p}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with Registry(args.registry) as reg:
        canon = reg.conn.execute(
            "SELECT * FROM canonical WHERE canonical_id = ?", (args.canonical_id,)
        ).fetchone()
        if canon is None:
            print(f"Canon introuvable : {args.canonical_id}", file=sys.stderr)
            return 1
        print(f"# {canon['canonical_id']} — {canon['display_name']}")
        if canon["label"]:
            print(f"  Libellé : {canon['label']}")
        review = reg.get_review("canonical", args.canonical_id)
        print(
            f"  {canon['n_pairs']} paires, vu d'abord dans '{canon['first_seen_op']}'"
            + (f" · revue : {review}" if review else "")
        )
        print("  Membres :")
        for m in reg.conn.execute(
            "SELECT operation, source_id, ddi_id, version, codelist_name"
            " FROM member WHERE canonical_id = ? ORDER BY operation, ddi_id",
            (args.canonical_id,),
        ):
            print(
                f"    - [{m['operation']}] {m['codelist_name']} "
                f"(v{m['version']}, {m['source_id']})"
            )
        sem = reg.semantic_for_canonical(args.canonical_id)
        if sem:
            print("  Rapprochements sémantiques :")
            for s in sem:
                other = s["b_name"] if s["canonical_a"] == args.canonical_id else s["a_name"]
                verdict = {1: "même concept", 0: "distinct"}.get(s["same_concept"], "?")
                dec = reg.get_review("semantic", pair_key(s["canonical_a"], s["canonical_b"]))
                print(
                    f"    ↔ {other} (cos {s['cosine']:.3f}, {verdict}"
                    f"{', conf ' + str(s['confidence']) if s['confidence'] is not None else ''}"
                    f"{', revue ' + dec if dec else ''})"
                )
        print("  Contenu :")
        for p in reg.conn.execute(
            "SELECT value, label FROM canonical_pairs WHERE canonical_id = ?"
            " ORDER BY ord",
            (args.canonical_id,),
        ):
            print(f"    {p['value']} → {p['label']}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with Registry(args.registry) as reg:
        if args.what == "semantic":
            rows = reg.browse_semantic(
                confirmed_only=args.confirmed_only,
                min_cosine=args.min_cosine,
                limit=args.limit,
                offset=args.offset,
            )
            for r in rows:
                r["same_concept"] = {1: True, 0: False}.get(r["same_concept"], "")
                r["cosine"] = round(r["cosine"], 4)
            cols = ["canonical_a", "canonical_b", "a_name", "b_name", "cosine",
                    "same_concept", "confidence", "review"]
        else:
            rows = reg.browse_canonicals(
                operation=args.operation,
                redundant=args.redundant,
                min_members=args.min_members,
                search=args.search,
                include_empty=not args.no_empty,
                sort=args.sort,
                limit=args.limit,
                offset=args.offset,
            )
            cols = ["canonical_id", "display_name", "label", "n_pairs", "n_members",
                    "n_operations", "first_seen_op", "review"]
    _print_rows(rows, cols, args.format)
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    author = args.author or os.environ.get("USER", "?")
    with Registry(args.registry) as reg:
        if args.set_label is not None or args.set_display_name is not None or \
                args.set_representative is not None:
            if not args.canonical_id:
                print("--set-* nécessite --canonical-id", file=sys.stderr)
                return 2
            try:
                if args.set_label is not None:
                    reg.set_canonical_field(args.canonical_id, "label", args.set_label)
                    print(f"label mis à jour pour {args.canonical_id}")
                if args.set_display_name is not None:
                    reg.set_canonical_field(
                        args.canonical_id, "display_name", args.set_display_name
                    )
                    print(f"display_name mis à jour pour {args.canonical_id}")
                if args.set_representative is not None:
                    ddi, _, ver = args.set_representative.partition(":")
                    m = reg.set_representative(args.canonical_id, ddi, ver or None)
                    print(f"représentant → {m['codelist_name']} (v{m['version']})")
            except (KeyError, ValueError) as exc:
                print(f"Erreur : {exc}", file=sys.stderr)
                return 1

        if args.review is not None:
            if args.pair:
                key = pair_key(args.pair[0], args.pair[1])
                reg.set_review("semantic", key, args.review, args.note, author)
                print(f"revue '{args.review}' enregistrée pour la paire {key} (par {author})")
            elif args.canonical_id:
                reg.set_review("canonical", args.canonical_id, args.review, args.note, author)
                print(
                    f"revue '{args.review}' enregistrée pour {args.canonical_id} (par {author})"
                )
            else:
                print("--review nécessite --canonical-id ou --pair", file=sys.stderr)
                return 2
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    from . import inspect as inspect_mod

    operation = args.operation or Path(args.source).stem
    out_dir = Path(args.output_dir) / operation
    print(f"Lecture {args.source} ...", file=sys.stderr)
    with open_source(args.source) as stream:
        counts, df_objects, _df_codes, paths = inspect_mod.run_inspect(
            stream, operation, out_dir, write_parquet=not args.no_parquet
        )

    print(
        f"Objets : {len(df_objects)} (+ {counts.get('Code', 0)} codes). Par type :"
    )
    for otype, n in counts.most_common():
        print(f"  {otype:24} {n}")

    if args.sample:
        print(f"\nÉchantillons (≤{args.sample} par type) :")
        for otype, _ in counts.most_common():
            sub = df_objects[df_objects["type"] == otype].head(args.sample)
            if sub.empty:
                continue
            print(f"  {otype} :")
            for _, r in sub.iterrows():
                print(f"    {r['name']}  —  {r['label'] or ''}")

    if paths:
        print("\nParquet :")
        for p in paths:
            print(f"  - {p}")
    else:
        print("\n(--no-parquet : aucun fichier écrit)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codelist-dedup",
        description="Dédoublonnage incrémental des listes de codes DDI 3.3.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Créer un registre vide.")
    p_init.add_argument("--registry", required=True)
    p_init.set_defaults(func=_cmd_init)

    p_ing = sub.add_parser("ingest", help="Intégrer une opération dans le registre.")
    p_ing.add_argument("--operation", required=True, help="Nom court (ex. BPE).")
    p_ing.add_argument("--source", required=True, help="Chemin local ou s3://...")
    p_ing.add_argument("--registry", required=True)
    p_ing.add_argument("--output-dir", default="./output")
    p_ing.add_argument("--dry-run", action="store_true")
    p_ing.add_argument("--emit-rml", action="store_true", help="Générer aussi le RML/TTL.")
    p_ing.add_argument("--near-threshold", type=float, default=0.90,
                       help="Seuil de similarité des quasi-doublons (défaut 0.90).")
    p_ing.set_defaults(func=_cmd_ingest)

    p_rep = sub.add_parser("report", help="Régénérer les artefacts depuis le registre.")
    p_rep.add_argument("--operation", required=True)
    p_rep.add_argument("--registry", required=True)
    p_rep.add_argument("--output-dir", default="./output")
    p_rep.add_argument("--emit-rml", action="store_true", help="Générer aussi le RML/TTL.")
    p_rep.add_argument("--near-threshold", type=float, default=0.90,
                       help="Seuil de similarité des quasi-doublons (défaut 0.90).")
    p_rep.set_defaults(func=_cmd_report)

    p_sem = sub.add_parser(
        "semantic",
        help="Phase 2 : rapprochement sémantique (embeddings + juge LLM).",
    )
    p_sem.add_argument("--registry", required=True)
    p_sem.add_argument("--output-dir", default="./output")
    p_sem.add_argument("--operation", default="all",
                       help="Libellé des fichiers de sortie (défaut : all).")
    p_sem.add_argument("--embed-model", default=None, help="Défaut : qwen3-embedding-8b.")
    p_sem.add_argument("--chat-model", default=None,
                       help="Juge LLM. Défaut : gemma4-26b-moe (rapide) ; "
                            "qwen3-6-35b-moe pour un arbitrage plus fin mais lent.")
    p_sem.add_argument("--min-cosine", type=float, default=0.85,
                       help="Seuil cosinus pour les candidats (défaut 0.85).")
    p_sem.add_argument("--top-k", type=int, default=5,
                       help="Voisins max par canon (défaut 5).")
    p_sem.add_argument("--max-judgements", type=int, default=50,
                       help="Plafond d'appels au juge LLM (défaut 50 ; "
                            "avec gemma4, sub-seconde, on peut monter sans coût notable).")
    p_sem.add_argument("--no-llm", action="store_true",
                       help="Embeddings + candidats seulement, sans juge LLM.")
    p_sem.add_argument("--emit-rml", action="store_true")
    p_sem.add_argument("--near-threshold", type=float, default=0.90)
    p_sem.set_defaults(func=_cmd_semantic)

    p_insp = sub.add_parser(
        "inspect",
        help="Inspecter un fichier source : comptes par type + export parquet.",
    )
    p_insp.add_argument("--source", required=True, help="Chemin local ou s3://...")
    p_insp.add_argument("--output-dir", default="./inspect")
    p_insp.add_argument("--operation", default=None,
                        help="Libellé de sortie (défaut : radical du nom de fichier).")
    p_insp.add_argument("--sample", type=int, default=3,
                        help="Nombre d'exemples affichés par type (défaut 3 ; 0 pour aucun).")
    p_insp.add_argument("--no-parquet", action="store_true",
                        help="Afficher le récap sans écrire de parquet.")
    p_insp.set_defaults(func=_cmd_inspect)

    p_show = sub.add_parser("show", help="Inspecter un canon et ses membres.")
    p_show.add_argument("--registry", required=True)
    p_show.add_argument("--canonical-id", required=True)
    p_show.set_defaults(func=_cmd_show)

    p_list = sub.add_parser("list", help="Parcourir le registre (canons ou paires).")
    p_list.add_argument("--registry", required=True)
    p_list.add_argument("--what", choices=["canonical", "semantic"], default="canonical")
    p_list.add_argument("--format", choices=["table", "csv", "json"], default="table")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--offset", type=int, default=0)
    # canoniques
    p_list.add_argument("--operation", help="Canons ayant un membre dans cette opération.")
    p_list.add_argument("--redundant", action="store_true", help="Canons à >1 membre.")
    p_list.add_argument("--min-members", type=int, default=0)
    p_list.add_argument("--search", help="Sous-chaîne dans display_name/label.")
    p_list.add_argument("--no-empty", action="store_true", help="Exclure les listes vides.")
    p_list.add_argument("--sort", choices=["members", "pairs", "name"], default="members")
    # sémantique
    p_list.add_argument("--confirmed-only", action="store_true")
    p_list.add_argument("--min-cosine", type=float, default=0.0)
    p_list.set_defaults(func=_cmd_list)

    p_upd = sub.add_parser(
        "update", help="Éditer un canon ou enregistrer une décision de revue."
    )
    p_upd.add_argument("--registry", required=True)
    p_upd.add_argument("--canonical-id", help="Canon cible.")
    p_upd.add_argument("--pair", nargs=2, metavar=("CID_A", "CID_B"),
                       help="Paire de canons (pour --review).")
    p_upd.add_argument("--set-label")
    p_upd.add_argument("--set-display-name")
    p_upd.add_argument("--set-representative", metavar="DDI_ID[:VERSION]",
                       help="Choisir la liste membre à conserver comme canonique.")
    p_upd.add_argument("--review", choices=["accepted", "rejected", "pending"])
    p_upd.add_argument("--note")
    p_upd.add_argument("--author", help="Défaut : $USER.")
    p_upd.set_defaults(func=_cmd_update)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
