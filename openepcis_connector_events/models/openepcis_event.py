# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The outbox: events wait here until the repository has them.

Validating a transfer must never wait for a network. A warehouse worker
pressing Validate is not in a position to do anything about an unreachable
repository, and blocking them on it turns somebody else's outage into a stopped
loading bay. So the hook writes rows and returns; a scheduled action delivers
them.

Rows are worth keeping after delivery, and this is the part that is easy to get
wrong. Capture is asynchronous: the repository answers *accepted*, validates
afterwards, and may still refuse. A queue that deletes on 202 reports success
for events that were thrown away minutes later. So a row carries its capture
job and is asked again on the next run, and only then does it say what really
happened.

Two identifiers are in play and they answer different questions. The
``idem_key`` is this database's own handle on a movement it has reported — a
UUIDv5 over the transfer, the business step and the identifiers, and the unique
index below, so the same movement cannot be queued twice. The ``event_hash`` is
what the event is *called*: the canonical CBV hash the repository computes over
the event itself.

We do not send that hash. The document leaves here without an ``eventID`` and
the repository fills it in, because there must be exactly one canonicalisation
and it is the one that stores the event. The hash computed here is a
*comparison* value only, for recognising our own events when they come back
through the inbox: if the two implementations ever disagree the worst that
happens is a missed echo — an event of ours shown as news — where a sent
identifier would have been wrong and permanent.
"""

import json
import logging
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..vendored import BenelogError, stamp_event_ids

logger = logging.getLogger(__name__)

#: Rows per scheduled run. Large enough that a day's warehouse traffic drains
#: in one go, small enough that a wedged repository cannot hold a cron worker
#: for minutes.
BATCH = 100

#: How often a row asks after a capture job before it gives up asking. The
#: answer "I do not know that job" does not improve with repetition, and a
#: queue that keeps asking turns one unanswerable delivery into a permanent
#: background load.
SETTLE_ATTEMPTS = 5


#: What the repository answers when it already holds an event
#: (EPCISEventValidationService: "Duplicate EPCIS Event"). Matched on the
#: phrase because the answer is a 400 like any other validation refusal — the
#: status alone cannot tell the two apart.
DUPLICATE_MARKERS = ("duplicate epcis event", "duplicate event")


def _is_duplicate(error):
    text = str(error).lower()
    return any(marker in text for marker in DUPLICATE_MARKERS)


def _naive_utc(moment):
    """An EPCIS instant as Odoo stores datetimes: naive, and in UTC.

    Odoo keeps every datetime naive and reads it as UTC; EPCIS writes an
    explicit offset. Dropping the ``Z`` and hoping is how a timestamp ends up an
    hour out — the offset is applied first, then discarded.
    """
    parsed = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


class OpenepcisEvent(models.Model):
    _name = "openepcis.event"
    _description = "OpenEPCIS visibility event"
    _order = "event_time desc, id desc"

    name = fields.Char(required=True, readonly=True, index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True, index=True)
    idem_key = fields.Char(
        string="Idempotency key",
        required=True,
        readonly=True,
        index=True,
        help="This database's handle on the movement, derived from the transfer, "
        "the business step and the identifiers — so the same movement cannot be "
        "queued twice. Not the event's identifier.",
    )
    event_hash = fields.Char(
        string="Expected event ID",
        readonly=True,
        index=True,
        help="The canonical CBV hash of this event, computed here for comparison "
        "only: it is how an event of ours is recognised when the inbox reads it "
        "back. The document is sent without an eventID; the repository assigns it.",
    )
    event_time = fields.Datetime(required=True, readonly=True, index=True)
    biz_step = fields.Char(string="Business step", readonly=True)
    epc_count = fields.Integer(string="Identifiers", readonly=True)
    payload = fields.Text(required=True, readonly=True)
    state = fields.Selection(
        [
            ("queued", "Waiting"),
            ("accepted", "Accepted"),
            ("captured", "Captured"),
            ("rejected", "Refused"),
            ("failed", "Failed"),
        ],
        default="queued",
        required=True,
        readonly=True,
        index=True,
        help="Accepted means the repository took custody of the document; "
        "captured, that it stored it. They are not the same answer.",
    )
    job = fields.Char(string="Capture job", readonly=True, copy=False)
    error = fields.Char(readonly=True, copy=False)
    attempts = fields.Integer(readonly=True, default=0)
    res_model = fields.Char(string="Source model", readonly=True)
    res_id = fields.Integer(string="Source", readonly=True)

    _sql_constraints = [
        (
            "idem_key_unique",
            "unique(company_id, idem_key)",
            "This movement has already been reported.",
        ),
    ]

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------

    @api.model
    def queue(self, epcis_document, subject, company, idem_key, source=None):
        """Put one document in the outbox, or leave the existing row alone.

        Returns the row, existing or new. A movement that is already queued is
        not queued again and not overwritten: the first report is the one that
        matters, and re-reporting only ever happens by accident.
        """
        events = (epcis_document.get("epcisBody") or {}).get("eventList") or []
        if not events:
            return self.browse()
        event = events[0]
        existing = self.sudo().search(
            [("company_id", "=", company.id), ("idem_key", "=", idem_key)], limit=1
        )
        if existing:
            return existing
        return self.sudo().create(
            {
                "name": subject,
                "company_id": company.id,
                "idem_key": idem_key,
                "event_hash": self._expected_event_id(epcis_document),
                "event_time": _naive_utc(event["eventTime"]),
                "biz_step": event.get("bizStep"),
                "epc_count": len(event.get("epcList") or [])
                + len(event.get("quantityList") or [])
                + len(event.get("childEPCs") or []),
                "payload": json.dumps(epcis_document, indent=1, sort_keys=False),
                "res_model": source and source._name,
                "res_id": source and source.id,
            }
        )

    @api.model
    def _expected_event_id(self, epcis_document):
        """What the repository will call this event, as far as we can tell.

        Comparison only — see the module docstring. A failure here is not a
        reason to refuse a transfer: without the value the inbox merely fails
        to recognise one of our own events later, which shows as news rather
        than as silence.
        """
        try:
            stamped = stamp_event_ids(epcis_document)
            return stamped["epcisBody"]["eventList"][0].get("eventID") or False
        except Exception:  # noqa: BLE001 — an identifier we only compare with
            logger.warning("OpenEPCIS: could not compute the expected event id", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    @api.model
    def _cron_capture(self):
        """Deliver what is waiting, then find out what became of what was sent."""
        self._deliver_queued()
        self._settle_accepted()

    @api.model
    def _deliver_queued(self):
        client = self.env["openepcis.client"]
        for company in self._companies():
            capture = client.with_company(company)._epcis_capture(company)
            rows = self.sudo().search(
                [("company_id", "=", company.id), ("state", "in", ("queued", "failed"))],
                limit=BATCH,
            )
            for row in rows:
                row._deliver(capture)

    def _deliver(self, capture):
        self.ensure_one()
        try:
            receipt = capture.submit(json.loads(self.payload))
        except BenelogError as error:
            if _is_duplicate(error):
                # The repository already holds this event. That is not a
                # failure, it is the answer we were hoping for: the retry
                # arrived at a repository that had taken the first attempt
                # after all. Booking it as refused would leave the queue full
                # of rows that look broken and are not.
                self.sudo().write(
                    {
                        "state": "captured",
                        "error": False,
                        "attempts": self.attempts + 1,
                    }
                )
                logger.info("OpenEPCIS event %s: the repository already holds it", self.name)
                return
            # Refused outright: nothing was stored, and the row stays for the
            # next run. The message is the repository's own — a validation
            # failure names a field, and paraphrasing loses the field.
            self.sudo().write(
                {
                    "state": "failed",
                    "error": str(error)[:500],
                    "attempts": self.attempts + 1,
                }
            )
            logger.warning("OpenEPCIS event %s refused: %s", self.name, error)
            return
        self.sudo().write(
            {
                "state": "accepted" if receipt.answerable else "captured",
                "job": receipt.job,
                "error": False,
                "attempts": self.attempts + 1,
            }
        )

    @api.model
    def _settle_accepted(self):
        client = self.env["openepcis.client"]
        for company in self._companies():
            capture = client.with_company(company)._epcis_capture(company)
            rows = self.sudo().search(
                [
                    ("company_id", "=", company.id),
                    ("state", "=", "accepted"),
                    ("job", "!=", False),
                    ("attempts", "<", SETTLE_ATTEMPTS),
                ],
                limit=BATCH,
            )
            for row in rows:
                row._settle(capture)

    def _settle(self, capture):
        self.ensure_one()
        try:
            outcome = capture.outcome(self.job)
        except BenelogError as error:
            logger.info("OpenEPCIS event %s: job not answerable yet (%s)", self.name, error)
            return
        if not outcome.settled:
            return
        if not outcome.known:
            # The repository cannot say what became of it. That is not a
            # success: a refused document answers exactly the same way. The row
            # stays accepted — the document was taken into custody, and that
            # much is true — and says so rather than turning green on a guess.
            self.sudo().write(
                {
                    "attempts": self.attempts + 1,
                    "error": _(
                        "The repository does not recognise this capture job, so whether "
                        "the event was stored cannot be confirmed. It was accepted."
                    )
                    if self.attempts + 1 >= SETTLE_ATTEMPTS
                    else False,
                }
            )
            return
        self.sudo().write(
            {
                "state": "captured" if outcome.success else "rejected",
                "error": "; ".join(outcome.errors)[:500] or False,
            }
        )

    @api.model
    def _companies(self):
        client = self.env["openepcis.client"]
        return self.env["res.company"].sudo().search([]).filtered(client._epcis_configured)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_retry(self):
        """Send a refused or failed row again.

        Safe by construction: the identifier is derived, so a document that did
        land is recognised by the repository rather than stored twice.
        """
        for row in self:
            if row.state in ("captured",):
                raise UserError(_("%s was captured; there is nothing to send again.", row.name))
        self.sudo().write({"state": "queued", "job": False})
        self._cron_capture()
        return True

    def action_open_source(self):
        self.ensure_one()
        if not self.res_model or not self.res_id:
            raise UserError(_("This event has no source record."))
        return {
            "type": "ir.actions.act_window",
            "res_model": self.res_model,
            "res_id": self.res_id,
            "view_mode": "form",
        }
