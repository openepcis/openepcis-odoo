# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""The HTTP client every module calls through.

Three properties are worth defending, because getting any of them wrong is
either a lock-out or a lie:

- a 401 is re-authorised exactly once, and that does not count as repeating
  the operation. Otherwise a POST that was refused never gets its second
  chance, or worse, one that succeeded gets sent twice;
- only idempotent verbs retry. A retried POST could publish twice or burn a
  GS1 key;
- errors carry the server's own RFC 7807 ``detail`` where there is one, with
  the structured facts alongside, and never invent a sentence the server did
  not say.
"""

import json
import logging
import time
from typing import Any

import requests

from .auth import AuthStrategy
from .config import ClientConfig
from .errors import BenelogError

logger = logging.getLogger(__name__)

#: Verbs that are safe to repeat. A retried POST is not: it could create a
#: second record or, worse, burn a GS1 key.
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE", "HEAD"})

MAX_ATTEMPTS = 3
BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.5)


class Client:
    """Authenticated JSON calls against the platform.

    :param config: where the platform is.
    :param auth: the bearer source; see
        :class:`~benelog_client.core.auth.OfflineTokenAuth`.
    :param session: a ``requests``-compatible session, injectable for tests.
    """

    def __init__(
        self,
        config: ClientConfig,
        auth: AuthStrategy,
        session: requests.Session | None = None,
    ) -> None:
        self._config = config
        self._auth = auth
        self._session = session or requests.Session()

    # -- Verbs ---------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def put(self, path: str, payload: Any) -> Any:
        return self.request("PUT", path, payload=payload)

    def post(self, path: str, payload: Any = None) -> Any:
        return self.request("POST", path, payload=payload)

    def patch(self, path: str, payload: Any) -> Any:
        return self.request("PATCH", path, payload=payload)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        params: dict[str, Any] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> Any:
        """Call the platform and return the decoded body.

        :returns: the parsed JSON, or ``None`` for an empty body (``204``).
        :raises BenelogError: for every non-2xx answer and every transport
            failure. Callers decide whether that aborts them or gets recorded.
        """
        url = self._url(path)
        method = method.upper()

        tries = MAX_ATTEMPTS if method in IDEMPOTENT_METHODS else 1
        reauth_allowed = True
        used = 0
        backoff: float = 0

        while True:
            if backoff:
                time.sleep(backoff)
                backoff = 0

            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._auth.bearer()}",
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"

            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params or None,
                    data=json.dumps(payload) if payload is not None else None,
                    timeout=timeout or self._config.request_timeout,
                )
            except requests.exceptions.RequestException as exc:
                # No status: the request may or may not have been applied. Only
                # idempotent methods get another go, so repeating is safe.
                last_error = BenelogError(
                    f"Could not reach the platform at {url}: {exc}", path=path
                )
                logger.warning("benelog %s %s failed: %s", method, path, exc)
                used += 1
                if used >= tries:
                    raise last_error from exc
                backoff = BACKOFF_SECONDS[min(used - 1, len(BACKOFF_SECONDS) - 1)]
                continue

            # A 401 means the access token lapsed early or was revoked in
            # flight. Minting a new one and going again is not a retry of the
            # operation: the call was refused, so nothing was applied. It
            # therefore costs no attempt and needs no backoff, which is what
            # makes it safe even for POST.
            if response.status_code == 401 and reauth_allowed:
                reauth_allowed = False
                self._auth.invalidate()
                continue

            if response.status_code < 300:
                return self._decode(response)

            error = self._error_from(response, path)
            used += 1
            if error.is_retryable and used < tries:
                logger.info(
                    "benelog %s %s answered %s, retrying", method, path, response.status_code
                )
                backoff = BACKOFF_SECONDS[min(used - 1, len(BACKOFF_SECONDS) - 1)]
                continue
            raise error

    def post_file(
        self,
        path: str,
        filename: str,
        content: bytes,
        form: dict[str, str] | None = None,
        timeout: tuple[float, float] | None = None,
    ) -> Any:
        """Upload a file as multipart form data.

        Separate from :meth:`request` because the body is not JSON and must not
        carry a ``Content-Type`` of its own; ``requests`` sets the multipart
        boundary, and overriding it produces an unparseable request.
        """
        url = self._url(path)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._auth.bearer()}",
        }
        try:
            response = self._session.post(
                url,
                headers=headers,
                files={"file": (filename, content, "application/octet-stream")},
                data=form or {},
                timeout=timeout or (5, 300),
            )
        except requests.exceptions.RequestException as exc:
            raise BenelogError(f"Could not reach the platform at {url}: {exc}", path=path) from exc
        if response.status_code >= 300:
            raise self._error_from(response, path)
        return self._decode(response)

    # -- Answers ---------------------------------------------------------------

    def _url(self, path: str) -> str:
        return self._config.base_url + (path if path.startswith("/") else "/" + path)

    @staticmethod
    def _decode(response: requests.Response) -> Any:
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # A login page instead of JSON is the classic symptom of an OIDC
            # bounce in front of the API; say so instead of "invalid JSON".
            if "html" in (response.headers.get("Content-Type") or ""):
                raise BenelogError(
                    "The platform answered with a web page instead of data. The "
                    "URL probably points at something other than the API.",
                    status=response.status_code,
                ) from None
            raise BenelogError(
                "The platform sent a body that is not JSON.", status=response.status_code
            ) from None

    @staticmethod
    def _error_from(response: requests.Response, path: str) -> BenelogError:
        """Turn a failed answer into a :class:`BenelogError` a human can read.

        The platform reports errors as RFC 7807 problem documents, so
        ``detail`` is already a sentence written for the caller. Prefer it over
        anything this side could invent.
        """
        problem: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                problem = body
        except ValueError:
            pass

        message = str(problem.get("detail") or problem.get("title") or problem.get("message") or "")
        if not message:
            message = (response.text or "").strip()[:300]
        if not message:
            message = "The platform refused the call without saying why."

        return BenelogError(message, status=response.status_code, problem=problem, path=path)
