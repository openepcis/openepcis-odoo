# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Which operation means which business step — as data, not as a rule in code.

Two warehouses that both run Odoo do not agree on what their operation types
mean. One company's "Quality Control" is an inspection; another's is a staging
step that happens to be called that. A connector that decided this in code
would be right for the demo and wrong for the second customer.

So the mapping is a field per operation type, seeded from what Odoo's own codes
strongly imply and editable afterwards. The seeding is a courtesy, not a claim:
somebody who changes it is not fighting the connector.
"""

from odoo import api, fields, models

from ..vendored import cbv

#: What Odoo's operation codes usually mean, as (business step, disposition).
#: ``sequence_code`` refines the internal ones, which are otherwise all alike.
DEFAULTS_BY_CODE = {
    "incoming": (cbv.RECEIVING, cbv.IN_PROGRESS),
    "outgoing": (cbv.SHIPPING, cbv.IN_TRANSIT),
    "internal": (cbv.STORING, cbv.IN_PROGRESS),
    "mrp_operation": (cbv.COMMISSIONING, cbv.ACTIVE),
}

DEFAULTS_BY_SEQUENCE = {
    "PICK": (cbv.PICKING, cbv.IN_PROGRESS),
    "PACK": (cbv.PACKING, cbv.IN_PROGRESS),
    "QC": (cbv.INSPECTING, cbv.IN_PROGRESS),
    "STOR": (cbv.STORING, cbv.SELLABLE_ACCESSIBLE),
}

BIZ_STEPS = [
    (cbv.RECEIVING, "Receiving"),
    (cbv.SHIPPING, "Shipping"),
    (cbv.PICKING, "Picking"),
    (cbv.PACKING, "Packing"),
    (cbv.UNPACKING, "Unpacking"),
    (cbv.STORING, "Storing"),
    (cbv.INSPECTING, "Inspecting"),
    (cbv.LOADING, "Loading"),
    (cbv.UNLOADING, "Unloading"),
    (cbv.STOCK_TAKING, "Stock taking"),
    (cbv.RETAIL_SELLING, "Retail selling"),
    (cbv.COMMISSIONING, "Commissioning"),
    (cbv.DECOMMISSIONING, "Decommissioning"),
    (cbv.TRANSPORTING, "Transporting"),
]

DISPOSITIONS = [
    (cbv.ACTIVE, "Active"),
    (cbv.IN_PROGRESS, "In progress"),
    (cbv.IN_TRANSIT, "In transit"),
    (cbv.SELLABLE_ACCESSIBLE, "Sellable, accessible"),
    (cbv.SELLABLE_NOT_ACCESSIBLE, "Sellable, not accessible"),
    (cbv.RETAIL_SOLD, "Sold"),
    (cbv.NON_SELLABLE_OTHER, "Not sellable"),
    (cbv.DAMAGED, "Damaged"),
    (cbv.EXPIRED, "Expired"),
    (cbv.RECALLED, "Recalled"),
    (cbv.DESTROYED, "Destroyed"),
    (cbv.INACTIVE, "Inactive"),
]


class StockPickingType(models.Model):
    _inherit = "stock.picking.type"

    openepcis_capture = fields.Boolean(
        string="Report as EPCIS events",
        default=True,
        help="Whether validating this kind of operation reports an event. The "
        "company switch has to be on as well; this decides which operations "
        "are worth an event once it is.",
    )
    openepcis_biz_step = fields.Selection(
        BIZ_STEPS,
        string="Business step",
        compute="_compute_openepcis_defaults",
        store=True,
        readonly=False,
        help="What this operation means in the GS1 Core Business Vocabulary — "
        "the word a trading partner reads without a mapping table.",
    )
    openepcis_disposition = fields.Selection(
        DISPOSITIONS,
        string="Disposition",
        compute="_compute_openepcis_defaults",
        store=True,
        readonly=False,
        help="The state the goods are left in afterwards. It persists until "
        "another event changes it, which is what makes it worth stating.",
    )

    @api.depends("code", "sequence_code")
    def _compute_openepcis_defaults(self):
        """Seed from the operation's own codes, and never overwrite a choice."""
        for picking_type in self:
            biz_step, disposition = picking_type._openepcis_default_mapping()
            picking_type.openepcis_biz_step = picking_type.openepcis_biz_step or biz_step
            picking_type.openepcis_disposition = (
                picking_type.openepcis_disposition or disposition
            )

    def _openepcis_default_mapping(self):
        """The (business step, disposition) this operation most likely means.

        Overridden where another module knows better — the point-of-sale bridge
        recognises a till, which no code on the operation type reveals.
        """
        self.ensure_one()
        if self.code == "internal":
            refined = DEFAULTS_BY_SEQUENCE.get((self.sequence_code or "").upper())
            if refined:
                return refined
        return DEFAULTS_BY_CODE.get(self.code, (cbv.STORING, cbv.IN_PROGRESS))
