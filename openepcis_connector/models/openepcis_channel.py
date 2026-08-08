# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""What a downstream registry expects, cached so the form can say so.

The resolver's ``GET /sync/channels`` lists every destination master data can be
published to, and — since the channel framework grew the field — which
vocabulary terms each one requires. That is the difference between learning what
GS1 Germany wants while filling a form in and learning it from a refused push
three days later.

Cached in a table rather than fetched on demand because the answer changes about
as often as a deployment does, and a product list view would otherwise make one
HTTP call per row.
"""

import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils.exceptions import OpenepcisError

_logger = logging.getLogger(__name__)


class OpenepcisChannel(models.Model):
    _name = "openepcis.channel"
    _description = "OpenEPCIS outbound channel"
    _order = "name"

    channel_id = fields.Char(string="Identifier", required=True, index=True)
    name = fields.Char(required=True)
    company_id = fields.Many2one("res.company", required=True, index=True)
    enabled = fields.Boolean(help="Switched on for this deployment.")
    dry_run = fields.Boolean(
        help="Attempts are recorded but nothing is sent onward. The platform "
        "ships with this on, because there is no GS1 sandbox to practise against."
    )
    configured = fields.Boolean(help="Credentials for this tenant are deposited.")
    required_terms_json = fields.Text(
        string="Required terms",
        help="Per record kind, the vocabulary terms this destination insists on.",
    )
    fetched_on = fields.Datetime(readonly=True)

    _channel_company_unique = models.Constraint(
        "unique (channel_id, company_id)",
        "A channel is listed once per company.",
    )

    # ------------------------------------------------------------------

    @api.model
    def refresh(self, company=None):
        """Re-read the channel list from the resolver for one company."""
        company = company or self.env.company
        try:
            channels = self.env["openepcis.client"].get("/sync/channels", company=company)
        except OpenepcisError as exc:
            raise UserError(_("The channel list could not be read: %s", exc)) from exc

        now = fields.Datetime.now()
        seen = self.env["openepcis.channel"]
        for entry in channels or []:
            identifier = entry.get("id")
            if not identifier:
                continue
            values = {
                "name": entry.get("displayName") or identifier,
                "enabled": bool(entry.get("enabled")),
                "dry_run": bool(entry.get("dryRun")),
                "configured": bool(entry.get("configured")),
                "required_terms_json": json.dumps(entry.get("requiredTerms") or {}),
                "fetched_on": now,
            }
            existing = self.search(
                [("channel_id", "=", identifier), ("company_id", "=", company.id)], limit=1
            )
            if existing:
                existing.write(values)
                seen |= existing
            else:
                seen |= self.create(dict(values, channel_id=identifier, company_id=company.id))

        # A channel the deployment has dropped should stop being advertised.
        stale = self.search([("company_id", "=", company.id)]) - seen
        stale.unlink()
        self.env.registry.clear_cache()
        return seen

    @api.model
    def required_terms(self, kind, company=None):
        """Local names of the terms every enabled channel requires for ``kind``.

        Returned without their ``gs1:`` prefix, because that is how they appear
        in a mapping row's target path.
        """
        company = company or self.env.company
        terms = set()
        channels = self.sudo().search([("company_id", "=", company.id), ("enabled", "=", True)])
        for channel in channels:
            try:
                by_kind = json.loads(channel.required_terms_json or "{}")
            except ValueError:
                _logger.warning("Channel %s has unreadable required terms", channel.channel_id)
                continue
            for term in by_kind.get(kind) or []:
                terms.add(term.split(":")[-1])
        return terms
