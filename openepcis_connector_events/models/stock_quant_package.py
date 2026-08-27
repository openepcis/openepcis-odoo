# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Logistic units, and the one GS1 key a company issues by itself.

A pallet is not an article. It exists for a week, carries a mixture of things,
and is identified by an SSCC — which, unlike a GTIN, is not drawn from a
registry: a company allocates its own from its prefix. So this mints them
locally rather than asking the platform, and the sequence is what keeps it from
issuing the same one twice.

An SSCC is what makes the aggregation event possible, and the aggregation event
is what lets a scan of a pallet label say what is underneath it without the
label having to carry a list.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..utils import sscc
from ..vendored import sscc_uri

logger = logging.getLogger(__name__)


class StockQuantPackage(models.Model):
    _inherit = "stock.quant.package"

    openepcis_sscc = fields.Char(
        string="SSCC",
        copy=False,
        index=True,
        help="Serial Shipping Container Code of this unit. Minted from the "
        "company prefix when the package is created.",
    )

    _sql_constraints = [
        ("openepcis_sscc_unique", "unique(openepcis_sscc)", "This SSCC is already in use."),
    ]

    @api.constrains("openepcis_sscc")
    def _check_openepcis_sscc(self):
        for package in self:
            if package.openepcis_sscc and not sscc.is_valid(package.openepcis_sscc):
                raise ValidationError(
                    _(
                        "'%s' is not a usable SSCC. It is 18 digits and ends in a "
                        "check digit.",
                        package.openepcis_sscc,
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        """Give a new unit its number, if this company can issue one.

        Minted on creation rather than on the first event: a package is
        identified from the moment it exists, and a number handed out later
        would leave the earlier events unable to name it.

        A company without a prefix simply gets packages without SSCCs. That is
        a configuration gap, not an error to raise in the middle of somebody
        packing a pallet.
        """
        packages = super().create(vals_list)
        for package in packages:
            if not package.openepcis_sscc:
                package._openepcis_mint_sscc(quiet=True)
        return packages

    def action_openepcis_mint_sscc(self):
        """Give this unit an SSCC now — for packages that predate the prefix."""
        for package in self:
            if package.openepcis_sscc:
                raise UserError(
                    _("%s already carries an SSCC.", package.display_name),
                )
            package._openepcis_mint_sscc(quiet=False)
        return True

    def _openepcis_mint_sscc(self, quiet=True):
        self.ensure_one()
        prefix = (self.company_id or self.env.company).sudo().openepcis_gcp
        if not prefix:
            if quiet:
                return ""
            raise UserError(
                _(
                    "No GS1 company prefix is deposited for %(company)s, so no SSCC "
                    "can be built.\nSettings > General Settings > OpenEPCIS.",
                    company=(self.company_id or self.env.company).display_name,
                )
            )
        reference = self.env["ir.sequence"].sudo().next_by_code("openepcis.sscc") or "0"
        try:
            number = sscc.build(prefix, reference)
        except ValueError as error:
            # The prefix and the sequence no longer fit into 18 digits. Silence
            # here would mean two pallets sharing a number, which is the one
            # thing an SSCC must never do.
            if quiet:
                logger.warning("SSCC for %s could not be built: %s", self.display_name, error)
                return ""
            raise UserError(str(error)) from error
        self.openepcis_sscc = number
        return number

    def _openepcis_uri(self):
        """The canonical URI of this unit, or an empty string if it has none."""
        self.ensure_one()
        return sscc_uri(self.openepcis_sscc) if self.openepcis_sscc else ""
