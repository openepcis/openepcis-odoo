# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""GS1's product classification, attached where it costs the least effort.

``gpcCategoryCode`` is an eight-digit GPC brick code and is required by GS1
registries. There are several thousand bricks, so asking for one per product
would be cruel. Odoo already groups products into categories, and a category
maps onto a brick almost exactly — set it once, and every product in the
category inherits it.
"""

from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    openepcis_gpc_code = fields.Char(
        string="GPC brick",
        size=8,
        help="GS1 Global Product Classification brick code. Products in this "
        "category are published with it unless they override it.",
    )
    openepcis_gpc_title = fields.Char(
        string="GPC brick name",
        readonly=True,
        help="Name of the chosen brick, filled in when it is looked up.",
    )
