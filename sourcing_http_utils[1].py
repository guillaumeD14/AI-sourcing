"""Appels HTTP avec timeout, tentatives limitées et messages clairs."""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

import requests

from sourcing_errors import NetworkError

DEFAULT_TIMEOUT: tuple[float, float] = (5.0, 30.0)  # (connexion, lecture)
RETRY_STATUSES: frozenset[int] = frozenset({500, 502, 503, 504})


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    service: str,
    timeout: tuple[float, float] | float = DEFAULT_TIMEOUT,
    retries: int = 3,
    backoff: float = 1.0,
    retry_statuses: Iterable[int] = RETRY_STATUSES,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> requests.Response:
    """Exécute une requête avec au plus ``retries`` tentatives.

    - Les erreurs réseau (timeout, connexion) et les statuts 5xx sont retentés
      avec un délai exponentiel.
    - Les statuts 4xx sont renvoyés tels quels : l'appelant les interprète.
    - Le message d'erreur ne contient jamais l'URL (elle peut porter une signature).
    """
    retry_set = frozenset(retry_statuses)
    attempts = max(1, retries)
    last_reason = "erreur inconnue"
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
        except requests.Timeout:
            last_reason = "délai dépassé"
        except requests.ConnectionError:
            last_reason = "connexion impossible"
        except requests.RequestException as exc:
            last_reason = f"erreur réseau ({type(exc).__name__})"
        else:
            if response.status_code not in retry_set:
                return response
            last_reason = f"le service répond {response.status_code}"
        if attempt < attempts:
            sleep(backoff * (2 ** (attempt - 1)))
    raise NetworkError(
        f"{service} : échec après {attempts} tentative(s) — {last_reason}. "
        "Vérifiez la connexion puis réessayez."
    )
