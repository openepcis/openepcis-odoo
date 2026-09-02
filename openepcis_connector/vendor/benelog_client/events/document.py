# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Building EPCIS 2.0 events, and giving them an identity that survives a retry.

An EPCIS event answers four questions about one moment: *what* was involved,
*when* it happened, *where* it happened, and *why* — the business step that
occasioned it. A host adapter knows all four; what it usually gets wrong is the
spelling, the identifier form and the event's own identity. This module holds
those three.

**Identifiers are canonical GS1 Digital Link URIs on** ``id.gs1.org``, never on
the deployment's own resolver. The URI in an ``epcList`` is an *identity*, not
an address to fetch: two companies describing the same pallet must produce
byte-identical strings, and they cannot if one of them writes its own hostname
into the identity. Where the identifier resolves is a separate question, and
the linkset answers it.

**Instance and class are not interchangeable.** A serial number makes an
instance and belongs in ``epcList``; a lot number does not — a lot is a class
of goods, and it belongs in ``quantityList`` as an ``epcClass``. Putting an
LGTIN in ``epcList`` is the single most common way to make an event that
validates and means the wrong thing.

**Event identity is the canonical CBV event hash.** The ``eventID`` is not a
name chosen for the event but a property of it: ``ni:///sha-256;<hex>?ver=CBV2.0``,
computed over everything the event asserts except a literal exclusion list
(``eventID`` itself, ``recordTime``, ``errorDeclaration``, ``@context``).
Anybody holding the event can recompute it, so two systems reporting the same
observation arrive at the same identifier without having agreed on anything
beforehand. Reporting the same movement twice — a retry, a resumed queue, a
re-run of yesterday's export — therefore produces the same identifier and the
repository recognises the second as the first.

That holds only as long as every field is stable, which is why ``event_time``
must come from the source record and never from the clock at send time: the
event time is part of the statement and therefore part of the identity.

The UUIDv5 that used to be the ``eventID`` has not gone away, it has changed
jobs. It is the *sender's* idempotency key now (:func:`idempotency_key`) — what
the outbox uses to recognise a movement it already holds — and it is no longer
the event's name. The two had to be separated because an ErrorDeclaration
deliberately repeats the eventID of the event it corrects, so the eventID
cannot carry a uniqueness constraint any more.
"""

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from ..core import gs1
from . import cbv

#: The GS1 namespace every identifier in an event is expressed in.
ID_GS1_ORG = "https://id.gs1.org"

#: JSON-LD context of EPCIS 2.0. Belongs on the document, not on the event.
EPCIS_CONTEXT = "https://ref.gs1.org/standards/epcis/2.0.0/epcis-context.jsonld"

#: UUIDv5 namespace for the sender-side idempotency keys this library derives
#: (see :func:`idempotency_key`). Private and arbitrary, as RFC 4122 intends:
#: its only job is to keep these keys from colliding with anybody else's
#: derived UUIDs. It is no longer part of any event's identity.
EVENT_NAMESPACE = uuid.UUID("9b7d5a24-1f6e-5c8a-9e42-6a0d3b5c7e11")

#: Path order of the GTIN qualifiers, as GS1 prescribes it. Not alphabetical
#: and not the order a caller happens to pass them in: a Digital Link with its
#: qualifiers out of order is a different string for the same thing, which
#: defeats the point of a canonical form.
QUALIFIER_ORDER = ("22", "10", "21")


def gtin14(gtin: str) -> str:
    """A GTIN in the 14-digit form a Digital Link requires.

    One line, because the arithmetic belongs to the identifier and not to the
    event: see :func:`benelog_client.core.gs1.gtin14`.

    AI 01 is fourteen digits — always, whatever length the barcode on the
    product happens to be. A GTIN-13 (the ordinary EAN), a GTIN-12 or a GTIN-8
    are the same identifier written shorter, and the Digital Link form is the
    padded one.

    This was missing, and it was invisible from inside: an event carrying
    ``/01/9521234000013`` (thirteen digits, straight from ``product.barcode``)
    is accepted by the capture endpoint with a 202 and then **rejected** by the
    repository's validation with "Translation failed" — measured against
    a live repository on 2026-08-30. The tests could not see it because they
    built the expected identifier out of the same barcode field, so they
    asserted whatever the code produced.

    Anything that is not a plain digit string comes back unpadded: it is not a
    GTIN, and inventing digits in front of it would be worse than handing it on.
    """
    return gs1.gtin14(gtin)


def instance_uri(gtin: str, lot: str | None = None, serial: str | None = None) -> str:
    """The canonical URI of a trade item, a lot of it, or a single unit.

    ``09521234000012`` alone is the model. With a lot it is an LGTIN, with a
    serial an SGTIN, and with both it is a single unit whose lot is also known.

    The GTIN is padded to fourteen digits, because that is what AI 01 is; see
    :func:`gtin14`.
    """
    qualifiers = {"10": lot, "21": serial}
    path = "/01/" + gtin14(gtin)
    for ai in QUALIFIER_ORDER:
        value = qualifiers.get(ai)
        if value:
            path += f"/{ai}/{_path_segment(value)}"
    return ID_GS1_ORG + path


def sgln(gln: str) -> str:
    """The canonical URI of a physical location — a read point or a place."""
    return f"{ID_GS1_ORG}/414/{str(gln).strip()}"


def sscc_uri(sscc: str) -> str:
    """The canonical URI of a logistic unit, the parent of an aggregation."""
    return f"{ID_GS1_ORG}/00/{str(sscc).strip()}"


def party(gln: str) -> str:
    """The canonical URI of a party. Same key as a location, different role."""
    return sgln(gln)


def biz_transaction(kind: str, reference: str, gln: str | None = None) -> tuple[str, str]:
    """One entry of a ``bizTransactionList``, with its reference as a URI.

    This is the join between the physical record and the commercial one — the
    reason a warehouse event can answer "which order was this?" without the
    asker having an ERP login. It is also a place the schema is strict about in
    a way that reads as arbitrary: the *type* may be a bare CBV token, but the
    *reference* must be an RFC 3986 URI. A bare document number goes in raw and
    the repository refuses the whole event, naming a URI pattern rather than the
    order number it choked on.

    **Pass a URL if the system has one.** A URN names a document; a URL leads to
    it. Both satisfy the schema and both are stable identifiers, but only one of
    them can be followed — by a partner reconciling a delivery, by an auditor
    years later, by a person who received an event and wants to see the paper
    behind it. An ERP that publishes its documents should say so here, and the
    host in that URL also states plainly who issued the document, which the URN
    form has to encode as a GLN to achieve.

    A reference that is already a URI — ``https://``, ``http://`` or ``urn:`` —
    is passed through untouched, so the caller decides.

    The fallback, for a system whose documents have no address, is the CBV form
    ``urn:epcglobal:cbv:bt:<GLN>:<reference>``. The GLN is not decoration:
    order number 4711 is only unique alongside whoever wrote it.
    """
    text = str(reference).strip()
    if text.startswith(("http://", "https://", "urn:")):
        return (kind, text)
    if not gln:
        raise ValueError(
            "a business transaction reference has to be a URI. Pass the URL of "
            f"the document, or the GLN of the party that issued it so a CBV "
            f"identifier can be built: {reference!r}"
        )
    return (kind, f"urn:epcglobal:cbv:bt:{str(gln).strip()}:{_urn_segment(text)}")


def quantity_element(
    epc_class: str, quantity: float | None = None, uom: str | None = None
) -> dict[str, Any]:
    """One line of a ``quantityList``.

    A quantity without a unit is a count of items — EPCIS reads it that way,
    and stating ``EA`` instead is not more precise, only more typing. A unit
    belongs on goods that are measured rather than counted, and then it is a
    UN/CEFACT code: ``KGM``, ``LTR``, ``MTR``.

    Quantity may be omitted entirely. "This class was here" is a legitimate and
    frequently the only honest statement: a scan at a gate observes a lot, it
    does not count it.
    """
    element: dict[str, Any] = {"epcClass": epc_class}
    if quantity is not None:
        element["quantity"] = quantity
        if uom:
            element["uom"] = uom
    return element


def idempotency_key(*parts: object) -> str:
    """A stable ``urn:uuid`` the *sender* uses to recognise its own work.

    Pass the things that make this movement *this* movement and no other: the
    reporting system, the document it came from, the business step, the
    identifiers. Do not pass a timestamp taken at send time, or a counter —
    they change between two reports of the same movement, which is exactly the
    case this exists to survive.

    This used to be the ``eventID``. It is not any more: the eventID is the
    canonical event hash, which describes the event rather than the sender's
    bookkeeping. Two differences follow, and both are the reason for the split.
    The hash covers ``eventTime``, so a correction that restates the time is a
    different event; and an ErrorDeclaration repeats the eventID on purpose, so
    the eventID cannot be a unique key in an outbox. This can, and is.
    """
    name = "|".join("" if part is None else str(part) for part in parts)
    return "urn:uuid:" + str(uuid.uuid5(EVENT_NAMESPACE, name))


def error_declaration(
    *,
    reason: str,
    declaration_time: datetime,
    corrective_event_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Say that a reported event was wrong, without rewriting the past.

    EPCIS corrects by declaration, not by edit: the erroneous event stays, and a
    second event repeats it with this block attached. That is possible because
    the declaration fields are excluded from the canonical hash — the correction
    therefore carries the same content identity as the event it corrects, and
    nothing has to look the identifier up.

    ``reason`` is one of the two CBV codes, and the choice is not cosmetic.
    ``did_not_occur`` withdraws the event and must not name corrective events;
    there is nothing left to put right. ``incorrect_data`` keeps the occurrence
    and disputes the description, and ``corrective_event_ids`` points *forward*
    at the events that state it properly.

    A changed ``eventTime`` cannot be a correction of this kind: the time is
    part of the canonical hash, so restating it makes a different event. Such a
    case is ``incorrect_data`` plus a new event, never a repetition.
    """
    if reason not in (cbv.DID_NOT_OCCUR, cbv.INCORRECT_DATA):
        raise ValueError(
            f"{reason!r} is not a CBV error reason; use cbv.DID_NOT_OCCUR or cbv.INCORRECT_DATA"
        )
    if reason == cbv.DID_NOT_OCCUR and corrective_event_ids:
        raise ValueError(
            "did_not_occur withdraws the event, so it must not name corrective events "
            "(CBV); use incorrect_data where a corrected statement follows"
        )
    declaration: dict[str, Any] = {
        "declarationTime": _instant(declaration_time),
        "reason": reason,
    }
    if corrective_event_ids:
        declaration["correctiveEventIDs"] = list(corrective_event_ids)
    return declaration


def object_event(
    *,
    action: str,
    event_time: datetime,
    biz_step: str | None = None,
    disposition: str | None = None,
    epcs: Sequence[str] = (),
    quantities: Sequence[Mapping[str, Any]] = (),
    read_point: str | None = None,
    biz_location: str | None = None,
    biz_transactions: Sequence[tuple[str, str]] = (),
    source_list: Sequence[tuple[str, str]] = (),
    destination_list: Sequence[tuple[str, str]] = (),
    event_identifier: str | None = None,
    error_declaration: Mapping[str, Any] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """An ObjectEvent: something happened to these goods.

    ``read_point`` and ``biz_location`` are GLNs, not URIs — this turns them
    into SGLNs, because a GLN written straight into a ``readPoint`` is one of
    those mistakes a repository accepts and a query never finds.
    """
    event: dict[str, Any] = {"type": "ObjectEvent"}
    if event_identifier:
        event["eventID"] = event_identifier
    event["eventTime"] = _instant(event_time)
    event["eventTimeZoneOffset"] = _offset(event_time)
    event["action"] = action
    if biz_step:
        event["bizStep"] = biz_step
    if disposition:
        event["disposition"] = disposition
    if epcs:
        event["epcList"] = list(epcs)
    if quantities:
        event["quantityList"] = [dict(element) for element in quantities]
    _place(event, read_point, biz_location)
    _paperwork(event, biz_transactions, source_list, destination_list)
    if error_declaration:
        event["errorDeclaration"] = dict(error_declaration)
    if extensions:
        event.update(extensions)
    return event


def aggregation_event(
    *,
    action: str,
    event_time: datetime,
    parent_id: str,
    biz_step: str | None = None,
    disposition: str | None = None,
    child_epcs: Sequence[str] = (),
    child_quantities: Sequence[Mapping[str, Any]] = (),
    read_point: str | None = None,
    biz_location: str | None = None,
    biz_transactions: Sequence[tuple[str, str]] = (),
    event_identifier: str | None = None,
    error_declaration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """An AggregationEvent: these goods are now inside that container.

    This is what makes a pallet answerable. Scan the SSCC on the label and the
    repository can say what is underneath it, without the label having to carry
    a list — and one ``DELETE`` later takes it apart again.
    """
    event: dict[str, Any] = {"type": "AggregationEvent"}
    if event_identifier:
        event["eventID"] = event_identifier
    event["eventTime"] = _instant(event_time)
    event["eventTimeZoneOffset"] = _offset(event_time)
    event["parentID"] = parent_id
    event["action"] = action
    if biz_step:
        event["bizStep"] = biz_step
    if disposition:
        event["disposition"] = disposition
    if child_epcs:
        event["childEPCs"] = list(child_epcs)
    if child_quantities:
        event["childQuantityList"] = [dict(element) for element in child_quantities]
    _place(event, read_point, biz_location)
    _paperwork(event, biz_transactions, (), ())
    if error_declaration:
        event["errorDeclaration"] = dict(error_declaration)
    return event


def transaction_event(
    *,
    action: str,
    event_time: datetime,
    biz_transactions: Sequence[tuple[str, str]],
    biz_step: str | None = None,
    disposition: str | None = None,
    epcs: Sequence[str] = (),
    quantities: Sequence[Mapping[str, Any]] = (),
    parent_id: str | None = None,
    read_point: str | None = None,
    biz_location: str | None = None,
    source_list: Sequence[tuple[str, str]] = (),
    destination_list: Sequence[tuple[str, str]] = (),
    event_identifier: str | None = None,
    error_declaration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A TransactionEvent: these goods now belong to that paperwork, or no longer do.

    The distinction from an ObjectEvent is worth being precise about, because
    both can name a business transaction and only one of them is *about* it. An
    ObjectEvent says what happened to the goods and mentions the paperwork in
    passing; a TransactionEvent says that the relationship between the two
    changed. ``ADD`` associates, ``DELETE`` disassociates, ``OBSERVE`` confirms
    an association that already held.

    The association is a standing statement, like an aggregation: once made, it
    answers questions until something withdraws it. That is what makes
    ``DELETE`` the interesting half — goods returned against a despatch advice
    are no longer part of that shipment, and nobody downstream can work that
    out from a receipt alone.

    ``biz_transactions`` is required and may name more than one document: goods
    can be committed to an order and a despatch advice at the same time.
    """
    if not biz_transactions:
        raise ValueError(
            "a TransactionEvent has to name the business transaction it is about; "
            "an event that associates goods with nothing is an ObjectEvent"
        )
    event: dict[str, Any] = {"type": "TransactionEvent"}
    if event_identifier:
        event["eventID"] = event_identifier
    event["eventTime"] = _instant(event_time)
    event["eventTimeZoneOffset"] = _offset(event_time)
    if parent_id:
        event["parentID"] = parent_id
    event["action"] = action
    if biz_step:
        event["bizStep"] = biz_step
    if disposition:
        event["disposition"] = disposition
    if epcs:
        event["epcList"] = list(epcs)
    if quantities:
        event["quantityList"] = [dict(element) for element in quantities]
    _place(event, read_point, biz_location)
    _paperwork(event, biz_transactions, source_list, destination_list)
    if error_declaration:
        event["errorDeclaration"] = dict(error_declaration)
    return event


def transformation_event(
    *,
    event_time: datetime,
    input_epcs: Sequence[str] = (),
    input_quantities: Sequence[Mapping[str, Any]] = (),
    output_epcs: Sequence[str] = (),
    output_quantities: Sequence[Mapping[str, Any]] = (),
    transformation_id: str | None = None,
    biz_step: str | None = None,
    disposition: str | None = None,
    read_point: str | None = None,
    biz_location: str | None = None,
    biz_transactions: Sequence[tuple[str, str]] = (),
    source_list: Sequence[tuple[str, str]] = (),
    destination_list: Sequence[tuple[str, str]] = (),
    event_identifier: str | None = None,
    error_declaration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A TransformationEvent: these goods stopped being, those began, from them.

    The only event in EPCIS that carries a claim across a production step.
    Everything else describes goods that continue to exist — they arrive, they
    move, they are packed. Here the inputs cease and the outputs begin, and the
    statement worth making is that the second came out of the first. Without it
    a chain of custody ends at the factory door: the flour arrived, the bread
    left, and no query connects them.

    It has **no action**, and that is not an omission: "these came into being"
    and "these ceased to be" are both true at once, and the field could only
    say one of them.

    Both sides are required. Half a transformation is not a smaller statement
    but a different and false one — "this came from nothing", or "this became
    nothing".

    ``transformation_id`` is for a transformation reported in more than one
    event, where the inputs are known at one moment and the outputs at another:
    the same identifier on both is what ties them together. A production run
    reported in one go does not need it.
    """
    if not (input_epcs or input_quantities):
        raise ValueError(
            "a transformation needs its inputs; without them it claims the output came from nothing"
        )
    if not (output_epcs or output_quantities):
        raise ValueError(
            "a transformation needs its outputs; without them it claims the input became nothing"
        )
    event: dict[str, Any] = {"type": "TransformationEvent"}
    if event_identifier:
        event["eventID"] = event_identifier
    event["eventTime"] = _instant(event_time)
    event["eventTimeZoneOffset"] = _offset(event_time)
    if input_epcs:
        event["inputEPCList"] = list(input_epcs)
    if input_quantities:
        event["inputQuantityList"] = [dict(element) for element in input_quantities]
    if output_epcs:
        event["outputEPCList"] = list(output_epcs)
    if output_quantities:
        event["outputQuantityList"] = [dict(element) for element in output_quantities]
    if transformation_id:
        event["transformationID"] = transformation_id
    if biz_step:
        event["bizStep"] = biz_step
    if disposition:
        event["disposition"] = disposition
    _place(event, read_point, biz_location)
    _paperwork(event, biz_transactions, source_list, destination_list)
    if error_declaration:
        event["errorDeclaration"] = dict(error_declaration)
    return event


def document(
    events: Iterable[Mapping[str, Any]], creation_time: datetime | None = None
) -> dict[str, Any]:
    """Wrap events into the EPCISDocument the capture endpoint expects.

    The context sits on the document, never on the event: a repository that
    stores the event verbatim would otherwise carry a copy of it on every row.
    """
    return {
        "@context": [EPCIS_CONTEXT],
        "type": "EPCISDocument",
        "schemaVersion": "2.0",
        "creationDate": _instant(creation_time or datetime.now(timezone.utc)),
        "epcisBody": {"eventList": [dict(event) for event in events]},
    }


def stamp_event_ids(epcis_document: Mapping[str, Any]) -> dict[str, Any]:
    """Give every event in the document the identifier it already has.

    The canonical CBV event hash is computed over the finished event, so this
    runs *after* the document is built, not before: the identity is a property
    of the statement, and a statement is not complete until it is written down.
    Returns a new document; the input is left alone.

    The computation is local and takes single-digit milliseconds. That matters
    more than it sounds: the outbox needs the identifier inside the transaction
    that validates a transfer, with no network in reach, and the promise that
    reporting never waits on a repository is not one to trade away for an
    identifier.

    :raises RuntimeError: when the optional hashing dependency is not installed.
    """
    events = list(epcis_document.get("epcisBody", {}).get("eventList", []))
    if not events:
        return dict(epcis_document)

    hash_generator, json_to_py = _hashing()
    # The library keeps its namespace table in a module global and never resets
    # it. In a long-lived worker serving several tenants the prefixes of every
    # document ever hashed accumulate there, and a prefix left over from an
    # earlier document silently changes how this one canonicalises — the kind
    # of fault that shows up after weeks and only in production.
    json_to_py._namespaces.clear()

    parsed = json_to_py.event_list_from_epcis_document_str(json.dumps(epcis_document))
    hashes = hash_generator.epcis_hashes_from_events(parsed, "sha256")
    if len(hashes) != len(events):
        raise RuntimeError(
            f"the hash generator answered {len(hashes)} identifiers for {len(events)} "
            "events; refusing to guess which belongs to which"
        )

    stamped = dict(epcis_document)
    body = dict(stamped.get("epcisBody", {}))
    body["eventList"] = [
        {**event, "eventID": event_id} for event, event_id in zip(events, hashes, strict=True)
    ]
    stamped["epcisBody"] = body
    return stamped


def _hashing() -> tuple[Any, Any]:
    """The hashing library, imported where it is used.

    Kept out of the module import so a host that never captures events does not
    need the dependency at all — this client's only hard requirement is
    ``requests``, and that is worth keeping.
    """
    try:
        from epcis_event_hash_generator import hash_generator, json_to_py
    except ImportError as missing:  # pragma: no cover - exercised by the message
        raise RuntimeError(
            "Computing an eventID needs the canonical hash generator. Install this "
            "client with its 'hash' extra: pip install 'benelog-client[hash]'."
        ) from missing
    return hash_generator, json_to_py


# -- Internals --------------------------------------------------------------


def _place(event: dict[str, Any], read_point: str | None, biz_location: str | None) -> None:
    if read_point:
        event["readPoint"] = {"id": sgln(read_point)}
    if biz_location:
        event["bizLocation"] = {"id": sgln(biz_location)}


def _paperwork(
    event: dict[str, Any],
    biz_transactions: Sequence[tuple[str, str]],
    source_list: Sequence[tuple[str, str]],
    destination_list: Sequence[tuple[str, str]],
) -> None:
    if biz_transactions:
        event["bizTransactionList"] = [
            {"type": kind, "bizTransaction": reference} for kind, reference in biz_transactions
        ]
    if source_list:
        event["sourceList"] = [{"type": kind, "source": value} for kind, value in source_list]
    if destination_list:
        event["destinationList"] = [
            {"type": kind, "destination": value} for kind, value in destination_list
        ]


def _instant(moment: datetime) -> str:
    """ISO 8601 with milliseconds and a ``Z``, which is what the schema wants."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (
        moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{moment.microsecond // 1000:03d}Z"
    )


def _offset(moment: datetime) -> str:
    """The offset the event was recorded in — required, and separate from the time.

    EPCIS keeps them apart on purpose: the instant says when, the offset says
    where the observer stood. A naive datetime is treated as UTC, because a
    guess about a local zone would be silently wrong rather than loudly.
    """
    offset = moment.utcoffset()
    if offset is None:
        return "+00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _urn_segment(value: str) -> str:
    """Percent-encode what a URN's namespace-specific string cannot carry raw.

    Document numbers are full of slashes — ``WH/OUT/00007`` is an ordinary Odoo
    reference — and a slash inside a URN segment is not wrong so much as
    ambiguous. Encoding keeps one reference one segment, and reverses cleanly.
    """
    from urllib.parse import quote

    return quote(str(value), safe="!'()*-._~")


def _path_segment(value: str) -> str:
    """Percent-encode what GS1 requires and nothing else.

    A serial may legitimately contain ``/``, ``?`` and ``#``; leaving those raw
    turns one path segment into two, or into a query. Everything else in the
    GS1 character set stays readable — an over-encoded Digital Link is still
    correct but nobody can read it off a screen any more.
    """
    from urllib.parse import quote

    return quote(str(value), safe="!'()*-.,_~;=:@&$")
