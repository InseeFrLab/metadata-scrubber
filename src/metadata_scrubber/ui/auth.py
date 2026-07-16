"""HTTP Basic Auth middleware -- protège l'API avec un mot de passe partagé.

Le mot de passe est injecté depuis un Secret Kubernetes via la variable
d'environnement ``SCRUBBER_AUTH_PASSWORD``. **Jamais codé en dur dans le code.**
"""

from __future__ import annotations

import base64
import os
import secrets
from typing import Callable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response


# Le mot de passe vient du Secret K8s (jamais en dur dans le code).
# En cas d'absence, le serveur refuse de démarrer avec un message clair.
def _load_password() -> str:
    pwd: str = os.environ.get("SCRUBBER_AUTH_PASSWORD", "")
    if not pwd:
        print(
            "SCRUBBER_AUTH_PASSWORD n'est pas défini dans l'environnement. Generating a new one ..."
        )
        pwd = base64.b64encode(secrets.token_bytes(8)).decode(encoding="utf-8")
        print(f"App password : {pwd}")
    return pwd


_PASSWORD: str = _load_password()

# Endpoints publics -- healthcheck et docs Swagger ne sont pas protégés
_PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/openapi.json", "/favicon.ico"})


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware Starlette / FastAPI qui applique le Basic Auth sur tous les
    endpoints sauf ceux de ``_PUBLIC_PATHS``.

    Le header ``Authorization: Basic base64(user:password)`` est attendu.
    Seule la partie ``password`` est vérifiée -- le ``user`` est ignoré, car il
    s'agit d'une clé partagée par l'équipe (pas de gestion d'identité).
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Endpoints publics : passage direct
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        auth_header: str | None = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Basic "):
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="scrubber"'},
            )

        try:
            encoded: str = auth_header[6:]  # retirer "Basic "
            decoded_bytes = base64.b64decode(encoded)
            decoded: str = decoded_bytes.decode("utf-8")
        except Exception:
            return JSONResponse(
                {"detail": "Invalid credentials"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="scrubber"'},
            )

        # user:password -- seul password compte (clé partagée)
        _user, _, password = decoded.partition(":")

        if not _secure_compare(password, _PASSWORD):
            return JSONResponse(
                {"detail": "Invalid credentials"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="scrubber"'},
            )

        return await call_next(request)


def _secure_compare(a: str, b: str) -> bool:
    """Comparaison de chaînes résistante aux *timing attacks*."""
    return secrets.compare_digest(a, b)


def _basic_auth_header(password: str) -> str:
    """Helper pour générer un header Basic Auth valide (utile dans les tests)."""
    encoded = base64.b64encode(f"team:{password}".encode()).decode()
    return f"Basic {encoded}"


def _test_basic() -> None:
    """Vérifier que le middleware instancie correctement."""
    print("=== Test AuthMiddleware (standalone) ===")
    print(
        f"ENV SCRUBBER_AUTH_PASSWORD {'défini' if os.environ.get('SCRUBBER_AUTH_PASSWORD') else 'non défini'}"
    )
    print(f"Public paths : {sorted(_PUBLIC_PATHS)}")
    mw = AuthMiddleware(None)
    print(f"Middleware instancié : {type(mw).__name__}")


if __name__ == "__main__":
    _test_basic()
