# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).

from odoo import _, fields, models

from ..utils.exceptions import OpenepcisError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    openepcis_enabled = fields.Boolean(related="company_id.openepcis_enabled", readonly=False)
    openepcis_base_url = fields.Char(related="company_id.openepcis_base_url", readonly=False)
    openepcis_api_key = fields.Char(related="company_id.openepcis_api_key", readonly=False)
    openepcis_api_secret = fields.Char(related="company_id.openepcis_api_secret", readonly=False)

    def action_openepcis_refresh_channels(self):
        """Re-read which registries this deployment publishes onward to.

        Worth a button rather than a background job: the answer changes when the
        platform is reconfigured, and the person who asked for that change is
        the one who wants to see it take effect.
        """
        self.ensure_one()
        self.execute()
        channels = self.env["openepcis.channel"].refresh(company=self.company_id)
        if not channels:
            message = _("No outbound destinations are configured for this tenant.")
        else:
            message = "\n".join(
                "%s — %s%s"
                % (
                    channel.name,
                    _("on") if channel.enabled else _("off"),
                    _(", dry run (nothing is sent onward)") if channel.dry_run else "",
                )
                for channel in channels
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Outbound destinations"),
                "message": message,
                "type": "success",
                "sticky": True,
            },
        }

    def action_openepcis_test_connection(self):
        """Probe the resolver and report each step, not just pass or fail.

        Every plausible failure here is a Keycloak provisioning gap with its own
        fix, so the useful answer is a list: what worked, what did not, and what
        to change. Saving first means the probe uses what is on screen.
        """
        self.ensure_one()
        self.execute()

        try:
            checks = self.env["openepcis.client"].diagnose(company=self.company_id)
        except OpenepcisError as exc:
            checks = [(_("Connection"), False, str(exc))]

        # Three outcomes, not two: None means the deployment does not offer that
        # feature, which is information rather than a fault to be fixed.
        marks = {True: "✓", False: "✗", None: "–"}
        failed = any(ok is False for _name, ok, _detail in checks)
        lines = [
            "%s %s%s" % (marks[ok], name, ": %s" % detail if detail else "")
            for name, ok, detail in checks
        ]
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("OpenEPCIS: check failed") if failed else _("OpenEPCIS connection"),
                "message": "\n".join(lines),
                "type": "warning" if failed else "success",
                # Sticky: the failing line names the claim to add in Keycloak,
                # which nobody can act on in the four seconds a toast lasts.
                "sticky": True,
            },
        }
