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
