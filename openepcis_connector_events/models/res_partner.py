# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Whom we let move our paperwork.

Trust is not a global setting. That a long-standing supplier may confirm his own
despatch says nothing about whether any other market participant may. So the
permission sits on the partner, next to the GLN that identifies him in the
events, and it has to be given deliberately — the default is no.

It is only ever a *permission*, never an instruction: the operation type decides
what may happen at all, and this decides whom it may happen for. Both have to
agree, which is the point. One of them alone is a single point of trust.
"""

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    openepcis_inbound_trusted = fields.Boolean(
        string="May move our transfers",
        default=False,
        help="Whether events reported by this partner may advance a transfer of "
        "ours — a despatch he confirms, a receipt he books. Needs the "
        "operation type to allow it too. Without a GLN on this partner "
        "nothing can be attributed to him in the first place.",
    )
