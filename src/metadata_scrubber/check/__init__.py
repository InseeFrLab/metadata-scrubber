"""scrubber-check — contrôle des doublons d'un fichier DDI contre les CodeLists publiées.

Service léger et déterministe (sans LLM), publiable à part de l'appli de
validation : une commande (`scrubber-check`) et une API HTTP (`scrubber-check-api`).

- `index`  : index de référence construit depuis l'API Métadonnées de l'Insee ;
- `engine` : comparaison des listes d'un fichier à l'index (et entre elles) ;
- `report` : rapport par liste (statut, correspondances, action suggérée) ;
- `cli`, `api` : interfaces.

Ce paquet n'importe ni `main`, ni `semantic`, ni `ui`.
"""
