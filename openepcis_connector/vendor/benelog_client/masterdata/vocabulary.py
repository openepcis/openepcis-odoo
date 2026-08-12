# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""The vocabulary manifest: which GS1 terms the platform speaks, pinned locally.

The manifest is a data file shipped with this package, seeded from the Phase 0
audit of the Odoo connector and pinned against the resolver's live OpenAPI. It
exists because the platform speaks two divergent vocabularies and nothing else
records that fact:

- **record**: the nested JSON of ``PUT /products/{gtin}`` and
  ``PUT /organizations/{globalLocationNumber}``, keyed by GS1 Web Vocabulary
  local names with dotted paths and ``[]`` list segments;
- **bulk**: the flat, English-only CSV column set of ``POST /bulk/*``, whose
  column names are load-bearing: the server-side parser looks columns up by
  name and silently drops what it does not recognise.

Where a term exists in both, the manifest carries the bulk column as an alias
of the record path, so a host maps its fields once. The platform may later
serve this same structure from an endpoint; until then the pinned file is the
single source, and updating it is a reviewed change, not a runtime surprise.
"""

import json
import sys
from dataclasses import dataclass
from importlib import resources

__all__ = ["Kind", "Term", "bulk_columns", "kind", "manifest_version", "term", "terms_for"]


@dataclass(frozen=True)
class Kind:
    """One publishable record kind and where it goes."""

    name: str
    """``PRODUCT`` or ``ORGANIZATION``."""

    key_term: str
    """The term carrying the GS1 key: ``gtin``, ``globalLocationNumber``."""

    key_type: str
    """The key type for validation: ``GTIN``, ``GLN``."""

    endpoint: str
    """Per-record upsert base path; the key is appended."""

    bulk_endpoint: str
    """CSV onboarding path."""

    required_terms: tuple[str, ...]
    """Terms the catalog refuses the record without."""

    record_type: str
    """The GS1 Web Vocabulary class of the record (``Product``,
    ``Organization``). The resolver's schema validation requires it at the
    document root; the upsert sets it when the caller has not."""


@dataclass(frozen=True)
class Term:
    """One GS1 Web Vocabulary term the platform accepts."""

    path: str
    """Dotted record path, ``[]`` marking a single-element list segment."""

    kind: str
    """Which record kind carries it."""

    shape: str
    """How the value is formed: ``text``, ``localized``, ``quantity``,
    ``boolean_text``, ``integer``, ``float``, ``date``."""

    scope: str
    """Where the term is spoken: ``record``, ``bulk``, or ``both``."""

    bulk_column: str = ""
    """The CSV column name, for terms with a bulk side. Load-bearing: the
    server parser matches it literally."""


class _Manifest:
    def __init__(self) -> None:
        # By the package object, not by name: the name would break when the
        # library is vendored under another package (the Odoo addon does).
        raw = json.loads(
            resources.files(sys.modules[__package__])
            .joinpath("vocabulary.json")
            .read_text(encoding="utf-8")
        )
        self.version: str = raw["version"]
        self.kinds: dict[str, Kind] = {
            name: Kind(
                name=name,
                key_term=data["keyTerm"],
                key_type=data["keyType"],
                endpoint=data["endpoint"],
                bulk_endpoint=data["bulkEndpoint"],
                required_terms=tuple(data["requiredTerms"]),
                record_type=data["recordType"],
            )
            for name, data in raw["kinds"].items()
        }
        self.terms: tuple[Term, ...] = tuple(
            Term(
                path=entry["path"],
                kind=entry["kind"],
                shape=entry["shape"],
                scope=entry["scope"],
                bulk_column=entry.get("bulkColumn", ""),
            )
            for entry in raw["terms"]
        )


_LOADED: _Manifest | None = None


def _load() -> _Manifest:
    global _LOADED
    if _LOADED is None:
        _LOADED = _Manifest()
    return _LOADED


def manifest_version() -> str:
    """The pin date of the shipped manifest."""
    return _load().version


def kind(name: str) -> Kind:
    """The record kind by name.

    :raises KeyError: for a kind the manifest does not know.
    """
    return _load().kinds[name]


def term(path: str) -> Term:
    """The term at a record path.

    :raises KeyError: for a path the manifest does not know.
    """
    for entry in _load().terms:
        if entry.path == path:
            return entry
    raise KeyError(path)


def terms_for(kind_name: str, scope: str = "") -> tuple[Term, ...]:
    """All terms of a record kind, optionally narrowed to one scope.

    ``scope="record"`` answers what a per-record upsert may carry,
    ``scope="bulk"`` what the CSV may carry; a term marked ``both`` appears in
    either answer.
    """
    return tuple(
        entry
        for entry in _load().terms
        if entry.kind == kind_name and (not scope or entry.scope in (scope, "both"))
    )


def bulk_columns(kind_name: str) -> tuple[str, ...]:
    """The exact CSV header for a kind, in manifest order, key column first.

    The server parser matches these names literally and silently drops
    anything else, so a header that is nearly right produces a run where every
    row fails for no visible reason. Never edit a name here without reading
    the parser.
    """
    key = kind(kind_name).key_term
    return (key, *(entry.bulk_column for entry in terms_for(kind_name, "bulk")))
