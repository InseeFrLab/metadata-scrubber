# codelist_dedup — Dédoublonnage incrémental des listes de codes DDI 3.3

Dédoublonne les listes de codes (`CodeList`) **opération par opération**, en
construisant un **registre canonique cumulatif**. On traite une opération, on en
sort une liste canonique simplifiée, puis on enrichit ce registre avec
l'opération suivante (les doublons inter-opérations sont rattachés au canon
existant).

**Phase 1** : égalité **exacte** de l'ensemble des paires `(valeur, libellé)`,
plus une couche **quasi-doublons** (similarité floue `SequenceMatcher`, code
réutilisé de `archives/dedoub_deterministe/`, bornée par buckets
`sig_values`/`sig_name`). **Phase 2** : rapprochement **sémantique** par
**embeddings** (candidats par cosinus) + **juge LLM** (confirmation), via un
endpoint OpenAI-compatible — capte les synonymes que la phase 1 rate.

> Tout est **proposé** : rien n'est fusionné ni réécrit dans la base source ;
> on ne produit que des artefacts relisibles.

**Quels champs servent à quoi.** La clé de doublon **exact** est le seul **contenu**
de la liste : l'ensemble normalisé des paires `(<r:Value>, libellé de la <Category>
référencée)`. Le `<CodeListName>` (code technique) et le `<r:Label>` (libellé
humain) de la `CodeList` **n'entrent pas** dans cette clé — sinon deux listes au
même contenu mais nommées différemment (ex. `L_DEP` ↔ `CL-RMES-COG-DEPARTEMENTS-2024`)
ne seraient plus rapprochées. Le `<r:Label>` sert en revanche à l'**affichage** et
à enrichir le texte des couches **floue** et **sémantique** (embeddings + juge).

## Installation


```bash
uv sync                 # crée .venv et installe les dépendances
uv run codelist-dedup --help
```

> Démos *notebook-like* (racine, cellules `# %%`) : `explore.py` illustre les trois
> niveaux de détection **via le paquet** ; `explore_standalone.py` réimplémente tout
> **inline, sans le paquet** (pédagogique, simplifié). `uv run python explore.py`.

Dépendances : `lxml` (extraction en flux), `numpy` (cosinus), `openai` (phase 2),
`pandas`/`pyarrow` (inspect/parquet) ; `rdflib` en groupe *dev* (validation des TTL).
Prérequis d'exécution :
- **AWS CLI configuré** pour lire les sources `s3://` (sinon utiliser des chemins locaux) ;
- pour la phase 2, les variables `OPENAI_BASE_URL` / `OPENAI_API_KEY` (cf. ci-dessous).

## Utilisation

```bash
# 1re opération (registre créé s'il n'existe pas)
uv run codelist-dedup ingest --operation BPE \
    --source s3://projet-metadonnees-rmes/BPE.xml \
    --registry ./registry.sqlite --output-dir ./output

# opérations suivantes → enrichissent le même registre
# --emit-rml ajoute la sortie RML/TTL ; --near-threshold règle le seuil flou (déf. 0.90)
uv run codelist-dedup ingest --operation RP \
    --source s3://projet-metadonnees-rmes/RP.xml \
    --registry ./registry.sqlite --output-dir ./output --emit-rml

# inspecter un canon (membres, contenu, rapprochements sémantiques, décision de revue)
uv run codelist-dedup show --registry ./registry.sqlite --canonical-id cl-9f4d58e620bfe84c

# régénérer les artefacts sans réingérer
uv run codelist-dedup report --operation BPE --registry ./registry.sqlite --output-dir ./output

# PHASE 2 — rapprochement sémantique sur le registre cumulé (embeddings + juge LLM)
uv run codelist-dedup semantic --registry ./registry.sqlite --output-dir ./output \
    --min-cosine 0.92 --max-judgements 50 --emit-rml
# --no-llm : embeddings + candidats seulement (rapide, sans appel au juge)
```

`--source` accepte un chemin local ou une URI `s3://` (lue en flux via
`aws s3 cp`, sans fichier temporaire). `--dry-run` n'écrit rien et affiche juste
le nombre de signatures distinctes.

### Phase 2 — prérequis
Variables d'environnement : `OPENAI_BASE_URL`, `OPENAI_API_KEY`.

| Rôle | Modèle par défaut | Surcharge |
|---|---|---|
| Embeddings | `qwen3-embedding-8b` | `CLD_EMBED_MODEL` / `--embed-model` |
| Juge LLM | `gemma4-26b-moe` *(rapide, ~0,6 s/paire)* | `CLD_JUDGE_MODEL` / `--chat-model` |

Pour un arbitrage plus fin (mais lent, ~20 s/paire), utiliser
`--chat-model qwen3-6-35b-moe` (modèle « thinking »). `diffusiongemma-26b-moe`
n'est pas utilisé. Embeddings et verdicts sont **mis en cache** dans le registre
(les verdicts sont indexés par modèle) → réexécution idempotente.
`--max-judgements` plafonne le nombre d'appels au juge (les candidats non jugés
sont signalés, jamais tronqués en silence). Les étapes longues (embeddings,
jugement) affichent une barre de progression `tqdm`.

## Inspecter un fichier source (diagnostic / EDA)

Indépendant du registre : compte **tous les types d'objets** d'un fichier DDI et
exporte deux parquet pour analyse (pandas).

```bash
uv run codelist-dedup inspect --source s3://projet-metadonnees-rmes/BPE.xml \
    --output-dir ./inspect --sample 3
#   --no-parquet : affiche seulement le récap (comptes + échantillons par type)
```

Écrit `inspect/<op>/objects_<op>.parquet` (1 ligne par objet, tous types :
`type, id, urn, version, source_id, name, label, description, n_codes`) et
`codes_<op>.parquet` (1 ligne par code des CodeList, libellé de catégorie résolu :
`codelist_id, codelist_name, codelist_label, value, code_label, category_id`).

## Parcourir & réviser le registre

```bash
# lister les canons redondants (table par défaut ; --format csv|json ; --limit/--offset)
uv run codelist-dedup list --registry ./registry.sqlite --redundant --sort members
#   filtres : --operation BPE  --min-members 3  --search depart  --no-empty
# lister les paires sémantiques (verdict + décision de revue)
uv run codelist-dedup list --registry ./registry.sqlite --what semantic --confirmed-only

# éditer un canon
uv run codelist-dedup update --registry ./registry.sqlite --canonical-id cl-XXXX \
    --set-label "Liste des départements"
# choisir la liste membre à conserver comme canonique
uv run codelist-dedup update --registry ./registry.sqlite --canonical-id cl-XXXX \
    --set-representative <ddi_id>[:version]

# enregistrer une décision de revue (audit : auteur + horodatage)
uv run codelist-dedup update --registry ./registry.sqlite --canonical-id cl-XXXX \
    --review accepted --note "validé" --author alice
uv run codelist-dedup update --registry ./registry.sqlite --pair cl-A cl-B \
    --review rejected --note "millésimes différents"
```

Les décisions sont stockées dans la table `review` du registre (`accepted` /
`rejected` / `pending`, avec note, auteur, horodatage) et **n'altèrent jamais la
base source DDI**. Elles apparaissent dans `show` et la colonne `review` de `list`.

## Artefacts (`output/<opération>/`)

| Fichier | Contenu |
|---|---|
| `canonical_codelists.csv` / `.json` | Le registre canonique cumulé (`display_name` = code technique, `label` = libellé humain). |
| `mapping_<op>.csv` | Chaque liste source → `canonical_id` + `match_type` (`new` / `intra_op_dup` / `inter_op_dup`). |
| `recommendations_<op>.csv` | Canons redondants triés par priorité = `(n_membres − 1) × n_pairs` (fréquence × impact). |
| `near_duplicates_<op>.csv` | Paires de canons **proches** (score ≥ seuil) détectées par la couche floue → candidats phase 2 (non fusionnés). |
| `rml_codelist_dedup_<op>.ttl` *(avec `--emit-rml`)* | Mapping RML/TTL (schéma de l'archive) : `skos:exactMatch` des doublons → liste canonique à conserver, `skos:closeMatch` des quasi-doublons (et des rapprochements sémantiques confirmés, `ex:CodeListSem`). |
| `semantic_candidates_<op>.csv` *(phase 2)* | Paires proches par embeddings (cosinus) + verdict du juge LLM (`same_concept`, `confidence`, `rationale`). |
| `semantic_summary_<op>.md` *(phase 2)* | Récapitulatif : nombre de doublons sémantiques confirmés, listes concernées, stats de cosinus/confiance, top paires. |

## Architecture

```
model.py      dataclasses (CodeEntry, CodeListRecord)
source.py     flux d'entrée (local ou s3://)
extract.py    lxml.iterparse en flux → CodeListRecord (nom, r:Label, libellés des codes résolus via CategoryReference)
signature.py  normalisation + signatures (sig_pairs primaire, sig_values/sig_name auxiliaires)
registry.py   SQLite : id canonique dérivé du contenu, fold-in idempotent
similarity.py mesures floues portées de l'archive (normalize, text_similarity, jaccard)
dedup.py      quasi-doublons : candidats bornés par buckets + scoring (similarity.py)
semantic.py   phase 2 : embeddings (cosinus) + juge LLM, mis en cache (tables embedding/semantic_verdict)
rml.py        sortie RML/TTL (schéma de l'archive, préfixe rdfs corrigé)
report.py     génération des artefacts
matchers/     exact.py (phase 1) ; semantic.py (interface, impl. dans semantic.py)
inspect.py    diagnostic d'un fichier source : extract_all (tous types) → DataFrames + parquet
cli.py        sous-commandes init / ingest / report / semantic / inspect / list / show / update
```

Le registre est **idempotent** : réingérer une opération n'ajoute ni canon ni
membre en double (identifiant canonique = `cl-` + préfixe du hash de contenu) ;
embeddings et verdicts LLM sont également mis en cache.
