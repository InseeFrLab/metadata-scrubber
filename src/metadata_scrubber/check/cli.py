"""cli.py — Commande `scrubber-check`.

    scrubber-check fichier.xml [--index PATH] [--serie s1998 …] [--operation s2219 …]
                   [--exclude-operation s2297 …] [--format table|json|csv]
                   [--output rapport.json] [--fail-on doublon|proche|never]
    scrubber-check index build [--output PATH] [--serie …] [--operation …] [--no-geo]
    scrubber-check index info  [--index PATH]

Codes de sortie : 0 = rien à signaler, 1 = doublons (selon --fail-on), 2 = erreur.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ..extractor import read_bytes
from ..insee_api import InseeMetadataClient
from ..registry_io import write_text
from .index import ReferenceIndex, build_index, default_index_path, index_metadata, read_index, write_index
from .report import check_bytes, has_findings, to_csv, to_table

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


def _check_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrubber-check",
        description=(
            "Détecte, pour un fichier DDI, les CodeLists déjà publiées dans l'API Métadonnées "
            "de l'Insee (doublons, sous-ensembles, listes proches). "
            "Voir aussi : scrubber-check index build | info."
        ),
    )
    parser.add_argument("fichier", help="Fichier DDI 3.3 (XML), local ou S3.")
    parser.add_argument("--index", default=default_index_path(),
                        help="Index de référence (défaut : $SCRUBBER_CHECK_INDEX ou reference_index.json.gz).")
    parser.add_argument("--serie", action="append", default=[], metavar="ID",
                        help="Restreindre la référence à une série (répétable).")
    parser.add_argument("--operation", action="append", default=[], metavar="ID",
                        help="Restreindre la référence à une opération (répétable).")
    parser.add_argument("--exclude-operation", action="append", default=[], metavar="ID",
                        help="Ignorer les listes propres à cette opération (re-contrôle d'une opération publiée).")
    parser.add_argument("--format", choices=["table", "json", "csv"], default="table")
    parser.add_argument("--output", help="Écrire le rapport dans ce fichier (local ou S3) au lieu de la sortie standard.")
    parser.add_argument("--fail-on", choices=["doublon", "proche", "never"], default="doublon",
                        help="Code de sortie 1 si des doublons (ou listes proches) sont trouvés.")
    return parser


def _index_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scrubber-check index", description="Index de référence.")
    sub = parser.add_subparsers(dest="action", required=True)

    build = sub.add_parser("build", help="Construire l'index depuis l'API (toute la base par défaut).")
    build.add_argument("--output", default=default_index_path(), help="Chemin de l'index (local ou S3).")
    build.add_argument("--serie", action="append", default=[], metavar="ID")
    build.add_argument("--operation", action="append", default=[], metavar="ID")
    build.add_argument("--no-geo", action="store_true", help="Ne pas importer les listes du COG.")
    build.add_argument("--geo-date", help="Date du COG (AAAA-MM-JJ, défaut : aujourd'hui).")
    build.add_argument("--cache-dir",
                       help="Cache des objets DDI (défaut : $SCRUBBER_CHECK_CACHE ou api_cache/ à côté de l'index).")

    info = sub.add_parser("info", help="Décrire un index existant.")
    info.add_argument("--index", default=default_index_path())
    return parser


def _index_command(argv: list[str]) -> int:
    args = _index_parser().parse_args(argv)
    if args.action == "info":
        meta = index_metadata(read_index(args.index))
        meta.pop("operations", None)
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return EXIT_OK

    cache_dir = args.cache_dir or os.environ.get("SCRUBBER_CHECK_CACHE") or (
        f"{args.output.rsplit('/', 1)[0]}/api_cache" if "/" in args.output else "api_cache"
    )
    client = InseeMetadataClient(cache_dir=cache_dir)
    doc = build_index(
        client, series=args.serie, operations=args.operation,
        with_geo=not args.no_geo, geo_date=args.geo_date,
        log=lambda msg: print(msg, file=sys.stderr),
    )
    write_index(doc, args.output)
    counts = doc["counts"]
    print(
        f"Index écrit : {args.output} · {counts['codelists']} liste(s) "
        f"({counts['by_level']}) · {counts['operations']} opération(s) avec DDI, "
        f"{counts['operations_without_ddi']} sans.",
        file=sys.stderr,
    )
    return EXIT_OK


def _check_command(argv: list[str]) -> int:
    args = _check_parser().parse_args(argv)
    index = ReferenceIndex.load(args.index).restrict(
        series=args.serie, operations=args.operation, exclude_operations=args.exclude_operation,
    )
    report = check_bytes(read_bytes(args.fichier), args.fichier, index)

    if args.format == "json":
        text = json.dumps(report, ensure_ascii=False, indent=2)
    elif args.format == "csv":
        text = to_csv(report)
    else:
        text = to_table(report)
    if args.output:
        write_text(text, args.output)
        print(f"Rapport écrit : {args.output}", file=sys.stderr)
    else:
        print(text)
    return EXIT_FINDINGS if has_findings(report, args.fail_on) else EXIT_OK


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    try:
        code = _index_command(argv[1:]) if argv[:1] == ["index"] else _check_command(argv)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"scrubber-check : erreur : {exc}", file=sys.stderr)
        code = EXIT_ERROR
    sys.exit(code)


if __name__ == "__main__":
    main()
