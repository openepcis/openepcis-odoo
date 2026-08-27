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

**Event identity is derived, not drawn.** The ``eventID`` is a UUIDv5 over the
facts the event states, so reporting the same movement twice — a retry, a
resumed queue, a re-run of yesterday's export — produces the same identifier
and the repository can recognise the second one as the first. An event with a
random id is a new truth every time it is sent.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

#: The GS1 namespace every identifier in an event is expressed in.
ID_GS1_ORG = "https://id.gs1.org"

#: JSON-LD context of EPCIS 2.0. Belongs on the document, not on the event.
EPCIS_CONTEXT = "https://ref.gs1.org/standards/epcis/2.0.0/epcis-context.jsonld"

#: UUIDv5 namespace for event identifiers minted by this library. Private and
#: arbitrary, as RFC 4122 intends: its only job is to keep these identifiers
#: from colliding with anybody else's derived UUIDs.
EVENT_NAMESPACE = uuid.UUID("9b7d5a24-1f6e-5c8a-9e42-6a0d3b5c7e11")

#: Path order of the GTIN qualifiers, as GS1 prescribes it. Not alphabetical
#: and not the order a caller happens to pass them in: a Digital Link with its
#: qualifiers out of order is a different string for the same thing, which
#: defeats the point of a canonical form.
QUALIFIER_ORDER = ("22", "10", "21")


def instance_uri(gtin: str, lot: str | None = None, serial: str | None = None) -> str:
    """The canonical URI of a trade item, a lot of it, or a single unit.

    ``09521234000012`` alone is the model. With a lot it is an LGTIN, with a
    serial an SGTIN, and with both it is a single unit whose lot is also known.
    """
    qualifiers = {"10": lot, "21": serial}
    path = "/01/" + str(gtin).strip()
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


def event_id(*parts: object) -> str:
    """A stable ``urn:uuid`` derived from the facts an event states.

    Pass the things that make this event *this* event and no other: the
    reporting system, the document it came from, the business step, the
    identifiers. Do not pass a timestamp taken at send time, or a counter —
    they change between two reports of the same movement, which is exactly the
    case this exists to survive.
    """
    name = "|".join("" if part is None else str(part) for part in parts)
    return "urn:uuid:" + str(uuid.uuid5(EVENT_NAMESPACE, name))


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
