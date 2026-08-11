# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Connection settings, held apart from credentials.

The config names *where* the platform is; the credentials live in an
:class:`~benelog_client.core.auth.AuthStrategy` and its
:class:`~benelog_client.core.auth.TokenStore`, because a host framework owns
their persistence and this library must not.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClientConfig:
    """Where the platform is and how patient to be with it.

    :param base_url: origin of the GS1 Digital Link resolver, e.g.
        ``https://id.epcis.cloud``. Also the prefix of every Digital Link.
    :param request_timeout: ``(connect, read)`` seconds for API calls.
    :param token_timeout: ``(connect, read)`` seconds for the identity
        provider, which answers fast or not at all.
    """

    base_url: str
    request_timeout: tuple[float, float] = (5, 30)
    token_timeout: tuple[float, float] = (5, 15)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
