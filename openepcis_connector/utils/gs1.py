# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""GS1 identifiers — re-exported from the vendored ``benelog_client``.

This module used to carry the implementation; it moved to the client library
(``vendor/benelog_client/core/gs1.py``, Apache-2.0) so every connector shares
one set of check-digit arithmetic. The import path stays, because half the
addon and its tests spell ``from ..utils import gs1``.
"""

from ..vendor.benelog_client.core.gs1 import (  # noqa: F401
    ANCHOR_AI,
    BAD_CHECK_DIGIT,
    BAD_LENGTH,
    EMPTY,
    KEY_LENGTHS,
    NOT_NUMERIC,
    KeyProblem,
    check_digit,
    clean,
    digital_link,
    is_valid,
    language_tag,
    problem_with,
    with_check_digit,
)
