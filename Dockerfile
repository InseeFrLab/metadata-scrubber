---
# Metadata Scrubber — Dockerfile pour le déploiement Kubernetes
#
# Image de base : inseefrlab/python-datascience:latest (outilssp cloud déjà inclus)
# Entrée : scrubber-web → FastAPI exposé sur le port 8000
# Secrets : injectés en variables d'environnement (jamais en clair ici)
---
FROM inseefrlab/python-datascience:latest

LABEL org.opencontainers.image.source="https://github.com/inseefrlab/metadata-scrubber"

WORKDIR /app

# Dépendances isolées du code pour tirer parti du cache Docker
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Code source du projet
COPY . .

EXPOSE 8000

# Démarrage du serveur monolithique FastAPI
CMD ["uv", "run", "scrubber-web"]