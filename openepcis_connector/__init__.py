# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
from . import models
from . import wizards


def post_init_hook(env):
    """Fill in the UN/CEFACT codes for the units Odoo ships.

    Done in code rather than in XML data because Odoo has reorganised its unit
    records between releases: a missing record must be skipped, not turn the
    install into a traceback.
    """
    env["uom.uom"]._openepcis_fill_rec20_codes()
