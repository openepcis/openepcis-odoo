# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).

from odoo import _, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    openepcis_events_enabled = fields.Boolean(
        related="company_id.openepcis_events_enabled", readonly=False
    )
    openepcis_epcis_url = fields.Char(related="company_id.openepcis_epcis_url", readonly=False)
    openepcis_gcp = fields.Char(related="company_id.openepcis_gcp", readonly=False)

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
