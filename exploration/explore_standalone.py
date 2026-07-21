# %% [markdown]
# # Dédoublonnage des listes de codes DDI
#
# 1. lire & parser le XML ;
# 2. extraire les listes de codes (avec résolution des libellés) ;
# 3. calculer une **signature** de contenu ;
# 4. **Phase 1 — exact** (même contenu) ;
# 5. **Phase 1 — quasi-doublons** (similarité floue) ;
# 6. **Phase 2 — sémantique** (embeddings + juge LLM).
# 7. **Utilisation des variables**


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

SOURCE = "s3://projet-metadonnees-rmes/BTS.xml"  # ou un chemin local
RUN_LLM = True  # False pour sauter la phase 2 (appels réseau)

# Preferred: create the shared AWS profile once, then reuse it here.
fs = s3fs.S3FileSystem(
    # profile="default",
    endpoint_url="https://minio.lab.sspcloud.fr",
    client_kwargs={"region_name": "us-east-1"},
)

print(fs.ls("projet-metadonnees-rmes"))


def read_bytes(src: str) -> bytes:
    """Lit la source : objet S3 (via aws CLI) ou fichier local."""
    with fs.open(src, "rb") as f:
        return f.read()


# %% [markdown]
# ## 1. Lire & parser le XML
#
# Un fichier DDI 3.3 est une suite de `<ddi:Fragment>`, chacun enveloppant **un**
# objet (`CodeList`, `Category`, `Variable`, `RepresentedVariable`, …). Les noms
# d'objets sont dans le namespace `logicalproduct`, les champs (`r:ID`, `r:Label`,
# `r:Value`…) dans `reusable`. Pour simplifier, on ignore les namespaces et on
# compare le **nom local** des balises.

# %%
root = etree.fromstring(read_bytes(SOURCE))


def local(el) -> str:
    """Nom de balise sans namespace, ex. '{...}CodeList' -> 'CodeList'."""
    return etree.QName(el).localname


def child(el, name):
    """Premier enfant direct dont le nom local est `name` (ou None)."""
    for c in el:
        if local(c) == name:
            return c
    return None


def text_of(el, name) -> str:
    """Texte d'un enfant direct (ex. r:ID, r:Value)."""
    c = child(el, name)
    return (c.text or "").strip() if c is not None else ""


def localized(el, name) -> str:
    """Texte d'un conteneur (CodeListName, r:Label…) -> son r:String / r:Content."""
    c = child(el, name)
    if c is None:
        return ""
    kids = list(c)
    return (kids[0].text or "").strip() if kids else (c.text or "").strip()


# Tous les objets = enfants des Fragments ; on les compte par type.
objects = [obj for frag in root.iter() if local(frag) == "Fragment" for obj in frag]
counts = Counter(local(o) for o in objects)
print("Objets par type :")
for otype, n in counts.most_common():
    print(f"  {otype:26} {n}")

# %% [markdown]
# ## 2. Extraire les listes de codes
#
# Un code (`<Code>`) porte sa **valeur** (`r:Value`) et une **référence** vers une
# `Category` (`r:CategoryReference/r:ID`) — **c'est la catégorie qui porte le
# libellé**. On indexe donc d'abord les catégories, puis on résout chaque code.

# %%
# Index {id_catégorie: libellé}
categories = {}
for o in objects:
    if local(o) == "Category":
        categories[text_of(o, "ID")] = localized(o, "Label") or localized(o, "CategoryName")

# Listes de codes, avec libellés résolus
codelists = []
for o in objects:
    if local(o) != "CodeList":
        continue
    codes = []
    for code in o.iter():  # .iter() => inclut les codes imbriqués
        if local(code) != "Code":
            continue
        value = text_of(code, "Value")
        ref = child(code, "CategoryReference")
        cat_id = text_of(ref, "ID") if ref is not None else ""
        codes.append((value, categories.get(cat_id, "")))
    codelists.append(
        {
            "id": text_of(o, "ID"),
            "name": localized(o, "CodeListName"),
            "label": localized(o, "Label"),
            "codes": codes,
        }
    )

n_codes = sum(len(cl["codes"]) for cl in codelists)
print(f"{len(codelists)} listes de codes extraites ({n_codes} codes au total).")
ex = codelists[random.randint(0, len(codelists))]
print(f"\nExemple : {ex['name']} — {ex['label']}  ({len(ex['codes'])} codes)")
for value, lbl in ex["codes"][:5]:
    print(f"    {value} → {lbl}")

# %% [markdown]
# ## 3. Normalisation + signature de contenu
#
# Deux listes sont des **doublons exacts** si elles ont le **même ensemble de
# paires `(valeur, libellé)`** — peu importe le nom, l'ordre, la casse ou les
# espaces. La *signature* capture exactement ça. Le nom/identifiant de la liste
# est **volontairement exclu** (sinon deux listes identiques nommées différemment
# ne seraient pas reconnues).


# %%
def normalize(txt: str) -> str:
    txt = unicodedata.normalize("NFC", txt or "")
    txt = re.sub(r"\s+", " ", txt).strip().lower()
    return txt


def signature(codes) -> tuple:
    """Ensemble trié & dédupliqué des paires (valeur, libellé) normalisées."""
    return tuple(sorted({(normalize(v), normalize(lbl)) for v, lbl in codes}))


for cl in codelists:
    cl["sig"] = signature(cl["codes"])

print("Exemple de signature (3 1res paires) :", ex.get("name"))
print("  ", signature(ex["codes"])[:3])

# %% [markdown]
# ## 4. Phase 1 — doublons **EXACTS**
#
# Il suffit de **grouper par signature** : toute signature partagée par ≥ 2 listes
# révèle des doublons parfaits.

# %%
groups = {}
for cl in codelists:
    groups.setdefault(cl["sig"], []).append(cl)

exact_dups = {sig: g for sig, g in groups.items() if len(g) > 1}
print(
    f"{len(codelists)} listes → {len(groups)} signatures distinctes "
    f"({len(codelists) - len(groups)} doublons exacts)."
)
print(f"\n{len(exact_dups)} groupes de doublons exacts :")
for g in sorted(exact_dups.values(), key=len, reverse=True)[:8]:
    print(f"  {len(g)}× {g[0]['name']}  ({len(g[0]['codes'])} codes) → {[c['id'][:8] for c in g]}")

# %% [markdown]
# ## 5. Phase 1 — **QUASI-doublons** (similarité floue)
#
# Pour les listes *proches* mais non identiques (variantes de libellé, un code en
# plus…), on compare le **texte concaténé** avec `difflib.SequenceMatcher`. On ne
# garde qu'**un représentant par signature** (pour ne pas recomparer les doublons
# exacts), puis on calcule toutes les paires (O(n²), OK car BPE est petit).


# %%
def concat(cl) -> str:
    parts = [cl["name"], cl["label"]]
    parts += [f"{v}: {lbl}" for v, lbl in cl["codes"]]
    return normalize(" ".join(parts))


uniques = [g[0] for g in groups.values()]  # 1 liste par signature
texts = {cl["id"]: concat(cl) for cl in uniques}

near = []
for a, b in combinations(uniques, 2):
    score = SequenceMatcher(None, texts[a["id"]], texts[b["id"]]).ratio()
    if score >= 0.90:
        near.append((score, a["name"], b["name"]))

near.sort(reverse=True)
print(f"{len(near)} paires quasi-identiques (score ≥ 0.90) :")
for score, na, nb in near:
    print(f"  {score:.3f}  {na}  ↔  {nb}")


# %% [markdown]
# ## 6. Phase 2 — rapprochement **SÉMANTIQUE** (embeddings + juge LLM)
#
# Les phases 1 ratent les synonymes (codes/libellés différents, même concept). On
# vectorise chaque liste (embeddings), on rapproche par **cosinus**, puis un **LLM**
# tranche. Endpoint OpenAI-compatible via les variables d'environnement.

# %%
if RUN_LLM:
    import json
    import os

    import numpy as np
    from openai import OpenAI

    client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"])

    # 1) Embeddings de chaque liste unique (texte = nom + label + codes, tronqué)
    items = uniques
    vecs = []
    B = 64
    for i in range(0, len(items), B):
        chunk = [texts[cl["id"]][:8000] for cl in items[i : i + B]]
        resp = client.embeddings.create(model="qwen3-embedding-8b", input=chunk)
        vecs += [d.embedding for d in resp.data]
    M = np.array(vecs, dtype="float32")
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    sims = M @ M.T  # cosinus (vecteurs normés)

    # 2) Meilleures paires (cosinus ≥ 0.90), hors paires triviales
    cand = []
    for i, j in combinations(range(len(items)), 2):
        if sims[i, j] >= 0.90:
            cand.append((float(sims[i, j]), i, j))
    cand.sort(reverse=True)
    print(f"{len(cand)} paires candidates (cosinus ≥ 0.90). Jugement LLM des 5 1res :")

    # 3) Juge LLM (gemma4) sur quelques paires
    SYS = (
        "Deux listes de codes décrivent-elles le MÊME concept ? Réponds "
        'STRICTEMENT en JSON {"meme_concept": bool, "confiance": 0..1, "raison": "..."}.'
    )
    for score, i, j in cand[:5]:
        a, b = items[i], items[j]
        prompt = f"Liste A ({a['name']}): {texts[a['id']][:1500]}\n\nListe B ({b['name']}): {texts[b['id']][:1500]}"
        out = (
            client.chat.completions.create(
                model="gemma4-26b-moe",
                messages=[
                    {"role": "system", "content": SYS},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=400,
            )
            .choices[0]
            .message.content
            or ""
        )
        m = re.search(r"\{.*\}", out, re.DOTALL)
        verdict = json.loads(m.group(0)) if m else {}
        print(f"  cos {score:.3f}  {a['name']} ↔ {b['name']}  → {verdict}")
else:
    print("RUN_LLM = False → phase 2 sautée.")
# %%
uniques

# %% [markdown]
# ## 7. Lien **Variables → CodeLists** comme signal de dédoublonnage
#
# Les `RepresentedVariable` (et `Variable`) référencent une `CodeList` via
# `r:CodeRepresentation/r:CodeListReference/r:ID`. Ce lien donne un signal
# métier fort : deux listes de codes utilisées par les **mêmes variables** ont
# toutes les chances de décrire le même concept — même si leurs libellés diffèrent
# légèrement.
#
# On construit :
# * `codelist_to_vars` : index `{codelist_id → [noms de variables]}` ;
# * `var_sig` : signature d'usage = ensemble trié des variables qui pointent vers
#   la liste — deux listes avec la même `var_sig` partagent exactement le même
#   périmètre d'utilisation.
#
# Ensuite on croise avec les groupes de quasi-doublons (phase 1 floue) pour voir
# si le signal variable confirme ou infirme les rapprochements.

# %%
# --- 7a. Extraction des références Variable → CodeList ---
codelist_to_vars: dict[str, list[str]] = {}
for o in objects:
    if local(o) not in ("Variable", "RepresentedVariable"):
        continue
    var_name = (
        localized(o, "VariableName") or localized(o, "RepresentedVariableName") or text_of(o, "ID")
    )
    code_repr = child(o, "CodeRepresentation")
    if code_repr is None:
        continue
    cl_ref = child(code_repr, "CodeListReference")
    if cl_ref is None:
        continue
    cl_id = text_of(cl_ref, "ID")
    if cl_id:
        codelist_to_vars.setdefault(cl_id, []).append(var_name)

n_linked = sum(1 for cl in codelists if cl["id"] in codelist_to_vars)
print(f"{n_linked}/{len(codelists)} listes de codes référencées par au moins une variable.")

# Attacher les variables à chaque codelist dict (sans modifier l'existant)
for cl in codelists:
    cl["vars"] = codelist_to_vars.get(cl["id"], [])

# Exemple : les 3 listes avec le plus de variables pointant dessus
top = sorted(codelists, key=lambda c: len(c["vars"]), reverse=True)[:3]
print("\nTop listes par nombre de variables référentes :")
for cl in top:
    print(f"  {cl['name']} ({len(cl['vars'])} vars) : {cl['vars'][:4]}")

# %%
# --- 7b. Signature d'usage (var_sig) ---
# Deux listes avec la même var_sig sont utilisées dans exactement le même contexte.
for cl in codelists:
    cl["var_sig"] = tuple(sorted(set(cl["vars"])))

var_sig_groups: dict[tuple, list] = {}
for cl in codelists:
    if cl["var_sig"]:  # ignorer les listes sans variable
        var_sig_groups.setdefault(cl["var_sig"], []).append(cl)

shared_usage = {sig: g for sig, g in var_sig_groups.items() if len(g) > 1}
print(
    f"\n{len(shared_usage)} groupes de listes partageant exactement le même ensemble de variables :"
)
for sig, g in sorted(shared_usage.items(), key=lambda x: len(x[1]), reverse=True)[:8]:
    names = [c["name"] for c in g]
    print(f"  {len(g)}× vars={list(sig)[:3]}{'…' if len(sig) > 3 else ''} → {names}")

# %%
# --- 7c. Croisement avec les quasi-doublons (phase 1 floue) ---
# Pour chaque paire quasi-identique, afficher si elles partagent les mêmes variables.
print("\nCroisement quasi-doublons × signal variable :")
if not near:
    print("  (aucun quasi-doublon détecté)")
else:
    cl_by_name = {cl["name"]: cl for cl in codelists}
    for score, na, nb in near:
        ca = cl_by_name.get(na)
        cb = cl_by_name.get(nb)
        if ca is None or cb is None:
            continue
        vars_a = set(ca["vars"])
        vars_b = set(cb["vars"])
        shared = vars_a & vars_b
        only_a = vars_a - vars_b
        only_b = vars_b - vars_a
        same_usage = vars_a == vars_b and bool(vars_a)
        marker = (
            "✓ même usage"
            if same_usage
            else ("~ usage partiel" if shared else "✗ usages disjoints")
        )
        print(
            f"  {score:.3f}  {na} ↔ {nb}  [{marker}]"
            f"\n    commun={sorted(shared)[:3]}  A seul={sorted(only_a)[:3]}  B seul={sorted(only_b)[:3]}"
        )

# %% [markdown]
# ## 8. Rapprochement sémantique des **variables** → candidats de fusion de CodeLists
#
# Idée inversée par rapport à la phase 2 : au lieu de comparer les listes de
# codes directement, on compare les **variables** qui les utilisent. Deux variables
# dont les noms/labels sont sémantiquement proches mais qui pointent vers des
# **CodeLists différentes** signalent un doublon potentiel de listes de codes.
#
# Pipeline :
# 1. Construire un enregistrement par variable (nom + label + codelist référencée).
# 2. Vectoriser (embeddings) chaque variable.
# 3. Trouver les paires de variables à cosinus ≥ seuil.
# 4. Retenir les paires qui pointent vers des CodeLists **distinctes** → ce sont
#    les candidats CodeList à fusionner.
# 5. Dédupliquer les candidats CodeList et les trier par score variable maximal.

# %%
if RUN_LLM:
    import json
    import os

    import numpy as np
    from openai import OpenAI

    if "client" not in dir():
        client = OpenAI(
            base_url=os.environ["OPENAI_BASE_URL"],
            api_key=os.environ["OPENAI_API_KEY"],
        )

    # --- 8a. Construire les enregistrements de variables ---
    # Un enregistrement par (variable, codelist) : on ne garde que les variables
    # qui référencent une codelist connue.
    cl_index = {cl["id"]: cl for cl in codelists}

    var_records = []
    for o in objects:
        if local(o) not in ("Variable", "RepresentedVariable"):
            continue
        var_name = (
            localized(o, "VariableName")
            or localized(o, "RepresentedVariableName")
            or text_of(o, "ID")
        )
        var_label = localized(o, "Label") or localized(o, "Description") or ""
        code_repr = child(o, "CodeRepresentation")
        if code_repr is None:
            continue
        cl_ref = child(code_repr, "CodeListReference")
        if cl_ref is None:
            continue
        cl_id = text_of(cl_ref, "ID")
        if cl_id not in cl_index:
            continue
        var_records.append(
            {
                "var_name": var_name,
                "var_label": var_label,
                "cl_id": cl_id,
                "cl_name": cl_index[cl_id]["name"],
                "text": normalize(f"{var_name} {var_label}"),
            }
        )

    print(f"{len(var_records)} variables avec une référence CodeList connue.")

    # --- 8b. Embeddings des variables ---
    VAR_SIM_THRESHOLD = 0.92
    var_texts = [r["text"][:4000] for r in var_records]
    var_vecs = []
    B = 64
    for i in range(0, len(var_texts), B):
        resp = client.embeddings.create(model="qwen3-embedding-8b", input=var_texts[i : i + B])
        var_vecs += [d.embedding for d in resp.data]

    V = np.array(var_vecs, dtype="float32")
    V /= np.linalg.norm(V, axis=1, keepdims=True)
    var_sims = V @ V.T

    # --- 8c. Paires de variables similaires pointant vers des CodeLists différentes ---
    cl_candidates: dict[tuple[str, str], float] = {}  # (cl_id_a, cl_id_b) → max score
    var_pairs_found = []
    for i, j in combinations(range(len(var_records)), 2):
        score = float(var_sims[i, j])
        if score < VAR_SIM_THRESHOLD:
            continue
        ra, rb = var_records[i], var_records[j]
        if ra["cl_id"] == rb["cl_id"]:
            continue  # même CodeList → pas intéressant ici
        # Canonicaliser la paire CodeList (ordre alphabétique sur l'id)
        key = (min(ra["cl_id"], rb["cl_id"]), max(ra["cl_id"], rb["cl_id"]))
        cl_candidates[key] = max(cl_candidates.get(key, 0.0), score)
        var_pairs_found.append(
            (score, ra["var_name"], rb["var_name"], ra["cl_name"], rb["cl_name"])
        )

    var_pairs_found.sort(reverse=True)
    print(
        f"\n{len(var_pairs_found)} paires de variables similaires (cos ≥ {VAR_SIM_THRESHOLD}) "
        f"→ {len(cl_candidates)} paires de CodeLists candidates à la fusion :"
    )

    # --- 8d. Afficher les candidats CodeList triés par meilleur score variable ---
    cl_candidate_list = sorted(cl_candidates.items(), key=lambda x: x[1], reverse=True)
    for (id_a, id_b), best_score in cl_candidate_list[:10]:
        cla = cl_index[id_a]
        clb = cl_index[id_b]
        # Déjà détectés en phase 1 ?
        already = cla["sig"] == clb["sig"]
        marker = " [déjà exact]" if already else ""
        print(
            f"  cos_var={best_score:.3f}  {cla['name']} ↔ {clb['name']}{marker}"
            f"\n    {len(cla['codes'])} codes  vs  {len(clb['codes'])} codes"
        )

    # --- 8e. Paires de variables les plus proches (top 10) pour inspection ---
    print("\nTop paires de variables similaires :")
    for score, vna, vnb, cna, cnb in var_pairs_found[:10]:
        same_cl = cna == cnb
        print(f"  {score:.3f}  {vna} ↔ {vnb}  (CodeLists : {cna} / {cnb})")

else:
    print("RUN_LLM = False → section 8 (embeddings variables) sautée.")

# %%
