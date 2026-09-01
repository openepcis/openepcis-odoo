# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).

from odoo import _, api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    openepcis_events_enabled = fields.Boolean(
        related="company_id.openepcis_events_enabled", readonly=False
    )
    openepcis_epcis_url = fields.Char(related="company_id.openepcis_epcis_url", readonly=False)
    openepcis_gcp = fields.Char(related="company_id.openepcis_gcp", readonly=False)
    openepcis_inbound_scope = fields.Selection(
        related="company_id.openepcis_inbound_scope", readonly=False
    )
    openepcis_inbound_batch = fields.Integer(
        related="company_id.openepcis_inbound_batch", readonly=False
    )
    openepcis_inbound_pages = fields.Integer(
        related="company_id.openepcis_inbound_pages", readonly=False
    )
    openepcis_inbound_minutes = fields.Integer(
        string="Read every (minutes)",
        compute="_compute_openepcis_inbound_minutes",
        inverse="_inverse_openepcis_inbound_minutes",
        help="How often to ask the repository what is new. The right number "
        "follows from what the events are for: a dashboard that people watch "
        "wants minutes, a nightly reconciliation is happy with hours. Asking "
        "more often costs a query, never a missed event — the watermark means "
        "a slow reader falls behind, not blind.",
    )

    def _openepcis_inbound_cron(self):
        return self.env.ref(
            "openepcis_connector_events.cron_poll_inbound_events", raise_if_not_found=False
        )

    @api.depends_context("company")
    def _compute_openepcis_inbound_minutes(self):
        cron = self._openepcis_inbound_cron()
        minutes = 0
        if cron:
            factor = {"minutes": 1, "hours": 60, "days": 1440}.get(cron.interval_type, 1)
            minutes = (cron.interval_number or 0) * factor
        for record in self:
            record.openepcis_inbound_minutes = minutes

    def _inverse_openepcis_inbound_minutes(self):
        """Write the interval back onto the scheduled action itself.

        Rather than keeping a second copy of the number on the company and
        syncing the two. The cron record is where Odoo already keeps this, it
        is what the scheduler actually reads, and a settings page that edits it
        directly cannot drift out of step with it.
        """
        cron = self._openepcis_inbound_cron()
        if not cron:
            return
        for record in self:
            minutes = max(1, record.openepcis_inbound_minutes or 1)
            if minutes % 60 == 0 and minutes >= 60:
                cron.sudo().write({"interval_number": minutes // 60, "interval_type": "hours"})
            else:
                cron.sudo().write({"interval_number": minutes, "interval_type": "minutes"})

    def action_openepcis_test_capture(self):
        """Ask the repository whether it would accept events from here.

        A separate button from the resolver's test, because they are separate
        services with separate permissions: the credential that publishes
        master data can be perfectly valid here and still be refused capture.
        """
        self.ensure_one()
        self.execute()
        problem = self.env["openepcis.client"]._epcis_check(self.company_id)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning" if problem else "success",
                "title": _("EPCIS repository"),
                "message": problem or _("Ready to accept events."),
                "sticky": bool(problem),
            },
        }
