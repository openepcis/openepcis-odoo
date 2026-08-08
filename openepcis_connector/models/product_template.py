# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Product fields the catalog needs and Odoo does not have.

Everything defined here exists because a downstream GS1 registry asks for it and
no standard Odoo field carries it. They live on the template because they
describe the article rather than the variant — two colours of the same shirt
share a brand, a net content and a country of origin.

Deliberately *not* reused:

``country_of_origin``
    Present in Odoo, but contributed by the Intrastat module, which is
    Enterprise. Depending on it would make this connector uninstallable on
    Community. Databases that do have it can point the mapping row at it
    instead — that is what a data-driven mapping is for.

a brand field
    Odoo has none in core. The popular one comes from OCA's ``product_brand``,
    which not everyone runs.
"""

from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    openepcis_brand_name = fields.Char(
        string="Brand",
        help="Brand as it appears to a consumer. Required by GS1 registries.",
    )
    openepcis_net_content = fields.Float(
        string="Net content",
        digits="Product Unit of Measure",
        help="How much product is in the package — 500 for a 500 ml bottle. "
        "Distinct from weight, and required by GS1 registries.",
    )
    openepcis_net_content_uom_id = fields.Many2one(
        "uom.uom",
        string="Net content unit",
        help="Unit the net content is expressed in.",
    )
    openepcis_country_of_origin_id = fields.Many2one(
        "res.country",
        string="Country of origin",
        help="Where the product was produced.",
    )
    openepcis_target_market_ids = fields.Many2many(
        "res.country",
        "openepcis_product_target_market_rel",
        "product_tmpl_id",
        "country_id",
        string="Target markets",
        help="Countries the product is sold in. GS1 registries require at least one.",
    )

    # Mirrors of the variant state, so a single-variant product — which is what
    # most Odoo databases hold — can be published from the template form
    # without the user ever meeting the variant. This is the same trick Odoo
    # plays with `barcode`.
    openepcis_publish = fields.Boolean(
        string="Publish to OpenEPCIS",
        compute="_compute_openepcis_publish",
        inverse="_inverse_openepcis_publish",
        store=False,
    )
    openepcis_state = fields.Selection(
        [
            ("not_synced", "Not published"),
            ("queued", "Waiting"),
            ("synced", "Published"),
            ("error", "Failed"),
            ("partial", "Partly published"),
        ],
        string="Publication",
        compute="_compute_openepcis_state",
    )
    openepcis_error = fields.Char(compute="_compute_openepcis_state")
    openepcis_digital_link = fields.Char(compute="_compute_openepcis_digital_link")
    openepcis_missing_terms = fields.Char(
        string="Still needed",
        compute="_compute_openepcis_missing_terms",
        help="Fields a downstream GS1 registry asks for and this product does "
        "not yet carry. Publishing is still allowed.",
    )

    @api.depends("product_variant_ids.openepcis_missing_terms")
    def _compute_openepcis_missing_terms(self):
        """The union across variants — a gap on any variant is a gap to close.

        Most of these fields live on the template anyway, so in practice the
        variants agree and the union is what one of them says.
        """
        for template in self:
            gaps = set()
            for variant in template.product_variant_ids:
                gaps.update(
                    term.strip()
                    for term in (variant.openepcis_missing_terms or "").split(",")
                    if term.strip()
                )
            template.openepcis_missing_terms = ", ".join(sorted(gaps))

    @api.depends("product_variant_ids.openepcis_publish")
    def _compute_openepcis_publish(self):
        for template in self:
            variants = template.product_variant_ids
            template.openepcis_publish = bool(variants) and all(
                v.openepcis_publish for v in variants
            )

    def _inverse_openepcis_publish(self):
        for template in self:
            template.product_variant_ids.openepcis_publish = template.openepcis_publish

    @api.depends("product_variant_ids.openepcis_state", "product_variant_ids.openepcis_error")
    def _compute_openepcis_state(self):
        """Summarise the variants, worst news first.

        A template is not itself published — its variants are, each with its own
        GTIN. Reporting the first variant's state would quietly mislead anyone
        with a product in three sizes.
        """
        for template in self:
            variants = template.product_variant_ids
            states = set(variants.mapped("openepcis_state"))
            failed = variants.filtered(lambda v: v.openepcis_state == "error")
            template.openepcis_error = failed[:1].openepcis_error or False
            if not states:
                template.openepcis_state = "not_synced"
            elif "error" in states:
                template.openepcis_state = "error"
            elif "queued" in states:
                template.openepcis_state = "queued"
            elif states == {"synced"}:
                template.openepcis_state = "synced"
            elif "synced" in states:
                template.openepcis_state = "partial"
            else:
                template.openepcis_state = "not_synced"

    @api.depends("product_variant_ids.openepcis_digital_link")
    def _compute_openepcis_digital_link(self):
        """Only meaningful when there is one variant, because there is one link per GTIN."""
        for template in self:
            variants = template.product_variant_ids
            template.openepcis_digital_link = (
                variants.openepcis_digital_link if len(variants) == 1 else False
            )

    def action_openepcis_publish(self):
        """Publish every variant of these templates.

        Each variant is its own trade item with its own GTIN, so publishing a
        template means publishing all of them.
        """
        variants = self.mapped("product_variant_ids")
        if not variants:
            return False
        return variants.action_openepcis_publish()

    def action_openepcis_open_digital_link(self):
        self.ensure_one()
        return self.product_variant_ids[:1].action_openepcis_open_digital_link()

    @api.model
    def _openepcis_default_net_content_uom(self):
        return self.env.ref("uom.product_uom_unit", raise_if_not_found=False)
