# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""When an operation type becomes a till, its meaning changes with it.

The bridge next door knows what a till means. It had no way of being asked:
the seeding of an operation type recomputes on the operation's own codes, and
pointing a point-of-sale configuration at one changes none of them. So a till
configured after the fact kept reporting its sales as shipments, and the
override sat there correct and unreached.

This is the trigger. It fires where the fact appears — a configuration gains a
picking type — and again at install, for the tills that were already there.
"""

from odoo import api, models


class PosConfig(models.Model):
    _inherit = "pos.config"

    @api.model_create_multi
    def create(self, vals_list):
        configs = super().create(vals_list)
        configs.picking_type_id._openepcis_reseed_for_till()
        return configs

    def write(self, vals):
        was = self.picking_type_id
        result = super().write(vals)
        if "picking_type_id" in vals:
            # Both ends: the new till learns what it is, and the old one — if
            # nothing else points at it — goes back to being a loading bay.
            (was | self.picking_type_id)._openepcis_reseed_for_till()
        return result
