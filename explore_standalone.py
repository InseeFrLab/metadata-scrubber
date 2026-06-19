# %% [markdown]
# # Dédoublonnage des listes de codes DDI — version autonome & pédagogique
#
# Script *notebook-like* (cellules `# %%`) qui **n'utilise PAS** le paquet
# `codelist_dedup` : tout est réimplémenté inline et simplifié, pour **expliquer
# chaque étape** :
#
# 1. lire & parser le XML ;
# 2. extraire les listes de codes (avec résolution des libellés) ;
# 3. calculer une **signature** de contenu ;
# 4. **Phase 1 — exact** (même contenu) ;
# 5. **Phase 1 — quasi-doublons** (similarité floue) ;
# 6. **Phase 2 — sémantique** (embeddings + juge LLM).
#
# Simplifications volontaires (vs le paquet) : on **charge tout le fichier** en
# mémoire (`etree.fromstring`) au lieu de streamer ; on compare par `localname` ;
# on reste sur des dict/listes Python (pas de SQLite) ; la phase 1 floue est en
# O(n²) sur toutes les paires. C'est limpide et suffisant pour un petit fichier
# (BPE) — à ne pas appliquer tel quel à RP (162 Mo).

# %%
# --- Imports & paramètres ---------------------------------------------------
import re
import subprocess
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from itertools import combinations

from lxml import etree

SOURCE = "s3://projet-metadonnees-rmes/BPE.xml"  # ou un chemin local
RUN_LLM = True  # False pour sauter la phase 2 (appels réseau)


def read_bytes(src: str) -> bytes:
    """Lit la source : objet S3 (via aws CLI) ou fichier local."""
    if src.startswith("s3://"):
        return subprocess.run(
            ["aws", "s3", "cp", src, "-"], capture_output=True, check=True
        ).stdout
    with open(src, "rb") as f:
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
        categories[text_of(o, "ID")] = localized(o, "Label") or localized(
            o, "CategoryName"
        )

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
ex = codelists[10]
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
    print(
        f"  {len(g)}× {g[0]['name']}  ({len(g[0]['codes'])} codes) "
        f"→ {[c['id'][:8] for c in g]}"
    )

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
for score, na, nb in near[:10]:
    print(f"  {score:.3f}  {na}  ↔  {nb}")

# Petite illustration de la mesure
print("\ntext_similarity :")
print(
    "  ",
    SequenceMatcher(
        None,
        normalize("Liste des départements"),
        normalize("Liste de codes DEPARTEMENTS"),
    ).ratio(),
)

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

    client = OpenAI(
        base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"]
    )

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
