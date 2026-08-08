# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Drawing GTINs and GLNs from your own GS1 company prefix.

This is the part that turns the connector from a synchroniser into something
worth installing: a product with no barcode is one button away from a real,
registered GTIN.

**Why drawing and registering are two separate acts.** Registration at GS1 is
irreversible — an identifier with no product data behind it can be neither
deleted nor deactivated. A number drawn for a form somebody then abandons would
be burnt for good. So the resolver hands out a *candidate*, held in its own
ledger and invisible to GS1, and only registers it when the record using it is
actually saved. This module mirrors that: draw fills the barcode in, publishing
confirms, and abandoning hands the number back.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils.exceptions import OpenepcisError

_logger = logging.getLogger(__name__)

#: Reading GS1's high-water mark means sorting a whole prefix range upstream,
#: which takes far longer than an ordinary catalog call.
DRAW_TIMEOUT = (5, 90)


class OpenepcisKeyPoolMixin(models.AbstractModel):
    """Drawing behaviour, mixed into whatever holds a GS1 key."""

    _name = "openepcis.key.pool.mixin"
    _description = "OpenEPCIS key pool"

    openepcis_key_state = fields.Selection(
        [
            ("own", "Own number"),
            ("candidate", "Drawn, not yet registered"),
            ("registered", "Registered with GS1"),
        ],
        string="Identifier origin",
        default="own",
        readonly=True,
        copy=False,
        help="Where this record's identifier came from. A drawn number can be "
        "handed back until it is registered; afterwards it belongs to you for good.",
    )

    # ------------------------------------------------------------------
    # Provided by the concrete model
    # ------------------------------------------------------------------

    def _openepcis_key_field(self):
        """Name of the field holding the key, so drawing can fill it in."""
        raise NotImplementedError

    def _openepcis_draw_ai(self):
        """Application identifier to draw under — ``01``, ``414`` or ``417``."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # The three acts
    # ------------------------------------------------------------------

    def action_openepcis_draw_key(self):
        """Take the next free number from the company prefix and hold it."""
        self.ensure_one()
        field = self._openepcis_key_field()
        if self[field]:
            raise UserError(
                _(
                    "This record already has an identifier (%s). Clear it first "
                    "if you really mean to draw another.",
                    self[field],
                )
            )

        row = self._openepcis_pool_call(
            "post", "/gs1de/keys/draw", payload={"ai": self._openepcis_draw_ai()}
        )
        key = (row or {}).get("key")
        if not key:
            raise UserError(_("The resolver returned no identifier."))

        self.with_context(openepcis_syncing=True).write(
            {field: key, "openepcis_key_state": "candidate"}
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Identifier drawn"),
                "message": _(
                    "%(key)s is held for this record. It is registered with GS1 "
                    "when the record is published — until then you can hand it back.",
                    key=key,
                ),
                "type": "success",
                "sticky": True,
            },
        }

    def action_openepcis_release_key(self):
        """Hand a held number back. Only possible while it is a candidate."""
        self.ensure_one()
        field = self._openepcis_key_field()
        if self.openepcis_key_state != "candidate":
            raise UserError(
                _(
                    "Only a drawn, unregistered identifier can be handed back. "
                    "Once GS1 holds it, it is yours for good."
                )
            )
        self._openepcis_pool_call(
            "delete", "/gs1de/keys/%s/%s" % (self._openepcis_draw_ai(), self[field])
        )
        self.with_context(openepcis_syncing=True).write(
            {field: False, "openepcis_key_state": "own", "openepcis_state": "not_synced"}
        )

    def _openepcis_confirm_key(self):
        """Register a held number, now that the record using it exists.

        Called after a successful publication. A failure here is logged rather
        than raised: the record *is* published, and telling the user it failed
        because a follow-up call did would be untrue. The resolver's confirm is
        idempotent, so the next publication tries again.
        """
        for record in self:
            if record.openepcis_key_state != "candidate":
                continue
            key = record[record._openepcis_key_field()]
            try:
                record._openepcis_pool_call(
                    "post", "/gs1de/keys/%s/%s/confirm" % (record._openepcis_draw_ai(), key)
                )
            except (OpenepcisError, UserError) as exc:
                _logger.warning(
                    "OpenEPCIS: %s published but identifier %s not yet registered: %s",
                    record.display_name,
                    key,
                    exc,
                )
                continue
            record.with_context(openepcis_syncing=True).openepcis_key_state = "registered"

    # ------------------------------------------------------------------

    def _openepcis_pool_call(self, verb, path, payload=None):
        """One key-pool call, with the failures phrased for whoever pressed the button."""
        client = self.env["openepcis.client"]
        company = self._openepcis_company()
        try:
            if verb == "post":
                return client.post(path, payload, company=company, timeout=DRAW_TIMEOUT)
            if verb == "delete":
                return client.delete(path, company=company, timeout=DRAW_TIMEOUT)
            return client.get(path, company=company, timeout=DRAW_TIMEOUT)
        except OpenepcisError as exc:
            raise UserError(self._openepcis_pool_message(exc)) from exc

    @api.model
    def _openepcis_pool_message(self, error):
        if error.is_missing_claim:
            return _(
                "%(detail)s\n\nDrawing identifiers needs the gs1CompanyPrefix claim "
                "on the API key's identity, and a GS1 licence deposited for this tenant.",
                detail=error.message,
            )
        if error.status == 409:
            return _(
                "No GS1 licence is registered for this tenant, so there is no "
                "company prefix to draw from."
            )
        if error.status == 403:
            return _("This identity is not allowed to draw identifiers for the tenant.")
        return str(error)
