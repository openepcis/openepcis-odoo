# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Let mapping rows target lots, so instance fields stay data rather than code."""

from odoo import api, models


class OpenepcisFieldMapping(models.Model):
    _inherit = "openepcis.field.mapping"

    @api.model
    def _selection_model_name(self):
        return super()._selection_model_name() + [("stock.lot", "Lot/Serial number")]
