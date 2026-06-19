# metadata-curator

Nettoyage, enrichissement et mise en cohérence de la base de métadonnées statistiques.

## Contexte

L'INSEE maintient un référentiel de métadonnées statistiques structuré selon le modèle DDI 3.3. Au fil des années et des opérations statistiques, ce référentiel a accumulé des redondances : les mêmes listes de codes ont été recréées indépendamment pour chaque opération (Recensement, SIRENE, BTS, BPE…), sans mutualisation. Par ailleurs, les liens entre variables et concepts sémantiques sont incomplets ou absents.

Ce projet vise à :

1. **Dédoublonner** les listes de codes (~20 000 dans la base actuelle) au sein de chaque opération puis entre opérations
2. **Enrichir** les métadonnées en établissant des liens entre variables et concepts SKOS
3. **Préparer le terrain** pour l'automatisation de la production de métadonnées à partir de fichiers de données (Parquet/CSV)

> Approche progressive par opération pilote (RP, SIRENE, BTS, BPE) pour permettre une évaluation itérative avant passage à l'échelle.

## Cas d'usage

### 1. Dédoublonnage des listes de codes

Identifier les listes de codes sémantiquement identiques stockées plusieurs fois, puis produire :
- un **fichier de mapping** (format RML envisagé) vers les listes canoniques à conserver
- des **recommandations priorisées** (fréquence × impact) pour la mutualisation

Périmètre : d'abord intra-opération, puis inter-opérations. Pas de modification des données historiques déjà publiées ; les mises à jour s'appliquent aux nouveaux millésimes uniquement via scripts.

### 2. Liens variables → concepts

Relier les variables statistiques aux concepts du thésaurus RMéS (ex. « commune », « sexe », « catégorie socioprofessionnelle ») en s'appuyant sur les données DDI de RP et SIRENE et les 54 concepts SKOS publiés sur `rdf.insee.fr`.

### 3. Vision long terme

Produire automatiquement une documentation DDI « bonne à 90% » à partir d'un fichier Parquet, en s'appuyant sur :
- le référentiel de métadonnées nettoyé (objets de référence)
- l'historique des opérations documentées
- une approche MCP + exposition SPARQL pour exploiter la sémantique RDF des concepts

## Données

Les fichiers sources résident dans le bucket S3 `s3://projet-metadonnees-rmes/`.

| Fichier | Taille | Contenu |
|---|---|---|
| `CodeLists&Categories_Dev4_Hors_resource_package_2026-04-21.xml` | 169 MB | **Référentiel RMéS** : 3 340 listes, 160 867 codes |
| `RP.xml` | 162 MB | **Recensement de la population** : 556 listes, 522 variables (+183 représentées) |
| `RP2022.xml` | 151 MB | **Recensement 2022** : ~757 listes, 2 951 variables |
| `EAR.xml` | 87 MB | **Enquêtes annuelles de recensement** : 656 listes, 902 variables |
| `SIRENE2025.xml` | 47 MB | **SIRENE 2025** : 42 listes, 318 variables |
| `Variables&RepresentedVariables_Dev4-2026-04-23.xml` | 36 MB | **Variables RMéS** : 12 374 variables, 14 053 variables représentées |
| `ddi-codelist-identifier.json` | 11 MB | **Index RMéS** : 19 437 UUID de listes (sans le contenu) |
| `BPE.xml` | 5.7 MB | **Base permanente des équipements** : 126 listes, 206 variables (+341 représentées) |
| `BTS.xml` | 4.5 MB | **Base Tous salariés** : 156 listes, 287 variables (+1 240 représentées) |
| `rmes-concepts.json` | 98 KB | **Concepts INSEE** : 54 concepts SKOS |

Les fichiers XML suivent le modèle DDI 3.3 ([documentation](https://docs.ddialliance.org/DDI-Lifecycle/3.3/model/index.html)).


### Modèle de données

```
Variable ──► RepresentedVariable ──► CodeList ──► Code ──► Category
(12 374)       (14 053)              (3 340)               (libellé fr-FR)
```

## Équipe

Projet collaboratif entre la **division Métadonnées et standards** et le **SSP Lab**.
