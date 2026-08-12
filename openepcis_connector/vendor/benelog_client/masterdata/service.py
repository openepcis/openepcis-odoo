# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Publishing master data: per-record upsert, bulk onboarding, GPC search.

Two ways into the catalog, with different semantics:

- :meth:`Masterdata.upsert_product` / :meth:`Masterdata.upsert_organization`
  are idempotent merges. ``PUT`` creates or updates, and an absent key means
  "leave alone", so publishing never clears a value by accident.
- :meth:`Masterdata.bulk_products` / :meth:`Masterdata.bulk_organizations`
  wrap the CSV endpoint, which **creates and does not update**: a key the
  catalog already holds comes back as a duplicate rather than being
  overwritten. It is an onboarding tool for the first load, not a
  synchroniser; day-to-day changes go record by record.
"""

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..core import gs1
from ..core.client import Client
from . import vocabulary

#: Rows per CSV upload. Keeps every chunk well under the endpoint's 10 MB cap,
#: so a chunk cannot be refused for size.
CHUNK_ROWS = 2000

#: Bulk error codes that mean "the catalog already holds this key" — the state
#: onboarding was trying to reach, so they are counted as present, not failed.
DUPLICATE_CODES = frozenset({"DUPLICATE_GTIN", "DUPLICATE_GLN"})

#: The code this side stamps on a row it refused to send because its key is
#: not a valid GS1 key. Kept apart from server codes on purpose.
INVALID_KEY = "INVALID_KEY"


class InvalidKey(ValueError):
    """A GS1 key that fails validation before any request is made.

    Carries the structured :class:`~benelog_client.core.gs1.KeyProblem`, so a
    host adapter can phrase the fault in its own language instead of parsing
    an English sentence.
    """

    def __init__(self, key: str, problem: gs1.KeyProblem) -> None:
        super().__init__(f"{key!r} is not a valid {problem.kind} ({problem.fault})")
        self.key = key
        self.problem = problem


@dataclass(frozen=True)
class BulkRowError:
    """One row the bulk load did not accept."""

    row: int
    """1-based position in the rows handed to the bulk call, not in any chunk."""

    code: str
    """The server's error code, or :data:`INVALID_KEY` for a row refused here."""

    message: str


@dataclass(frozen=True)
class BulkReport:
    """What became of a bulk load, over all chunks."""

    total: int
    """Rows handed in."""

    accepted: int
    """Rows the catalog newly created."""

    duplicates: int
    """Rows whose key the catalog already held. Bulk loading only creates, so
    these are already in the state the load was aiming for."""

    failures: tuple[BulkRowError, ...]
    """Everything else, including rows refused here for an invalid key."""


@dataclass(frozen=True)
class GpcNode:
    """One node of the GS1 Global Product Classification."""

    code: str
    title: str
    definition: str
    lineage: str
    """The human-readable path from segment down to this node."""


class Masterdata:
    """Master data calls against the resolver, through a configured client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    # -- Per-record upsert ---------------------------------------------------

    def upsert_product(self, gtin: str, document: dict[str, Any]) -> Any:
        """Create or update one product; ``PUT`` merges, so this is idempotent."""
        return self._upsert(vocabulary.kind("PRODUCT"), gtin, document)

    def upsert_organization(self, gln: str, document: dict[str, Any]) -> Any:
        """Create or update one organization."""
        return self._upsert(vocabulary.kind("ORGANIZATION"), gln, document)

    def _upsert(self, kind: vocabulary.Kind, key: str, document: dict[str, Any]) -> Any:
        cleaned = gs1.clean(key)
        problem = gs1.problem_with(cleaned, kind.key_type)
        if problem:
            raise InvalidKey(key, problem)
        payload = dict(document)
        payload[kind.key_term] = cleaned
        # The resolver's schema validation requires the GS1 class at the root
        # ("required property 'type' not found" otherwise). Callers stating
        # their own — e.g. ["Product", "TextileApparel"] — are left alone.
        payload.setdefault("type", kind.record_type)
        return self._client.put(f"{kind.endpoint}/{cleaned}", payload)

    # -- Bulk onboarding -----------------------------------------------------

    def bulk_products(self, rows: Iterable[dict[str, Any]]) -> BulkReport:
        """Load products in bulk. Creates only; see the module docstring.

        Rows are dicts keyed by the bulk column names of the vocabulary
        manifest (:func:`~benelog_client.masterdata.vocabulary.bulk_columns`).
        Unknown keys are dropped; a row whose GTIN is not a valid GS1 key is
        refused here and reported, because the server would fail it one opaque
        row at a time.
        """
        return self._bulk(vocabulary.kind("PRODUCT"), rows)

    def bulk_organizations(self, rows: Iterable[dict[str, Any]]) -> BulkReport:
        """Load organizations in bulk. Same semantics as :meth:`bulk_products`."""
        return self._bulk(vocabulary.kind("ORGANIZATION"), rows)

    def _bulk(self, kind: vocabulary.Kind, rows: Iterable[dict[str, Any]]) -> BulkReport:
        columns = vocabulary.bulk_columns(kind.name)
        failures: list[BulkRowError] = []
        sendable: list[tuple[int, dict[str, Any]]] = []

        total = 0
        for position, row in enumerate(rows, start=1):
            total = position
            key = gs1.clean(str(row.get(kind.key_term) or ""))
            problem = gs1.problem_with(key, kind.key_type)
            if problem:
                failures.append(
                    BulkRowError(
                        row=position,
                        code=INVALID_KEY,
                        message=f"not a valid {kind.key_type} ({problem.fault})",
                    )
                )
                continue
            sendable.append((position, {**row, kind.key_term: key}))

        accepted = 0
        duplicates = 0
        for start in range(0, len(sendable), CHUNK_ROWS):
            chunk = sendable[start : start + CHUNK_ROWS]
            answer = self._client.post_file(
                kind.bulk_endpoint,
                "benelog-import.csv",
                self._csv(columns, [row for _, row in chunk]),
                form={"format": "csv"},
            )
            accepted += int(answer.get("successCount") or 0)
            for problem_row in answer.get("errors") or []:
                code = str(problem_row.get("errorCode") or "")
                if code in DUPLICATE_CODES:
                    duplicates += 1
                    continue
                # The server numbers rows per upload; map back to the caller's
                # numbering so the report survives chunking.
                sent_index = int(problem_row.get("rowNumber") or 0)
                position = chunk[sent_index - 1][0] if 1 <= sent_index <= len(chunk) else 0
                failures.append(
                    BulkRowError(
                        row=position,
                        code=code,
                        message=str(problem_row.get("errorMessage") or ""),
                    )
                )

        return BulkReport(
            total=total, accepted=accepted, duplicates=duplicates, failures=tuple(failures)
        )

    @staticmethod
    def _csv(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buffer.getvalue().encode("utf-8")

    # -- Classification ------------------------------------------------------

    def search_gpc(self, query: str, level: str = "BRICK", size: int = 40) -> list[GpcNode]:
        """Search the GS1 Global Product Classification by free text."""
        nodes = self._client.get(
            "/gpc/search", params={"q": query.strip(), "level": level, "size": size}
        )
        return [
            GpcNode(
                code=str(node.get("code")),
                title=str(node.get("title") or ""),
                definition=str(node.get("definition") or ""),
                # The field has appeared under both names in live answers.
                lineage=str(node.get("lineage") or node.get("path") or ""),
            )
            for node in (nodes or [])
            if node.get("code")
        ]
