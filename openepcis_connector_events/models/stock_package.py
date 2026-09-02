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
from datetime import datetime, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..utils import sscc
from ..vendored import (
    aggregation_event,
    cbv,
    document,
    idempotency_key,
    instance_uri,
    quantity_element,
    sscc_uri,
)
from .stock_move_line import PIECE

logger = logging.getLogger(__name__)


class StockPackage(models.Model):
    # Odoo 19 renamed the model: stock.quant.package became stock.package, and
    # gained nesting with it. The 18.0 branch inherits the old name.
    _inherit = "stock.package"

    openepcis_sscc = fields.Char(
        string="SSCC",
        copy=False,
        index=True,
        help="Serial Shipping Container Code of this unit. Minted from the "
        "company prefix when the package is created.",
    )

    # Odoo 19 declares table constraints this way. The 18.0 branch still uses
    # _sql_constraints, which 19 accepts, warns about once and then ignores — so
    # the old form here would leave the rule uncreated and nothing would say so.
    _openepcis_sscc_unique = models.Constraint(
        "unique (openepcis_sscc)",
        "This SSCC is already in use.",
    )

    @api.constrains("openepcis_sscc")
    def _check_openepcis_sscc(self):
        for package in self:
            if package.openepcis_sscc and not sscc.is_valid(package.openepcis_sscc):
                raise ValidationError(
                    _(
                        "'%s' is not a usable SSCC. It is 18 digits and ends in a check digit.",
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
        # A unit can be created already inside another, and that is as much a
        # nesting as writing the container afterwards. The write override never
        # sees it, so it is said from here — after the SSCCs exist, or the event
        # would have no name for the unit that just went in.
        inside = packages.filtered("parent_package_id")
        if inside:
            try:
                inside._openepcis_report_renesting({package: self.browse() for package in inside})
            except Exception:  # noqa: BLE001 — a report must never fail a creation
                logger.exception("OpenEPCIS: reporting a new unit's container failed")
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

    # ------------------------------------------------------------------
    # Units inside units
    # ------------------------------------------------------------------

    def write(self, vals):
        """Report a unit moving into or out of another unit.

        Odoo 19 lets logistic units nest, and GS1 has had the answer since
        before that: a pallet of cases is an aggregation whose children are
        SSCCs rather than trade items. It is the same standing statement as a
        packed pallet — scan the outer label and the repository answers what is
        under it — so it has to be reported at both ends or it goes stale.

        ``parent_package_id`` is the hook because every route to nesting passes
        through it: the put-in-pack flows write it, the field on the package
        form writes it, and ``unpack`` clears it on the children. One override
        therefore covers all of them, including the one this connector
        deliberately left unreported when the addons were first ported.

        ``package_dest_id`` is left alone on purpose. That field is where a unit
        is *going* to be put during an open transfer, and a plan is not a fact.
        """
        if "parent_package_id" not in vals:
            return super().write(vals)
        before = {package: package.parent_package_id for package in self}
        result = super().write(vals)
        try:
            self._openepcis_report_renesting(before)
        except Exception:  # noqa: BLE001 — a report must never fail a transfer
            logger.exception("OpenEPCIS: reporting a change of container failed")
        return result

    def _openepcis_report_renesting(self, before):
        """One event per container, not one per unit that moved.

        An aggregation is a statement about a container, so two cases put on
        the same pallet are one statement about that pallet. Grouping also
        keeps a re-nesting honest: a unit moved from one pallet to another
        leaves the first and joins the second, and both halves are said.
        """
        left = {}
        joined = {}
        for package, was in before.items():
            now_inside = package.parent_package_id
            if now_inside == was:
                continue
            if was:
                left[was] = left.get(was, self.browse()) | package
            if now_inside:
                joined[now_inside] = joined.get(now_inside, self.browse()) | package
        for parent, children in left.items():
            parent._openepcis_report_nesting(children, cbv.DELETE, cbv.UNPACKING)
        for parent, children in joined.items():
            parent._openepcis_report_nesting(children, cbv.ADD, cbv.PACKING)

    def _openepcis_report_nesting(self, children, action, biz_step):
        """Say which units are, or are no longer, inside this one."""
        self.ensure_one()
        company = self.company_id or self.env.company
        if not self.openepcis_sscc or not self.env["openepcis.client"]._epcis_configured(company):
            # Nothing was ever claimed about this container, so there is nothing
            # to add to and nothing to take apart.
            return
        child_epcs = [child._openepcis_uri() for child in children if child.openepcis_sscc]
        if not child_epcs:
            logger.info(
                "OpenEPCIS: units moved in or out of %s carry no SSCC — nothing reported",
                self.name,
            )
            return
        # The container's own location, because the statement is about it. A
        # child on its way somewhere else has already left.
        read_point = self.location_id._openepcis_read_point() if self.location_id else ""
        if not read_point:
            logger.info(
                "OpenEPCIS: %s changed contents, but no location carries a GLN — nothing reported",
                self.name,
            )
            return
        # Nesting is an act rather than a completion: unlike a transfer it has
        # no recorded date of its own, and the moment it is written is the
        # moment it becomes true. The same rule the Unpack button follows.
        moment = datetime.now(timezone.utc).replace(microsecond=0)
        event = aggregation_event(
            action=action,
            event_time=moment,
            parent_id=self._openepcis_uri(),
            biz_step=biz_step,
            disposition=cbv.IN_PROGRESS,
            child_epcs=child_epcs,
            child_quantities=[],
            read_point=read_point,
            biz_location=read_point,
        )
        self.env["openepcis.event"].queue(
            document([event]),
            _("%(name)s (%(step)s)", name=self.name, step=biz_step),
            company,
            idem_key=idempotency_key(
                self.env["ir.config_parameter"].sudo().get_param("database.uuid") or "odoo",
                biz_step,
                self.openepcis_sscc,
                *sorted(child_epcs),
                moment.isoformat(),
            ),
            source=self,
        )

    def unpack(self):
        """Taking the unit apart, from the package itself.

        Odoo empties a unit in two very different ways, and only one of them
        goes through a transfer. Picking goods off a pallet in a delivery
        leaves move lines and reaches the hook on ``stock.picking``; pressing
        Unpack here leaves none at all — it clears the package on the quants
        and is done. Reporting only the first would tell the repository about
        the rarer of the two and stay silent about the everyday one, and an
        aggregation nobody withdraws keeps answering with goods that have been
        on the shelf for weeks.

        Everything the event needs is read *before* the call, and that is more
        than it looks: after it, the quants no longer name the unit, so the
        contents are gone — and so is ``location_id``, which Odoo computes from
        those same quants. Reading the read point afterwards found an empty
        location and the report died on a singleton. Whatever the unit knows
        about itself, it knows it only until it is emptied.

        Never raises: emptying a pallet must not fail because a repository is
        unreachable, which is the same rule the transfer hook follows.

        This reports the goods directly inside, which is exactly what this call
        moves. The child units it also detaches are reported too, but not from
        here: clearing their container is a write on them, and the override on
        ``write`` says it. So an unpack with both produces two statements about
        the same container — one for the loose goods, one for the units — and
        each is true and complete on its own.
        """
        described = {package: package._openepcis_describe() for package in self}
        result = super().unpack()
        for package, (epcs, quantities, read_point) in described.items():
            try:
                package._openepcis_report_unpacked(epcs, quantities, read_point)
            except Exception:  # noqa: BLE001 — a report must never fail an unpack
                logger.exception("OpenEPCIS: reporting the unpacking of %s failed", package.name)
        return result

    def _openepcis_describe(self):
        """What this unit holds and where it stands — while it still knows.

        The contents read the way the transfer hook reads its move lines: a
        serial is an instance and belongs in the EPC list, a lot is a class and
        belongs in the quantity list. The read point comes from the location,
        which answers with its own GLN or the nearest one above it.
        """
        self.ensure_one()
        epcs = []
        merged = {}
        for quant in self.quant_ids:
            product = quant.product_id
            gtin = product._openepcis_key()
            if not gtin or not product.openepcis_publish:
                continue
            lot_name = quant.lot_id.name
            if product.tracking == "serial" and lot_name:
                epcs.append(instance_uri(gtin, serial=lot_name))
                continue
            epc_class = instance_uri(gtin, lot=lot_name) if lot_name else instance_uri(gtin)
            uom = quant.product_uom_id.openepcis_rec20_code or ""
            merged[(epc_class, uom)] = merged.get((epc_class, uom), 0) + quant.quantity
        quantities = [
            # The same spelling a transfer uses. A quantity with no unit is
            # read by EPCIS as a piece count, so the code for pieces is left
            # off rather than stated — and it has to be left off *here* too,
            # or the same goods read as one thing when they are packed and as
            # another when they are unpacked. The quantity element goes into
            # the event hash, so the two statements would not even be
            # comparable.
            quantity_element(epc_class, quantity, uom if uom and uom != PIECE else None)
            for (epc_class, uom), quantity in merged.items()
        ]
        # The same walk a transfer uses, asked of the location while the unit
        # still has one.
        read_point = self.location_id._openepcis_read_point() if self.location_id else ""
        return epcs, quantities, read_point

    def _openepcis_report_unpacked(self, epcs, quantities, read_point):
        """Say that this unit no longer holds what it held."""
        self.ensure_one()
        if not epcs and not quantities:
            return
        company = self.company_id or self.env.company
        if not self.openepcis_sscc or not self.env["openepcis.client"]._epcis_configured(company):
            return
        if not read_point:
            logger.info(
                "OpenEPCIS: %s was unpacked, but no location carries a GLN — nothing reported",
                self.name,
            )
            return
        moment = datetime.now(timezone.utc).replace(microsecond=0)
        event = aggregation_event(
            action=cbv.DELETE,
            event_time=moment,
            parent_id=self._openepcis_uri(),
            biz_step=cbv.UNPACKING,
            disposition=cbv.IN_PROGRESS,
            child_epcs=epcs,
            child_quantities=quantities,
            read_point=read_point,
            biz_location=read_point,
        )
        self.env["openepcis.event"].queue(
            document([event]),
            _("%s (unpacked)", self.name),
            company,
            idem_key=idempotency_key(
                self.env["ir.config_parameter"].sudo().get_param("database.uuid") or "odoo",
                "unpack",
                self.openepcis_sscc,
                moment.isoformat(),
            ),
            source=self,
        )

    def _openepcis_uri(self):
        """The canonical URI of this unit, or an empty string if it has none."""
        self.ensure_one()
        return sscc_uri(self.openepcis_sscc) if self.openepcis_sscc else ""
