"""Appels réseau : timeout obligatoire, tentatives limitées, erreurs traduites pour l'acheteur.

Aucune clé n'apparaît dans les messages : les en-têtes ne sont jamais recopiés dans les erreurs.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

import requests

from sourcing_errors import AuthError, NetworkError, QuotaError, SourcingError

DEFAULT_TIMEOUT = (6.0, 30.0)  # (connexion, lecture) en secondes
RETRY_STATUSES = frozenset({500, 502, 503, 504})


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "delifrance-sourcing/2.0"
    return session


def request_with_retry(
    session: Any,
    method: str,
    url: str,
    *,
    service: str,
    retries: int = 2,
    backoff: float = 1.0,
    timeout: tuple[float, float] | float = DEFAULT_TIMEOUT,
    ok_statuses: Iterable[int] = (200,),
    sleep: Any = time.sleep,
    error_hook: Callable[[Any], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Exécute la requête. Réessaie sur erreur réseau et 5xx ; traduit 401/403/429 en erreurs claires.

    ``service`` ("Cloudinary", "Tavily"...) est utilisé dans les messages. ``error_hook`` reçoit la réponse en
    erreur et peut lever une exception plus précise. Retourne l'objet réponse.
    """
    ok = set(ok_statuses)
    last_problem = ""
    for attempt in range(retries + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.Timeout:
            last_problem = "délai dépassé"
        except requests.exceptions.ConnectionError:
            last_problem = "connexion impossible"
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"{service} : erreur réseau ({type(exc).__name__}).") from None
        else:
            status = response.status_code
            if status in ok:
                return response
            if status in RETRY_STATUSES:
                last_problem = f"erreur serveur {status}"
            else:
                if error_hook is not None:
                    error_hook(response)  # peut lever une erreur plus précise (lecture du corps de la réponse)
                raise_for_status_message(service, status, response)
        if attempt < retries:
            sleep(backoff * (2 ** attempt))
    raise NetworkError(f"{service} indisponible ({last_problem}) après {retries + 1} tentative(s). Réessayez dans un instant.")


def raise_for_status_message(service: str, status: int, response: Any = None) -> None:
    """Lève l'exception adaptée au code HTTP, avec une consigne de correction."""
    if status in (401, 403):
        raise AuthError(f"{service} a refusé l'accès (HTTP {status}). Vérifiez la clé / les identifiants dans les Secrets Streamlit.")
    if status in (429, 432, 433):
        raise QuotaError(f"{service} : quota ou limite de débit atteint (HTTP {status}). Attendez ou réduisez le budget de requêtes.")
    if status == 404:
        raise SourcingError(f"{service} : ressource introuvable (HTTP 404).")
    raise SourcingError(f"{service} a répondu avec une erreur (HTTP {status}).")
