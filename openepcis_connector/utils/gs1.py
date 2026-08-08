# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""GS1 identifiers: arithmetic, validation, Digital Link construction.

The arithmetic and the length tables come from the GS1 General Specifications —
section 7.9 for the check digit, the key-format tables for permitted lengths.
Both are published standards, and the rest of this module is the connector's own:
it shares no code with any other implementation, and nothing here is derived from
a server-side component of the platform. That is what lets the addon carry its
own licence without qualification.

Free of Odoo imports on purpose, for two reasons. It runs under a bare test run
with no database, which is where a broken check digit should be caught. And a
validator that produced finished English sentences could not be translated — so
what comes out of :func:`problem_with` is a *description* of the fault, and the
Odoo layer turns it into a message a user reads.
"""

import re
from dataclasses import dataclass

#: Total lengths (check digit included) each key type may have, per the GS1
#: General Specifications. A GTIN comes in four sizes because GTIN-8, -12 and -13
#: predate the 14-digit form and remain valid.
KEY_LENGTHS = {
    "GTIN": (8, 12, 13, 14),
    "GLN": (13,),
    "SSCC": (18,),
}

#: Application identifier a key is anchored on in a Digital Link.
#:
#: A GLN appears twice with different meanings: 414 identifies a physical
#: location, 417 identifies the party that operates it. The resolver routes them
#: separately, so choosing the wrong one produces a URI nothing answers.
ANCHOR_AI = {
    "GTIN": "01",
    "PARTY_GLN": "417",
    "LOCATION_GLN": "414",
    "SSCC": "00",
}

#: Separators people leave in when they copy a code off a label or out of a
#: spreadsheet. Anything else non-numeric is a mistake worth reporting, not
#: something to quietly discard.
_PADDING = re.compile(r"[\s\- ‑–.]+")
_ALL_DIGITS = re.compile(r"\A[0-9]+\Z")

#: Faults :func:`problem_with` can report.
EMPTY = "empty"
NOT_NUMERIC = "not_numeric"
BAD_LENGTH = "bad_length"
BAD_CHECK_DIGIT = "bad_check_digit"


@dataclass(frozen=True)
class KeyProblem:
    """What is wrong with a key, in terms the Odoo layer can phrase.

    Carries the surrounding facts — which key type was expected, which lengths
    that type allows, how long the value actually is — so that whoever writes the
    message needs no second lookup.
    """

    fault: str
    """One of :data:`EMPTY`, :data:`NOT_NUMERIC`, :data:`BAD_LENGTH`,
    :data:`BAD_CHECK_DIGIT`."""

    kind: str
    """The key type that was expected: ``GTIN``, ``GLN``, ``SSCC``."""

    allowed_lengths: tuple = ()
    """Lengths that type permits — for :data:`BAD_LENGTH`, the answer to "how
    long should it be?"."""

    actual_length: int = 0
    """Length of what was supplied, separators removed."""

    correct_check_digit: str = ""
    """For :data:`BAD_CHECK_DIGIT`, the digit the key should have ended with."""


def check_digit(body):
    """The check digit for a key body — everything except the final digit.

    GS1 General Specifications 7.9: counting from the right of the *body*,
    digits in odd positions are multiplied by three and those in even positions
    by one; the check digit is whatever must be added to that total to reach a
    multiple of ten.

    :param body: digits only, e.g. the leading 13 of a GTIN-14.
    :returns: a single character, ready to append.
    """
    digits = [int(character) for character in reversed(body)]
    total = 3 * sum(digits[0::2]) + sum(digits[1::2])
    return str(-total % 10)


def with_check_digit(body):
    """A complete key: the body plus its check digit."""
    return "%s%s" % (body, check_digit(body))


def clean(raw):
    """A key with the padding people leave in taken out.

    Accepts what Odoo hands over: ``None`` and ``False`` for an unset field, and
    integers for a value that came from a spreadsheet. Note that ``False`` has to
    be caught explicitly — ``str(False)`` would produce ``"False"``, which is
    truthy and would sail through every later check as though it were a code.
    """
    if raw is None or raw is False:
        return ""
    return _PADDING.sub("", str(raw)).strip()


def problem_with(raw, kind):
    """Why this value is not a usable key of that type, or ``None`` if it is.

    The order of the checks is the order a person would apply them: is there
    anything there, is it a number, is it the right size, does it check out.
    """
    try:
        allowed = KEY_LENGTHS[kind]
    except KeyError:
        raise ValueError("This connector does not handle %r keys" % (kind,)) from None

    key = clean(raw)
    if not key:
        return KeyProblem(EMPTY, kind, allowed)
    if not _ALL_DIGITS.match(key):
        return KeyProblem(NOT_NUMERIC, kind, allowed, len(key))
    if len(key) not in allowed:
        return KeyProblem(BAD_LENGTH, kind, allowed, len(key))

    expected = check_digit(key[:-1])
    if key[-1] != expected:
        return KeyProblem(BAD_CHECK_DIGIT, kind, allowed, len(key), expected)
    return None


def is_valid(raw, kind):
    """Whether this value is a well-formed key of that type."""
    return problem_with(raw, kind) is None


def digital_link(base_url, ai, key):
    """The GS1 Digital Link URI a key resolves to.

    ``https://id.example.org`` + ``01`` + ``09521234567890`` gives
    ``https://id.example.org/01/09521234567890``. No qualifiers: this connector
    publishes at model level, where the key alone is the whole identity.

    Returns an empty string when any part is missing, so that a half-built URI
    never reaches a form or a label.
    """
    if not base_url or not ai or not key:
        return ""
    return "%s/%s/%s" % (base_url.rstrip("/"), ai, clean(key))


def language_tag(odoo_lang):
    """The language tag the catalog stores, for an Odoo locale code.

    Localised catalog fields are keyed by a bare language subtag, so ``de_DE``
    becomes ``de`` and ``en_GB`` becomes ``en``: the territory says where a
    reader is, not what language the text is in.

    Scripts are the exception, because they distinguish the text itself rather
    than the audience — ``zh_CN`` becomes ``zh-Hans`` and ``sr_RS@latin``
    becomes ``sr-Latn``. Merging those would put two mutually unreadable
    variants under one key.
    """
    if not odoo_lang:
        return ""
    code = str(odoo_lang).strip().replace("-", "_")

    script = None
    if "@" in code:
        code, modifier = code.split("@", 1)
        script = {"latin": "Latn", "cyrillic": "Cyrl"}.get(modifier.lower())

    language, _, territory = code.partition("_")
    language = language.lower()
    territory = territory.upper()

    if language == "zh" and script is None:
        script = {"CN": "Hans", "SG": "Hans", "TW": "Hant", "HK": "Hant"}.get(territory)

    return "%s-%s" % (language, script) if script else language
