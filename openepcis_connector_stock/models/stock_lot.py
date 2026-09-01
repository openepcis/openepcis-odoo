# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Lots and serial numbers as per-instance catalog documents.

A GTIN names the *model* of a thing; a lot or serial number names the batch or
the single unit in front of you. GS1 keeps that distinction in the Digital Link
path — ``/01/<gtin>`` is the model, ``/01/<gtin>/10/<lot>`` the batch (LGTIN),
``/01/<gtin>/21/<serial>`` the single unit (SGTIN) — and the catalog stores a
distinct document at each level. This model publishes Odoo's ``stock.lot`` to
those instance paths.

The key is deliberately the **product's** GTIN. A lot has no GS1 key of its
own; it *qualifies* one. Which qualifier depends on how the product is tracked:
by unique serial number, the lot name is a serial (AI 21); by lots — or not
tracked at all, where a lot record only exists because a person created one on
purpose — it is a batch number (AI 10).

One ordering rule shapes everything here: **the product goes first.** An
instance document hangs off the product's GTIN, and an instance under a GTIN
the catalog does not hold points at a model nobody can resolve. A lot whose
product is not published yet is therefore not an error — it *waits*, says so
on its form, and follows on its own the moment the product lands. Holding the
lot's data hostage over the product's state would break the same rule the rest
of the connector lives by.
"""

from odoo import _, api, fields, models
from odoo.addons.openepcis_connector.utils import gs1

#: The catalog term that mirrors each path qualifier, so the document itself
#: says what its URI says. The resolver also stamps these from the URL, but
#: carrying them in the payload keeps the document self-describing — and the
#: resolver's linkset auto-population derives the per-instance anchor from
#: exactly these fields, which is what makes ``?linkType=masterData`` resolve
#: at instance level instead of redirecting up to the GTIN.
QUALIFIER_TERMS = {"10": "hasBatchLotNumber", "21": "hasSerialNumber"}


class StockLot(models.Model):
    _name = "stock.lot"
    _inherit = ["stock.lot", "openepcis.sync.mixin"]

    openepcis_product_notice = fields.Char(
        string="Waiting on the product",
        compute="_compute_openepcis_product_notice",
        help="Why this lot is not being published yet. This is a wait, not a "
        "failure — the lot follows automatically once its product is in the "
        "catalog.",
    )

    # ------------------------------------------------------------------
    # Mixin hooks
    # ------------------------------------------------------------------

    def _openepcis_key(self):
        self.ensure_one()
        return gs1.clean(self.product_id.barcode)

    def _openepcis_key_type(self):
        return "GTIN"

    def _openepcis_kind(self):
        return "PRODUCT"

    def _openepcis_anchor_ai(self):
        return gs1.ANCHOR_AI["GTIN"]

    def _openepcis_endpoint(self):
        return "/products"

    def _openepcis_key_term(self):
        return "gtin"

    def _openepcis_qualifiers(self):
        """AI 21 for serial-tracked products, AI 10 for everything else.

        Tracking is a property of the product, not of the lot: Odoo uses one
        model for both and the ``tracking`` field says which one a record is.
        ``none`` falls back to the batch qualifier — a lot record on an
        untracked product exists because someone made one deliberately, and a
        batch is the weaker, safer claim.
        """
        self.ensure_one()
        name = (self.name or "").strip()
        if not name:
            return {}
        ai = "21" if self.product_id.tracking == "serial" else "10"
        return {ai: name}

    def _openepcis_payload(self):
        payload = super()._openepcis_payload()
        for ai, value in self._openepcis_qualifiers().items():
            payload[QUALIFIER_TERMS[ai]] = value
        return payload

    @api.model
    def _openepcis_watched_fields(self):
        """The lot number is watched on top of the mapped fields.

        For every other published model the key and the mapping rows cover
        what matters. Here the name is neither — it is the *path qualifier* —
        and renaming a lot must re-publish it so the document appears under
        the name people scan. The set from super() is ormcached; the union
        builds a new set rather than mutating the cached one.
        """
        return super()._openepcis_watched_fields() | {"name"}

    def _openepcis_missing_terms(self):
        """Registry requirements do not apply at instance level.

        The terms a GS1 registry insists on (brand, GPC, net content…) gate the
        *class-level* product, which carries them. Repeating the demand on
        every batch would put a permanent warning on records that cannot and
        need not satisfy it.
        """
        self.ensure_one()
        return []

    # ------------------------------------------------------------------
    # The product gate
    # ------------------------------------------------------------------

    def _openepcis_product_blocker(self):
        """Why the product's state stops this lot from publishing, or ``""``."""
        self.ensure_one()
        product = self.product_id
        if gs1.problem_with(product.barcode, "GTIN"):
            return _(
                "The product %s has no valid GTIN — an instance document hangs "
                "off the product's barcode.",
                product.display_name,
            )
        if product.openepcis_state != "synced":
            return _(
                "The product %s is not in the catalog yet. Publish it first — "
                "this record follows on its own.",
                product.display_name,
            )
        return ""

    @api.depends("openepcis_publish", "product_id.openepcis_state", "product_id.barcode")
    def _compute_openepcis_product_notice(self):
        for lot in self:
            lot.openepcis_product_notice = (
                lot._openepcis_product_blocker() if lot.openepcis_publish else ""
            )

    def _openepcis_mark_queued(self):
        """Queue only lots whose product is already in the catalog.

        The rest stay visibly un-queued with the reason on the form — waiting,
        not failed, because "the product is not published yet" is an ordering
        fact, not an error. They are re-queued from
        :meth:`product.product._openepcis_record_success` the moment the
        product lands.
        """
        ready = self.filtered(lambda lot: not lot._openepcis_product_blocker())
        return super(StockLot, ready)._openepcis_mark_queued()

    def _openepcis_check_ready(self):
        """The GS1 check on the product's GTIN, plus the ordering gate.

        Reached only by an explicit *Publish now* on a waiting lot (the queue
        never holds one), where a named blocker in the answer is worth more
        than a silent skip.
        """
        blocker = super()._openepcis_check_ready()
        if blocker:
            return blocker
        if not (self.name or "").strip():
            return _("The lot/serial number is empty.")
        return self._openepcis_product_blocker()

    @api.model
    def _openepcis_cron_sync(self):
        """Scheduled action entry point, kept here so the cron names a real model."""
        return super()._openepcis_cron_sync()
