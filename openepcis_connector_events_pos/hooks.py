# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""What to do about the tills that were already there.

This bridge is auto-installed, which means it usually arrives at a database
that has been selling for a while. Its override only decides the *seed* of an
operation type's meaning, and seeds are written once — so without a nudge at
install the tills already configured would keep reporting their sales as
shipments, and the bridge would look installed and do nothing.
"""


def reseed_tills(env):
    tills = env["pos.config"].sudo().search([]).picking_type_id
    if tills:
        tills._openepcis_reseed_for_till()
