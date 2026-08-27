# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Where the events go, and whether they go at all.

The EPCIS repository is deliberately a second address rather than a path under
the resolver. They are two services with two jobs: the resolver answers *what
is this identifier*, the repository *what happened to it*. A deployment may
well run them on one host, but a connector that assumes so cannot be pointed at
one that does not.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..utils import sscc


class ResCompany(models.Model):
    _inherit = "res.company"

    openepcis_events_enabled = fields.Boolean(
        string="Report visibility events",
        help="Send validated transfers to the EPCIS repository as events. Off "
        "until the repository address is deposited and an operation type is armed.",
    )
    openepcis_epcis_url = fields.Char(
        string="EPCIS repository",
        help="Origin of the EPCIS 2.0 repository, e.g. https://api.epcis.cloud. "
        "A different service from the resolver, and usually a different host.",
    )
    openepcis_gcp = fields.Char(
        string="GS1 company prefix",
        help="Used to mint SSCCs for logistic units. Unlike a GTIN, an SSCC is "
        "not drawn from a registry: a company allocates its own from its prefix, "
        "and only has to make sure it never repeats one.",
    )

    @api.constrains("openepcis_gcp")
    def _check_openepcis_gcp(self):
        for company in self:
            if not company.openepcis_gcp:
                continue
            problem = sscc.problem_with_prefix(company.openepcis_gcp)
            if problem:
                raise ValidationError(
                    _(
                        "'%(prefix)s' cannot be a GS1 company prefix. %(why)s",
                        prefix=company.openepcis_gcp,
                        why=problem,
                    )
                )
