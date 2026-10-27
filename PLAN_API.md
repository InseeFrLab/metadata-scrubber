# Dédoublonnage des CodeLists — diagnostic et évolution via l'API Métadonnées V2

## Contexte

Objectif métier :
1. **Nettoyer la base** et la structurer en 3 niveaux de CodeLists : nomenclatures officielles /
   listes communes gérées par la division métadonnées / listes spécifiques à une enquête.
2. **Pré-détecter les doublons à l'ajout d'une opération**, avant publication.

Livrables de ce plan :
- l'appli de dédoublonnage alimentée par l'API Insee (phases A, B) ;
- un **service publiable `scrubber-check`** (CLI + API HTTP) : un fichier DDI en entrée, ses doublons
  parmi les CodeLists publiées dans l'API en sortie (phase C) ;
- une appli web plus sûre et plus rapide à utiliser, apparence conservée (phase U).

---

## 1. Répond-on au besoin aujourd'hui ? — Partiellement

**Acquis (à conserver)** : entonnoir de détection robuste *dans un fichier* (exact, hors échappement,
libellés, flou symétrique, sémantique, usage), listes suspectes, validation humaine, décisions conservées
(`decisions.json`), registre nettoyé éditable et réinjecté (`--registry`).

**Besoin 1 — nettoyer la base, 3 niveaux**
- Périmètre = un XML exporté à la main. La base n'est jamais vue en entier : d'un run à l'autre, seules
  les listes *approuvées* du registre nettoyé sont comparées ; les listes « en attente » ou « sans doublon »
  d'un run précédent sortent du champ.
- Aucun niveau dans le registre nettoyé (schéma v2 plat).
- Aucune provenance : une liste ne sait pas quelles opérations / séries l'utilisent, alors que c'est le
  critère naturel commune / spécifique.
- Maître = première du groupe dans l'ordre du XML.
- `BasedOnObject` est extrait (`CodeList.based_on`) mais seulement affiché : il n'entre pas dans la détection.
- Pas de détection d'inclusion (A ⊂ B) ; nomenclatures officielles seulement ajoutées à la main.
- Le registre nettoyé perd agence / version → plus de lien vers l'objet Colectica.

**Besoin 2 — pré-détection**
- Pas de mode « contrôler une opération contre la référence » : on lance un run complet qui mélange paires
  internes et paires avec le registre, et laisse des décisions en attente.
- Pas de réponse par liste « utilisez plutôt X ».

---

## 2. Ce que l'API change — explication détaillée

### 2.1 Ce que l'API expose (vérifié par appels réels, publique, sans authentification)

Base : `https://api.insee.fr/metadonnees/V2` (le `servers` d'`openapi.json` pointe vers l'environnement de
développement → URL configurable par `INSEE_METADATA_API_URL`).

Chaîne de navigation, de l'opération à la liste de codes :

```
/operations/series                       → ~200 séries (seriesId)
/operations/serie/{s}                    → operations[], famille
/structures/operation/{op}/fichiers      → StudyUnit DDI 3.3 : PhysicalInstanceReference[]
/structures/fichier/{agency}/{id}/{v}    → Variable[] avec CodeListReference(agency, id, version)
/structures/codelist/{agency}/{id}/{v}   → CodeList + Code[] + Category[] (fragments DDI 3.3)
```

À côté :
- `/geo/{regions, departements, pays, communes, arrondissements, zonesDEmploi2020, unitesUrbaines2020,
  airesDAttractionDesVilles2020, bassinsDeVie2022, quartiersPrioritairesDeLaPolitiqueDeLaVille2024…}?date=`
  → listes complètes `code` / `intitule` = nomenclatures officielles géographiques ;
- `/codes/{nomenclature}/{niveau}/{code}` → libellé d'**un** poste (cj, naf2025, nafr2, pcs2020, coicop…) :
  on ne peut pas télécharger la nomenclature entière, mais on peut **tester** si une liste en est une.

Les fragments renvoyés ont exactement la forme que lit déjà `extractor.parse_xml` (enfants de `Fragment`).
`extract_codelists` / `extract_variables` fonctionnent tels quels (vérifié sur `s2219` et `s2297`).

### 2.2 Ce que les données de l'API révèlent (mesuré sur la série BTS `s1998`)

| Constat | Mesure | Conséquence |
|---|---|---|
| Chaque millésime recrée ses listes | BTS 2023 (`s2219`, 51 listes) et BTS 2024 (`s2297`, 47 listes) : **0 identifiant commun** | la duplication vient surtout de la reconduction d'un millésime à l'autre |
| Les copies sont tracées | **40/47** listes 2024 ont un `BasedOnObject` vers une liste 2023 | signal déterministe de « même lignée », gratuit, aujourd'hui inexploité |
| Beaucoup de copies sont strictement identiques | **27/47** listes 2024 ont le contenu exact d'une liste 2023 | doublons exacts inter-millésimes, invisibles tant qu'on charge un fichier par run |
| Couverture partielle | sur 9 opérations BTS, 2 ont des fichiers DDI ; 6 renvoient un StudyUnit vide, 1 un 404 | l'API ne couvre que les opérations documentées et publiées |
| Versions | 25/25 listes échantillonnées : version référencée = dernière version | on peut suivre les références versionnées sans perdre la dernière version |

### 2.3 Changements induits, étape par étape du pipeline

| Étape | Aujourd'hui | Avec l'API | Effet sur le besoin |
|---|---|---|---|
| **Entrée** | un XML exporté (`BTS.xml`), lu par `read_bytes` | une opération, une série ou toute la base, téléchargées puis assemblées (`corpus.py`) ; XML local toujours accepté | plus d'export manuel ; la base peut être traitée en un seul run |
| **Identité d'une liste** | `id` (+ `version`, `agency` lus mais non conservés au registre) | triplet `agency/id/version` = URN, conservé jusqu'au registre nettoyé | on peut retrouver l'objet Colectica, et à terme le corriger |
| **Provenance** | aucune ; `vars` = noms de variables du fichier | `operations`, `series`, `famille` de chaque liste, déduits du chemin série → opération → fichier → variable → liste | base de calcul des niveaux commune / spécifique et de la réutilisation |
| **Rattachement variables** | par id de liste, dans le seul fichier | par référence versionnée, sur tout le corpus | nombre de variables / d'opérations utilisatrices réel ; listes non référencées détectables |
| **Versions** | `latest_versions` garde la plus récente par id | idem, mais on sait quelle version chaque variable référence | évite de comparer une version obsolète |
| **Filiation (`BasedOnObject`)** | affichée seulement | nouvelle détection `lignee` : liste ↔ liste dont elle est copiée, avec le diff de codes | regroupe d'emblée les reconductions de millésime (40/47 sur BTS) |
| **Nomenclatures officielles** | ajoutées à la main au registre | amorcées depuis `/geo/*` (codes + intitulés datés) ; reconnaissance des listes CJ / NAF / PCS par sondage `/codes/…` | niveau « officielle » rempli automatiquement, cible des listes « Voir dictionnaire » |
| **Choix de la maître** | première du groupe dans l'ordre du XML | niveau > choix de l'expert > nb de séries > nb de variables > ordre | la maître proposée est la liste de référence la plus pertinente |
| **Niveaux** | inexistants | suggestion : officielle (registre / doublon d'une officielle), commune (≥ 2 séries), spécifique (1 série) ; validation par l'expert | réponse directe au besoin 1 |
| **Référence pour la pré-détection** | registre nettoyé seulement | registre à 3 niveaux, éventuellement complété par le corpus API | réponse au besoin 2 |
| **Coût** | aucun appel réseau | ≈ 1 + nb fichiers + nb listes appels par opération (BTS 2023 : 1 + 4 + 51), mis en cache par `agency/id/version` (objets versionnés = immuables) | runs suivants quasi gratuits |

### 2.4 Limites de l'API à garder en tête
- Seules les opérations **publiées** et documentées en DDI sont visibles → pour une opération en cours
  d'ajout, on part de son XML (export Colectica / Bauhaus-Variables) comparé à une référence venant de l'API.
- Pas de liste complète des nomenclatures non géographiques (NAF, PCS…) : seulement un test poste par
  poste, donc une **reconnaissance** et non un import. Les CodeLists officielles déjà présentes dans
  Colectica s'importent par leur id (`/structures/codelist/{agency}/{id}`).
- Opérations anciennes sans DDI : aucune information, à signaler dans `run_summary.json`.
- L'API est en lecture seule : les corrections dans Colectica (RegisterOrReplace, DELETE) restent hors
  périmètre, comme décidé dans les observations.

---

## 3. Plan d'implémentation

### Phase A — Source « API » et provenance
- **`src/metadata_scrubber/insee_api.py`** (client `httpx`, à déclarer dans `pyproject.toml`) :
  `list_series()`, `serie_operations(s)`, `operation_info(op)` (JSON) ;
  `operation_fragments(op)` qui suit la chaîne du § 2.1 et renvoie les fragments XML.
  Cache `<audit_dir>/api_cache/<agency>/<id>/<version>.xml` (via `s3.make_s3_filesystem` ou fichier local).
  StudyUnit vide ou 404 → opération ignorée et comptée.
- **`corpus.py`** : `build_corpus(op_ids) -> (objects, provenance)` ; concatène les fragments (dédoublonnés
  par `agency/id/version`) puis réutilise `extract_codelists` / `extract_variables` sans les modifier.
- **`types.CodeList`** : `operations: set[str]`, `series: set[str]`, `urn: str`.
- **`main.py`** : `xml_source` **ou** `--operation sXXXX` (répétable) / `--serie sXXXX` / `--all-operations`.
  Seule l'étape 1 change (`read_bytes` + `parse_xml` → `build_corpus`).
- **Lignée** : `funnel.detect_lineage(codelists)` sur `based_on` → candidats `lignee` (confiance selon le
  diff de codes), traités dans `main._build_candidates` avant le flou.
- **Listes non référencées** : avec `--all-operations`, export `unreferenced_codelists.json` (listes du
  corpus sans variable référente).
- **Registre des doublons** (`reporting/duplicates_registry._serialize_minimal`) : `urn`, `operations`,
  `series`, nb de séries → affichés dans l'UI.

### Phase B — Les 3 niveaux et le choix de la maître
- **Registre nettoyé v3** (`cleaned_registry.py`, migration v2 → v3 dans `migrate_cleaned_doc`) :
  `level` (`officielle` | `commune` | `specifique` | `""`), `source` (`api-geo`, `api-structures`,
  `validation`, `manuel`), `agency`, `version`, `urn`, `operations`, `series`. Renseignés à la création,
  jamais écrasés ensuite (même règle que name / label / codes). Rechargés par `load_cleaned_codelists`.
- **`levels.py`** : `suggest_level(cl)` et `master_rank(cl)`. `main.py` trie `codelists` avec `master_rank`
  avant la détection : tout le code qui prend `group[0]` comme maître choisit alors la bonne liste, sans
  autre modification.
- **`scrubber seed-officielles [--date] [--codelist agency/uuid …]`** : entrées `officielle` depuis
  `/geo/*` (id `insee-geo:<type>:<date>`) et depuis des CodeLists Colectica désignées.
- **Placeholders** : rapprochement du nom normalisé (`quality.name_key`) avec les officielles →
  suggestion « remplacer par ».
- **Reconnaissance de nomenclature** (optionnelle) : ≤ 5 codes testés sur `/codes/{nom}/{niveau}/{code}`
  quand le format de code correspond ; libellés comparés avec `normalize`.
- **UI** (`ui/static/js/registry.js`, `cleaned.js`, `ui/services/registry_service.py`, `ui/server.py`) :
  badge et filtre de niveau, sélecteur de niveau dans le registre nettoyé, source (opération / XML) dans
  l'onglet Pipeline.

### Phase C — Service de contrôle publiable : `scrubber-check` (CLI + API)

Besoin : **à partir d'un fichier DDI fourni en entrée, détecter ses doublons parmi les CodeLists publiées
dans l'API Insee**. Choix validés : même dépôt, **service séparé** (commande, API HTTP, image et
déploiement propres) ; référence par défaut = **toute la base publiée**, avec option pour restreindre.

#### Principe
```
                 (hors ligne, quotidien)                         (à la demande)
API Insee ──► scrubber-check index build ──► reference_index.json.gz ──┐
  séries → opérations → fichiers → CodeLists                            ├─► moteur de contrôle ──► rapport
  + /geo/* (officielles)                     fichier.xml fourni ───────┘   (nouvelle × référence)
```
L'API Insee n'offre ni recherche ni liste globale des CodeLists : on ne peut pas l'interroger « à la
volée » pour chaque fichier (des milliers d'appels). On construit donc un **index de référence** une fois,
on le met en cache, et chaque contrôle ne compare que le fichier fourni à cet index.

#### Sous-paquet `src/metadata_scrubber/check/`
- **`index.py`** — construction et chargement de l'index :
  - `build_index(scope)` : parcourt `/operations/series` → `/operations/serie/{s}` → opérations →
    `/structures/operation/{op}/fichiers` → fichiers → CodeLists (via `insee_api.py` et `corpus.py` de la
    phase A, cache par `agency/id/version` : une reconstruction ne retélécharge que les nouvelles versions),
    plus les listes géographiques `/geo/*` marquées `officielle`.
  - Contenu par CodeList : `urn`, `name`, `label`, `codes`, `sig`, `sig_business`, `sig_labels`,
    `quality_flags`, `based_on`, `operations`, `series`, `level_suggere` (officielle / commune si ≥ 2 séries /
    spécifique). En-tête : date de construction, URL de l'API, nb de séries / opérations / listes,
    opérations sans DDI.
  - Stockage `reference_index.json.gz` en local ou S3 (`registry_io` / `s3.py`) ; chemin via
    `SCRUBBER_CHECK_INDEX`.
  - `load_index(path)` reconstruit les `CodeList` et des **index inversés** en mémoire :
    `sig` → listes, `sig_business` → listes, `sig_labels` → listes, valeur de code → listes,
    libellé de code → listes.
- **`engine.py`** — `check(codelists_fournies, index, scope=None) -> CheckReport` :
  - signatures calculées par une fonction extraite de `main.py` (étape 4 : `quality_flags`, `sig`,
    `sig_business`, `sig_labels`, cas placeholder) → `funnel.prepare_signatures(codelists)`, appelée aussi
    par `main.py` (pas de duplication) ;
  - pour chaque liste fournie, dans l'ordre de l'entonnoir :
    1. `reutilisee` : même `agency/id` présent dans l'index (la liste existe déjà, c'est le bon cas) ;
    2. `exact`, `exact_business`, `labels` : recherche directe dans les index inversés (O(1)) ;
    3. `inclusion` (nouvelle fonction `funnel.detect_inclusions`, `|A∩B|/|A| ≥ 0,95`) ;
    4. `fuzzy` : `funnel._code_similarity` / `_name_similarity` **seulement** sur les listes de l'index
       partageant au moins une valeur ou un libellé de code et compatibles en taille → quelques dizaines de
       comparaisons par liste au lieu de toute la base ;
    5. `lignee` : `BasedOnObject` de la liste fournie pointant vers une liste de l'index ;
    6. doublons **internes** au fichier (mêmes fonctions de `funnel.py` sur les seules listes fournies).
  - `scope` = séries / opérations : filtre l'index avant comparaison.
  - Pas de LLM (service léger, déterministe, rapide) ; l'étape sémantique reste dans le pipeline complet.
- **`report.py`** — rapport par liste fournie : `statut` (`reutilisee` | `doublon` | `inclusion` | `proche`
  | `nouvelle`), jusqu'à 3 correspondances triées par niveau puis confiance, chacune avec `urn`, nom, niveau,
  séries / opérations utilisatrices, type de détection, confiance, diff (`codes_en_plus`, `codes_en_moins`,
  `libelles_differents`), **action suggérée** (« utiliser la nomenclature officielle X », « réutiliser la
  liste commune Y », « liste identique dans la série S : réutiliser plutôt que recréer »), `quality_flags`.
  Synthèse : nb de listes par statut, version et date de l'index. Formats `json`, `csv`, `table` (terminal).
- **`cli.py`** — commande `scrubber-check` :
  ```
  scrubber-check fichier.xml [--index PATH|s3://…] [--serie s1998 …] [--operation s2219 …]
                 [--format table|json|csv] [--output rapport.json] [--fail-on doublon|proche|never]
  scrubber-check index build [--output PATH] [--serie …]     # construction / mise à jour
  scrubber-check index info  [--index PATH]                  # date, volumes, opérations sans DDI
  ```
  Codes de sortie : 0 = rien à signaler, 1 = doublons selon `--fail-on`, 2 = erreur → utilisable en CI ou
  avant un chargement Colectica.
- **`api.py`** — application FastAPI **sans IHM**, commande `scrubber-check-api` :
  - `POST /v1/check` : fichier DDI en `multipart/form-data` (ou corps XML brut) ; paramètres `serie`,
    `operation`, `format` ; renvoie le rapport (JSON par défaut, CSV possible) ;
  - `GET /v1/index` : métadonnées de l'index chargé ; `GET /health` ;
  - documentation OpenAPI automatique (`/docs`) ; index chargé une fois au démarrage et rechargé quand le
    fichier change (vérification de date au plus toutes les 5 min) ;
  - authentification optionnelle en réutilisant `ui/auth.py` (même variable de mot de passe).

#### Sécurité et limites (service exposé)
- Parsing XML durci : `etree.XMLParser(resolve_entities=False, no_network=True)` dans
  `extractor.parse_xml` (protège aussi l'appli existante contre les entités externes / XXE).
- Taille maximale du fichier reçu (50 Mo par défaut, `SCRUBBER_CHECK_MAX_MB`), délai maximal par contrôle.
- Le service ne fait **que lire** l'API Insee ; il n'écrit rien dans Colectica.

#### Packaging et déploiement
- `pyproject.toml` : dépendances de base réduites au nécessaire (`lxml`, `httpx`, `fastapi`, `uvicorn`,
  `python-multipart`, `s3fs`) ; extra `web` (`jinja2`, `sse-starlette`, `numpy`, `openai`, `pandas`,
  `pyarrow`, `tqdm`) pour l'appli actuelle. Nouveaux scripts `scrubber-check`, `scrubber-check-api`.
  Le sous-paquet `check` n'importe ni `main.py`, ni `semantic.py`, ni `ui/`.
- `Dockerfile` existant : `uv sync --extra web`. Nouveau `Dockerfile.check` (sans l'extra) ; vérifier que
  le `Dockerfile` actuel installe bien les extras.
- `deploy/check/` : `deployment.yaml`, `service.yaml`, `ingress.yaml`, `cronjob.yaml`
  (`scrubber-check index build` chaque nuit vers S3).
- `.github/workflows/deploy.yml` : construction et publication de la seconde image.

#### Onglet « Pré-contrôle » de l'appli web
Appelle le même `check.engine` (pas de second moteur) : dépôt d'un XML → tableau du rapport (voir U6).
Quand le registre nettoyé (phase B) est disponible, ses niveaux validés priment sur `level_suggere`.

### Réutilisé sans modification
`extractor.parse_xml / extract_codelists / extract_variables / latest_versions`, `quality.*`, `normalize.*`,
`decisions.*`, `run_output.*`, `registry_io.*`, `s3.make_s3_filesystem`.

### Documentation
`doc/note_methodologique_dedoublonnage.qmd` (source API, lignée, niveaux, inclusion),
`doc/guide_utilisateur.qmd` (pré-contrôle, niveaux, raccourcis clavier, sauvegarde automatique),
`doc/modifications_recentes.qmd`, `README.md` ; captures `doc/img/*` à refaire (elles montrent encore
l'ancienne vue en cartes).

---

## 3 bis. Phase U — Ergonomie de l'application web (apparence conservée)

Choix validés : **pas de refonte visuelle** (Bootstrap actuel conservé), **sauvegarde automatique des
décisions**. Indépendante des phases A–C : livrable en premier.

### Problèmes constatés dans le code
- **Perte de travail possible** : les décisions vivent dans une variable globale du serveur
  (`ui/server.py` : `_registry_state`, `_registry_path`) jusqu'au clic « Sauvegarder ». Un redémarrage du
  pod les efface ; un second utilisateur qui charge un autre registre **remplace** celui du premier.
  `updateDecision` (`registry.js`) ne vérifie pas `resp.ok` : un échec affiche quand même « ✅ Décision
  mise à jour ».
- **Chemins S3 tapés à la main** à trois endroits (pipeline, validation, registre nettoyé), sans liste des runs.
- **Décider coûte cher** : liste déroulante (2 clics) par doublon, aucun raccourci clavier, un toast par
  décision, comparaison des codes cachée derrière « Détails ».
- **Actions de masse dangereuses** : `bulk_set_decisions` agit sur **tout** le registre, pas sur le filtre
  affiché ; « Tout rejeter » est irréversible ; confirmations par `confirm()` natif.
- **Bruit visuel** : 4 pastilles par liste, légende en paragraphe, bouton « Filtrer » inutile (les filtres
  s'appliquent déjà en direct), barre de confiance pleine largeur.
- **Rendu non échappé** : `dup.name`, `dup.label`, `dup.vars`, `dup.cat_ids` sont injectés sans
  `escapeHtml` dans `renderDuplicateCard` → affichage cassé si un nom contient `<` ou `&`.
- L'état (run, filtres, liste sélectionnée) est perdu au rechargement de la page.

### U1 — Sauvegarde automatique et état serveur fiable
- `PATCH /api/registry/decision`, `/bulk`, `/dismiss`, `/swap-parent` : après modification, écriture
  immédiate de `decisions.json` (réutiliser `decisions.update_decisions` + `registry_io.write_json_registry`,
  sous `_registry_lock`) ; réponse en erreur si l'écriture échoue.
- `GET /api/registry` réapplique `decisions.apply_decisions` au chargement → un registre rechargé (après
  redémarrage, sur un autre poste) retrouve les dernières décisions.
- État serveur indexé par chemin de registre (`dict[path, state]`) au lieu d'une seule globale ; le client
  envoie le chemin à chaque appel.
- Le bouton « Sauvegarder » devient « Mettre à jour le registre nettoyé » (`write_cleaned_registry` +
  écriture de `codelist_duplicates.json`).
- Côté client : `updateDecision` teste `resp.ok`, rétablit l'ancien état en cas d'échec ; indicateur
  discret dans l'en-tête « Enregistré à 14:32 » / « Échec de l'enregistrement — réessayer ».
- Registre nettoyé (`cleaned.js`) : garde l'enregistrement explicite (édition libre), mais ajoute une
  alerte `beforeunload` quand `cleanedDirty` est vrai.

### U2 — Retrouver ses runs sans taper de chemin
- Nouvelle route `GET /api/runs?root=…` : liste les dossiers datés (`run_output.RUN_DIR_PATTERN`) avec
  leur `run_summary.json` (date, source, statut, nb de candidats, avertissements).
- Un seul réglage « Dossier de sortie » (mémorisé dans `localStorage`) ; les chemins du registre des
  doublons, de `decisions.json` et du registre nettoyé en sont déduits (`cleaned_registry_path`,
  `decisions_path`). Champs de chemin libres conservés derrière « Avancé ».
- Onglet Validation : liste déroulante des runs (le plus récent présélectionné) au lieu du champ texte.
- Fin de pipeline : « Valider ce run » charge directement le registre du run dans l'onglet Validation.
- Onglet Pipeline : historique des runs (même route) avec statut et bouton « Ouvrir ».

### U3 — Décider plus vite
- Remplacer la liste déroulante par un groupe de 3 boutons (Approuver / Rejeter / En attente), état
  courant mis en évidence, icône + texte (pas seulement la couleur).
- Résumé du diff visible sur chaque carte sans ouvrir « Détails » : « 12 codes communs · +2 · −1 · 1 libellé
  différent » (calcul extrait de `buildCodeComparisonHtml`) ; tableau comparatif ouvert d'office pour le
  premier doublon en attente.
- Raccourcis clavier : `j`/`k` liste suivante/précédente, `n` prochaine liste avec doublons en attente,
  `a`/`r`/`p` sur le doublon actif, `↓`/`↑` entre doublons, `?` aide. Désactivés dans les champs de saisie.
- Option « passer à la liste suivante quand tout est décidé ».
- Plus de toast par décision (l'indicateur U1 suffit) ; toasts réservés aux erreurs et aux actions de masse.
- Confiance en pastille « 90 % » à côté des badges de détection au lieu de la barre pleine largeur.

### U4 — Actions de masse sûres
- `POST /api/registry/bulk` accepte les filtres courants (décision, origine, recherche, type de détection)
  et ne modifie que les doublons **affichés** ; il renvoie la liste des décisions précédentes.
- Libellés avec le nombre concerné (« Approuver les 23 doublons exacts affichés »), confirmation par modale
  Bootstrap, toast avec **Annuler** (renvoie les décisions précédentes).
- « Tout rejeter » / « Tout remettre en attente » regroupés dans un menu « Plus d'actions ».
- Fonction commune `confirmDialog()` dans `main.js` qui remplace tous les `confirm()` natifs
  (`registry.js`, `cleaned.js`).

### U5 — Navigation, filtres et lisibilité
- Suppression du bouton « Filtrer » ; filtres, run et liste sélectionnée mémorisés dans l'URL
  (`#run=…&decision=pending&q=…&cl=…`) → rechargement et lien partageable.
- Filtre par défaut « À traiter » (doublons en attente + listes sans doublon ni ajoutées ni écartées) ;
  barre de progression « 128 / 240 décisions » dans les statistiques.
- Colonne de gauche : une seule pastille (nb en attente) + infobulle détaillée ; la légende devient une
  infobulle « ? ».
- Tri de la colonne de gauche : en attente d'abord, confiance max, nom, nb de variables (puis nb de séries
  après la phase A).
- Pagination répétée en bas de liste.

### U6 — Onglets à venir avec les phases A–C
- Pipeline : sélecteur de source « Fichier XML (chemin ou dépôt) / Opération / Série » avec recherche dans
  `/operations/series` ; options avancées (verbeux, dossier de sortie) repliées.
- Validation / registre nettoyé : badge et filtre de niveau (phase B).
- Onglet « Pré-contrôle » (phase C) : dépôt d'un XML ou choix d'une opération → tableau par liste
  (statut : réutilisée / doublon d'une officielle / proche d'une commune / nouvelle), cible suggérée,
  comparaison des codes, export CSV.

### U7 — Robustesse du rendu
- `escapeHtml` sur toutes les valeurs interpolées de `renderDuplicateCard` et de `cleaned.js`.
- Remplacer les `onclick="copyId(...)"` en ligne par des écouteurs délégués.
- `aria-label` sur les boutons à icône seule ; focus visible sur la carte de doublon active.

### Fichiers concernés
`ui/templates/index.html`, `ui/static/js/{main,registry,pipeline,cleaned}.js`, `ui/static/css/style.css`,
`ui/server.py`, `ui/models.py`, `ui/services/registry_service.py` ; réutilise `decisions.py`,
`cleaned_registry.py`, `run_output.py`.

---

## 4. Vérification
- Tests pytest avec réponses API enregistrées dans `tests/fixtures/api/` (`s2219`, `s2297`, un fichier, des
  codelists, `/geo/regions`) et client HTTP simulé, sans réseau en CI :
  `test_insee_api.py` (chaîne, 404 / StudyUnit vide, cache), `test_corpus.py` (provenance, liste partagée),
  `test_lineage.py` (BTS 2023 → 2024), `test_levels.py`, `test_inclusion.py`, `test_precheck.py`
  (mode croisé : pas de paire référence × référence), migration v2 → v3 dans `test_cleaned_registry.py`.
- Service de contrôle : `test_check_index.py` (construction depuis réponses figées, cache par version,
  rechargement), `test_check_engine.py` (fichier de démo `demo/BPE_demo.xml` contre un petit index :
  réutilisée / exact / hors échappement / libellés / inclusion / proche / nouvelle / interne ; filtre
  `scope`), `test_check_cli.py` (formats, codes de sortie selon `--fail-on`), `test_check_api.py`
  (`TestClient` : envoi d'un fichier, taille maximale, XML avec entité externe refusé / non résolu).
- Phase U (`tests/test_registry_routes.py` étendu) : une décision PATCH écrit `decisions.json` ; un
  rechargement réapplique les décisions ; deux chemins de registre chargés en parallèle ne se mélangent
  pas ; `bulk` ne touche que le filtre et renvoie l'état précédent (annulation) ; `GET /api/runs` liste
  les dossiers datés.
- `uv run pytest` : suite existante verte.
- Bout en bout :
  1. `uv run scrubber seed-officielles` ;
  2. `uv run scrubber --serie s1998 --no-llm` → la lignée doit regrouper ≈ 40 paires 2023 ↔ 2024 ;
  3. `uv run scrubber-check index build --serie s1998 --output ./idx.json.gz` puis
     `uv run scrubber-check <export XML de BTS 2024> --index ./idx.json.gz` → environ 27 listes en `doublon` exact
     et environ 40 en `lignee` vis-à-vis de BTS 2023 (mesures du § 2.2) ; `uv run scrubber-check-api` puis
     `curl -F file=@… localhost:8000/v1/check` → même rapport en JSON ;
     `uv run scrubber-check index build` (toute la base) : durée et volume notés dans la doc ;
  4. `uv run scrubber-web` → filtres de niveau et onglet Pré-contrôle.
- Ergonomie, dans le navigateur (`uv run scrubber-web`, démo `demo/BTS_demo.xml`) : décider au clavier,
  redémarrer le serveur puis recharger → décisions présentes ; action de masse puis « Annuler » ; rechargement
  de la page → run, filtres et liste sélectionnée conservés ; nom de liste contenant `<b>` affiché tel quel.

## Ordre de livraison
0. Plan versionné à la racine (`PLAN_API.md`).
1. Phase U (U1 → U5, U7) — gains immédiats pour les experts, sans dépendre de l'API.
2. Phase A — client API, corpus, provenance, lignée (socle commun du service de contrôle).
3. Phase C — service publiable `scrubber-check` (CLI + API + index + déploiement) ; les niveaux y sont
   d'abord **suggérés** depuis la provenance.
4. Phase B — niveaux validés dans le registre nettoyé, choix de la maître ; le service les utilise dès
   qu'ils existent. U6 suit A, B et C.

---

## 5. État d'avancement (26/10/2026)

Livré : phases U, A, B et C, avec tests (`tests/test_autosave.py`, `test_insee_api.py`,
`test_pipeline_api.py`, `test_check.py`, `test_levels.py`) et documentation
(`doc/scrubber_check.qmd`, guide, prise en main, note méthodologique, changements récents).

Écarts au plan :

- **Actions de masse** : au lieu d'envoyer les filtres au serveur, le client calcule
  les doublons affichés et les envoie à une route générique `POST /api/registry/decisions`,
  qui renvoie les décisions précédentes (annulation). L'ancienne route `/bulk` reste.
- **Lignée** : type de détection `lineage` (badge « lignée »).
- **Inclusion** : faite dans `scrubber-check` (inclusion et extension), pas encore dans
  l'entonnoir du pipeline.
- **Listes non référencées** : export seulement pour une source XML ; l'API ne donne accès
  qu'aux listes référencées par des variables.
- **Reconnaissance de nomenclature par `/codes/…`** (NAF, PCS, CJ) : non faite.
- **Niveaux** : la maître choisie par l'expert passe avant un niveau seulement *suggéré*
  (pas avant un niveau validé).
- **Packaging** : les dépendances de l'appli et du LLM sont dans le groupe `web`, installé
  par défaut (`[tool.uv] default-groups`), plutôt qu'un extra : `uv sync` et
  `uv run scrubber-web` fonctionnent comme avant ; `Dockerfile.check` installe sans
  (`--no-default-groups`, environnement de 70 Mo contre 378 Mo).
- **Seed des officielles** : les communes (~35 000 codes) sont exclues par défaut
  (`--geo communes` pour les ajouter), l'écran d'édition du registre nettoyé ne les
  afficherait pas bien.

Constats faits pendant l'implémentation :

- Base complète : 617 opérations, 33 avec fichiers DDI ; 7 083 CodeLists publiées
  (11 utilisées par ≥ 2 séries) ; index de 3,3 Mo construit en ~27 min (7 881 appels),
  chargé en ~9 s ; contrôle de BTS 2024 en ~8 s contre toute la base, après ajout d'un
  pré-filtre des candidats au score flou (23 s sans).

- Certains fichiers d'opérations publiées ne sont pas diffusés par l'API (HTTP 401) :
  ignorés et signalés (`restricted_objects`) au lieu d'interrompre le parcours.
- Non vérifiable ici (pas de Docker) : construction des images et déploiement Kubernetes.
