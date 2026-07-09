# %%
# --- Imports & paramètres ---------------------------------------------------
import random
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from itertools import combinations

import s3fs
from lxml import etree

SOURCE = "s3://projet-metadonnees-rmes/ddi-codelist-identifier.json"  # ou un chemin local

# Preferred: create the shared AWS profile once, then reuse it here.
fs = s3fs.S3FileSystem(
    profile="default",
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)

print(fs.ls("projet-metadonnees-rmes"))


def read_bytes(src: str) -> bytes:
    """Lit la source : objet S3 (via aws CLI) ou fichier local."""
    with fs.open(src, "rb") as f:
        return f.read()

# %%
# --- Lecture du JSON depuis S3 ------------------------------------------------
import json

with fs.open(SOURCE, "r", encoding="utf-8") as f:
    data = json.load(f)

# %%
# --- 1. Structure JSON --------------------------------------------------------
print("=== TYPE ROOT ===")
print(type(data).__name__)

if isinstance(data, dict):
    print(f"Clés racine ({len(data)}):")
    for k in list(data.keys()):
        print(f"  - {k!r}")

    # Un échantillon de la structure (profondeur max 3)
    def _preview(obj, indent=2, depth=0):
        if depth >= 3:
            return "..."
        if isinstance(obj, dict):
            lines = ["{\n"]
            for i, (k, v) in enumerate(list(obj.items())[:3]):
                prefix = "  " * (indent + 1)
                suffix = ",\n" if i < len(list(obj.keys())) - 1 else "\n"
                lines.append(f"{prefix}{k!r}: {_preview(v, indent + 1, depth + 1)}{suffix}")
            lines.append(f'{"  " * indent}}}')
            return "".join(lines)
        elif isinstance(obj, list):
            if len(obj) == 0:
                return "[]\n"
            sample = [str(o) for o in obj[:2]]
            if len(obj) > 2:
                sample.append("...")
            return f"[{', '.join(sample)}]\n"
        else:
            return repr(obj) + "\n"

    first_key = next(iter(data))
    print(f"\n--- Exemple de valeur pour la clé {first_key!r} ---")
    print(_preview(data[first_key]))

# %%
# --- 2. Top 10 des clés racine par taille de valeur --------------------------
if isinstance(data, dict):
    top10 = sorted(data.items(), key=lambda kv: len(str(kv[1])), reverse=True)[:10]
    print("=== TOP 10 des clés par taille de contenu (octets) ===")
    for i, (k, v) in enumerate(top10, 1):
        size = len(str(v))
        print(f"  {i:2d}. {k:40s} → {size:>8,} octets  ({type(v).__name__})")

# %%
# --- 3. Stats par type de codelist --------------------------------------------
def _extract_codes(obj):
    """Extrait une liste de codes depuis un objet ou une liste d'objets."""
    codes = []
    if isinstance(obj, (list, tuple)):
        for item in obj:
            if isinstance(item, (list, tuple)):
                codes.append(str(item[0]))
            elif isinstance(item, dict):
                # Chercher une clé "value", "code", ou la première clé
                for k in ("value", "code", "id", "identifiant"):
                    if k in item:
                        codes.append(str(item[k]))
                        break
                else:
                    # Prendre la première valeur
                    codes.append(str(list(item.values())[0]))
    elif isinstance(obj, dict):
        for v in obj.values():
            codes.extend(_extract_codes(v))
    return codes


# Si la racine est un dict de codelists
if isinstance(data, dict):
    all_codes_by_key = {}
    for k, v in data.items():
        if isinstance(v, dict):
            code_list = []
            codes = []
            for cv in v.values():
                if isinstance(cv, list):
                    code_list.extend(cv)
                    codes.extend(_extract_codes(cv))
            all_codes_by_key[k] = {
                "entry_count": len(code_list),
                "unique_codes": len(set(codes)),
                "codes_sample": codes[:5],
            }

    print("\n=== STRUCTURE DES CODELISTS ===")
    print(f"Codelists trouvées dans la racine: {len(all_codes_by_key)}")

    for i, (k, info) in enumerate(
        sorted(all_codes_by_key.items(),
               key=lambda x: x[1]["entry_count"], reverse=True)[:20],
        1,
    ):
        print(f"\n  {i:2d}. {k:45s} ({info['entry_count']:5d} entrées, {info['unique_codes']:5d} codes uniques)")
        if info["codes_sample"]:
            print(f"       Exemple: {info['codes_sample']}")

# %%
# --- 4. Exploration approfondie ------------------------------------------------
def _explore_nested(obj, path="", max_depth=4, depth=0):
    """Parcours récursif pour identifier les sections intéressantes."""
    if depth > max_depth:
        return

    sections = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_path = f"{path}.{key}" if path else key
            if isinstance(value, (dict, list)):
                depth_of_section = 0
                if isinstance(value, list):
                    depth_of_section = 1
                    if value and isinstance(value[0], (dict, list)):
                        depth_of_section += 1
                elif isinstance(value, dict):
                    all_leaves = all(not isinstance(v, (dict, list)) for v in value.values())
                    if not all_leaves:
                        depth_of_section += 1

                found = list(_explore_nested(value, new_path, max_depth, depth + depth_of_section))
                if found:
                    sections[key] = found
                else:
                    sections[key] = [(new_path, type(value).__name__)]
            else:
                sections[key] = [(new_path, type(value).__name__)]
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):  # Premier échantillon
            new_path = f"{path}[{i}]"
            if isinstance(item, (dict, list)):
                found = list(_explore_nested(item, new_path, max_depth, depth + 1))
                if found:
                    sections[f"[{i}]"] = found
            else:
                sections[f"[{i}]"] = [(new_path, type(item).__name__)]

    if sections:
        yield (path or "(root)", sections)


if isinstance(data, dict):
    print("\n=== EXPLORATION NIVELÉE (max 4 niveaux) ===")
    for path, children in _explore_nested(data, "(root)", max_depth=4):
        if path == "(root)":
            print("\n  root:")
        else:
            print(f"\n  {path}:")
        for key, items in children.items():
            print(f"    ├── {key}:")
            for item in items[:3]:
                if isinstance(item, tuple):
                    print(f"    │   └── {item[0]} ({item[1]})")

