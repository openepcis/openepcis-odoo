# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The inbox: what other people's systems have said about our things.

A partner's EPCIS event is an *observation*, not a document. It carries a
foreign clock, a foreign reading of what a business step means, and no promise
that it will ever be corrected. Odoo's stock, by contrast, is a valued figure
that belongs to a closing: every movement creates valuation layers and ends up
in the books. Posting an observation into that would make the closing depend on
a third party's data quality, and the only way back out would be a reversal
mechanism nobody wants to design.

So the inbox does not move stock. It makes events *visible* — on the lot, on
the package, and eventually in the traceability report, where a line from
somebody else is exactly what Odoo's own report cannot show. That is the whole
point of EPCIS: the report stops at the company boundary, and the events do not.

Deliberately a model of its own rather than a direction flag on
``openepcis.event``. The two share almost no field, no state machine and no
duty; a flag would have put a case distinction into every method of the outbox.
"""

import json
import logging

from odoo import _, api, fields, models

from ..vendored import BenelogError

logger = logging.getLogger(__name__)

#: Events per scheduled run, when a company states no preference of its own.
BATCH = 200

#: Pages the catch-up will walk in one run. A repository that keeps handing out
#: a next-page token must not turn one scheduled run into an unbounded one.
PAGES = 20


class OpenepcisInboundEvent(models.Model):
    _name = "openepcis.inbound.event"
    _description = "OpenEPCIS event received from the repository"
    _order = "event_time desc, id desc"

    name = fields.Char(required=True, readonly=True, index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    event_uuid = fields.Char(
        string="Event ID",
        required=True,
        readonly=True,
        index=True,
        help="The repository's own event identifier. Derived from what the event "
        "states, so the same event read twice is recognised as one.",
    )
    event_time = fields.Datetime(readonly=True, index=True)
    record_time = fields.Datetime(
        readonly=True,
        index=True,
        help="When the repository wrote it. This, not the event time, is what the "
        "catch-up walks forward on.",
    )
    biz_step = fields.Char(string="Business step", readonly=True)
    disposition = fields.Char(readonly=True)
    party_gln = fields.Char(string="Reported by", readonly=True)
    epc_ref = fields.Char(
        string="Identifier",
        readonly=True,
        index=True,
        help="The identifier this event is about, kept even when nothing in this "
        "database answers to it.",
    )
    payload = fields.Text(required=True, readonly=True)
    state = fields.Selection(
        [
            ("received", "Received"),
            ("matched", "Matched"),
            ("posted", "Shown"),
            ("proposed", "Waiting for confirmation"),
            ("booked", "Posted"),
            ("unmatched", "Unknown identifier"),
            ("ignored", "Our own"),
            ("failed", "Failed"),
        ],
        default="received",
        required=True,
        readonly=True,
        index=True,
        help="Unknown identifier is not a failure: a lot created tomorrow should "
        "still find its earlier life waiting.",
    )
    error = fields.Char(readonly=True, copy=False)
    res_model = fields.Char(string="Related model", readonly=True)
    res_id = fields.Integer(string="Related record", readonly=True)

    _sql_constraints = [
        (
            "inbound_event_uuid_unique",
            "unique(company_id, event_uuid)",
            "This event has already been received.",
        ),
    ]

    # ------------------------------------------------------------------
    # Catching up
    # ------------------------------------------------------------------

    @api.model
    def _cron_poll(self):
        """Read what the repository has recorded since we last looked."""
        client = self.env["openepcis.client"]
        for company in self._companies():
            try:
                query = client.with_company(company)._epcis_query(company)
            except Exception as error:  # a misconfigured company must not stop the rest
                logger.warning("OpenEPCIS inbox: no query service for %s (%s)", company.display_name, error)
                continue
            self._poll_company(company, query)

    @api.model
    def _poll_company(self, company, query):
        settings = company.sudo()
        since = settings.openepcis_events_since
        watermark = self._watermark(since)
        batch = settings.openepcis_inbound_batch or BATCH
        pages = settings.openepcis_inbound_pages or PAGES
        newest = None
        seen = 0
        kept = 0
        try:
            for event in query.since(watermark, per_page=100, pages=pages):
                if seen >= batch:
                    break
                seen += 1
                recorded_first = event.get("recordTime")
                if recorded_first and (newest is None or recorded_first > newest):
                    newest = recorded_first
                if not self._in_scope(company, event):
                    # Out of scope still moves the watermark: it was read and
                    # judged, and asking for it again next run would only make
                    # the same judgement more slowly.
                    continue
                kept += 1
                row = self._receive(company, event)
                if row and row.state == "received":
                    row._resolve()
        except BenelogError as error:
            # A repository that cannot be read is not an empty repository. Leave
            # the watermark where it is; the next run asks for the same window.
            logger.warning("OpenEPCIS inbox: %s could not be read (%s)", company.display_name, error)
            return
        if newest:
            # Only after the run got through. A watermark advanced before the
            # work skips whatever the failure swallowed.
            company.sudo().openepcis_events_since = newest
        logger.info(
            "OpenEPCIS inbox: %s read %s events, kept %s", company.display_name, seen, kept
        )

    @staticmethod
    def _watermark(since):
        """Where to start reading, stepped back a little.

        Assigning a record time and making an event visible to queries are two
        steps in the repository, not one, so an event can be written just before
        the mark and become findable just after it. Without the overlap it falls
        through that gap for good. Re-reading costs nothing: the event id is
        derived from the event's own facts, so seeing it twice is not the same
        as it happening twice.
        """
        if not since:
            return ""
        from datetime import datetime, timedelta, timezone

        try:
            parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return ""
        return (parsed - timedelta(minutes=5)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    # ------------------------------------------------------------------
    # Receiving
    # ------------------------------------------------------------------

    @api.model
    def _in_scope(self, company, event):
        """Whether this event is any of our business.

        A repository shared along a chain hands out whatever the credential may
        see, and in a busy chain that is mostly other people's goods. Filtering
        happens here rather than in the query because the repository matches
        identifiers whole: there is no prefix search to ask for "everything
        under our prefix", and a wildcard is not an identifier. Reading a row
        and dropping it is cheap; the expensive part — resolving it, telling
        somebody about it — is what this skips.
        """
        scope = company.sudo().openepcis_inbound_scope or "all"
        if scope == "all":
            return True
        identifier = self._first_identifier(event)
        parts = self._parse_identifier(identifier)
        if not parts:
            # Nothing we can read is nothing we can place, and a narrowed scope
            # is a request not to be shown that.
            return False
        kind, key, _qualifier = parts
        if scope == "own_gcp":
            prefix = (company.sudo().openepcis_gcp or "").strip()
            if not prefix:
                # Asked to narrow to our own prefix without stating one. Keeping
                # everything would quietly ignore the setting; keeping nothing
                # would quietly empty the inbox. Keep everything and say so.
                logger.warning(
                    "OpenEPCIS inbox: %s narrows to its own prefix but has none set",
                    company.display_name,
                )
                return True
            return self._bears_prefix(kind, key, prefix)
        return bool(self.sudo()._subject_of(kind, key, _qualifier))

    @staticmethod
    def _bears_prefix(kind, key, prefix):
        """Whether a GS1 key was issued under this company prefix.

        Where the prefix begins depends on the key, and getting that wrong
        silently empties an inbox rather than raising anything — so both forms
        are tried rather than one assumed. A GTIN-14 carries an indicator digit
        in front, a GTIN-13 does not, and Digital Links are minted both ways in
        the wild. An SSCC always has its extension digit in front.
        """
        digits = "".join(c for c in str(key) if c.isdigit())
        if kind == "gtin":
            return digits.startswith(prefix) or digits[1:].startswith(prefix)
        if kind == "sscc":
            return digits[1:].startswith(prefix)
        return False

    def _subject_of(self, kind, key, qualifier):
        """Resolve an identifier without a row — used before one is created."""
        if kind == "sscc":
            return self.env["stock.quant.package"].sudo().search(
                [("openepcis_sscc", "=", key)], limit=1
            ) or None
        product = self.env["product.product"].sudo().search([("barcode", "=", key)], limit=1)
        if not product or not qualifier:
            return product or None
        return self.env["stock.lot"].sudo().search(
            [("product_id", "=", product.id), ("name", "=", qualifier)], limit=1
        ) or None

    @api.model
    def _receive(self, company, event):
        """Record one event, or leave the existing row alone."""
        uuid = event.get("eventID")
        if not uuid:
            # Without an identifier there is no idempotency, and an inbox
            # without idempotency writes the same event on every run.
            return self.browse()
        existing = self.sudo().search(
            [("company_id", "=", company.id), ("event_uuid", "=", uuid)], limit=1
        )
        if existing:
            return existing
        state = "ignored" if self._is_our_own(company, uuid) else "received"
        return self.sudo().create(
            {
                "name": event.get("bizStep") or event.get("type") or _("Event"),
                "company_id": company.id,
                "event_uuid": uuid,
                "event_time": self._naive_utc(event.get("eventTime")),
                "record_time": self._naive_utc(event.get("recordTime")),
                "biz_step": event.get("bizStep"),
                "disposition": event.get("disposition"),
                "party_gln": self._reported_by(event),
                "epc_ref": self._first_identifier(event),
                "payload": json.dumps(event, indent=1, sort_keys=False),
                "state": state,
            }
        )

    @api.model
    def _is_our_own(self, company, uuid):
        """Whether we sent this event ourselves.

        The outbox derives its event ids the same way the repository keeps them,
        so our own events come back verbatim. Telling the story twice — once as
        a transfer, once as news from outside — would be worse than useless.
        """
        return bool(
            self.env["openepcis.event"]
            .sudo()
            .search_count([("company_id", "=", company.id), ("event_uuid", "=", uuid)])
        )

    def _resolve(self):
        """Find what in this database the event is about, if anything."""
        self.ensure_one()
        record = self._find_subject()
        if not record:
            self.sudo().write({"state": "unmatched"})
            return
        self.sudo().write({"state": "matched", "res_model": record._name, "res_id": record.id})
        self._tell(record)
        # Showing comes first and always happens; posting is the exception that
        # has to argue for itself, and it argues after the fact is on record.
        self._consider_posting(record)

    def _find_subject(self):
        """The lot or package an identifier belongs to, if this database has one.

        The identifier is not stored anywhere to look up: a lot *composes* its
        Digital Link out of the product's barcode and its own name. So the way
        back is to take it apart again — GTIN to the product, the qualifier to
        the lot — rather than to add a second, redundant field that would then
        have to be kept in step.
        """
        self.ensure_one()
        parts = self._parse_identifier(self.epc_ref)
        if not parts:
            return None
        return self._subject_of(*parts)

    @staticmethod
    def _parse_identifier(identifier):
        """Take a GS1 Digital Link apart: what kind, which key, which qualifier.

        Only the two forms this connector ever mints are read — an SSCC, and a
        GTIN with a lot or a serial. Anything else comes back as nothing, which
        leaves the row unmatched and visible rather than guessed at.
        """
        if not identifier:
            return None
        segments = [s for s in str(identifier).split("/") if s]
        pairs = {}
        for index in range(len(segments) - 1):
            if segments[index].isdigit() and len(segments[index]) in (2, 3):
                pairs[segments[index]] = segments[index + 1]
        if "00" in pairs:
            return ("sscc", pairs["00"], None)
        if "01" in pairs:
            return ("gtin", pairs["01"], pairs.get("21") or pairs.get("10"))
        return None

    def _tell(self, record):
        """Say it where somebody will see it.

        The chatter of the lot is the cheapest useful step: it is where the
        person handling that lot already looks, and it costs no new screen.
        """
        self.ensure_one()
        if not hasattr(record, "message_post"):
            return
        record.sudo().message_post(
            body=_(
                "EPCIS: %(party)s reported %(step)s on %(when)s.",
                party=self.party_gln or _("a partner"),
                step=self.biz_step or _("an event"),
                when=self.event_time or _("an unstated date"),
            )
        )
        self.sudo().write({"state": "posted"})

    # ------------------------------------------------------------------
    # Reading an event
    # ------------------------------------------------------------------

    @staticmethod
    def _first_identifier(event):
        for key in ("epcList", "childEPCs", "inputEPCList", "outputEPCList"):
            values = event.get(key) or []
            if values:
                return values[0]
        return event.get("parentID")

    @staticmethod
    def _sscc_of(identifier):
        """The SSCC out of a Digital Link URI, if that is what this is."""
        if not identifier or "/00/" not in identifier:
            return None
        return identifier.rsplit("/00/", 1)[1].split("/")[0]

    @staticmethod
    def _reported_by(event):
        for source in event.get("sourceList") or []:
            if str(source.get("type", "")).endswith("owning_party"):
                return source.get("source")
        return None

    @staticmethod
    def _naive_utc(moment):
        """An EPCIS instant as Odoo stores datetimes: naive, and in UTC."""
        if not moment:
            return False
        from datetime import datetime, timezone

        try:
            parsed = datetime.fromisoformat(str(moment).replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)

    @api.model
    def _companies(self):
        return self.env["res.company"].sudo().search(
            [("openepcis_events_enabled", "=", True), ("openepcis_epcis_url", "!=", False)]
        )
