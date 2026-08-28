# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Minting SSCCs, which is the one GS1 key a company issues entirely by itself.

A GTIN identifies an article and a GLN a place, and both are registered — which
is why this connector draws them from the platform's pool. An SSCC identifies
*one logistic unit*: this pallet, built this morning, gone by Friday. Nobody
registers it and nobody needs to look it up in a directory, so it is minted
locally from three parts:

    extension digit + company prefix + serial reference = 17 digits, then a
    check digit.

The extension digit is free for the issuer to use — a hint about the unit's
size, or simply zero. The serial reference fills whatever remains of the 17,
which is why a longer prefix leaves fewer numbers: a 7-digit prefix leaves nine
digits of serial, a 12-digit one leaves four.

The single rule that matters: an SSCC must never repeat within a year of the
unit's life. That is what the sequence is for, and why this refuses to build
one out of a number that no longer fits.
"""

from ..vendored import gs1

#: Total length of an SSCC, check digit included.
SSCC_LENGTH = 18


def problem_with_prefix(prefix):
    """Why this cannot be a GS1 company prefix, or an empty string if it can."""
    digits = gs1.clean(prefix)
    if not digits:
        return "It is empty."
    if not digits.isdigit():
        return "A company prefix is digits only."
    if not 6 <= len(digits) <= 12:
        return "A company prefix is between 6 and 12 digits; this one has %d." % len(digits)
    return ""


def build(prefix, serial_reference, extension_digit="0"):
    """An SSCC from a prefix and a serial reference, check digit included.

    :raises ValueError: when the serial reference does not fit beside the
        prefix. Truncating it would be the worse answer: two units would end up
        with the same SSCC, and the whole point of the key is that they do not.
    """
    digits = gs1.clean(prefix)
    room = SSCC_LENGTH - 1 - 1 - len(digits)
    reference = str(serial_reference).strip()
    if not reference.isdigit():
        raise ValueError("a serial reference is digits only, got %r" % serial_reference)
    if len(reference) > room:
        raise ValueError(
            "serial reference %s does not fit beside a %d-digit prefix — "
            "%d digits are left for it" % (reference, len(digits), room)
        )
    body = "{}{}{}".format(str(extension_digit)[:1], digits, reference.zfill(room))
    return gs1.with_check_digit(body)


def is_valid(candidate):
    """Whether this is a well-formed SSCC, check digit and all."""
    digits = gs1.clean(candidate)
    return (
        len(digits) == SSCC_LENGTH
        and digits.isdigit()
        and gs1.with_check_digit(digits[:-1]) == digits
    )
