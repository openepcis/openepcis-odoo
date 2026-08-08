# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Which Odoo field feeds which GS1 term — as records, not as code.

Every Odoo database names things differently: one has a brand module, the next
keeps the brand in a product attribute, a third in ``x_studio_marke``. Hard-coded
mapping would make the connector wrong for almost everyone and unfixable without
a developer. So the mapping is data: shipped with sensible defaults, editable by
an administrator, extensible by another addon adding rows.

The target side is a dotted path into the catalog's product document, because
the terms that matter are nested::

    brand.brandName                                 -> {"brand": {"brandName": {...}}}
    countryOfOrigin.countryCode                     -> {"countryOfOrigin": {"countryCode": "DE"}}
    targetMarket[].targetMarketCountries.countryCode-> {"targetMarket": [{...}]}

A ``[]`` segment means "a single-element list", which is how the catalog models
target markets and referenced files.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..utils.gs1 import language_tag

_logger = logging.getLogger(__name__)


class OpenepcisFieldMapping(models.Model):
    _name = "openepcis.field.mapping"
    _description = "OpenEPCIS field mapping"
    _order = "model_name, sequence, id"

    name = fields.Char(compute="_compute_name", store=False)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    model_name = fields.Selection(
        selection="_selection_model_name",
        string="Applies to",
        required=True,
        help="The Odoo model this row reads from.",
    )
    odoo_field = fields.Char(
        string="Odoo field",
        required=True,
        help="Field name, or a dotted path across relations: "
        "categ_id.openepcis_gpc_code reads the category's GPC code.",
    )
    gs1_path = fields.Char(
        string="GS1 term",
        required=True,
        help="Dotted path into the catalog document, e.g. brand.brandName. "
        "A [] segment builds a single-element list.",
    )
    value_type = fields.Selection(
        [
            ("text", "Text"),
            ("localized", "Text, all languages"),
            ("quantity", "Measurement (value + unit)"),
            ("boolean", "Yes/no"),
            ("boolean_text", "Yes/no, as text"),
            ("integer", "Whole number"),
            ("float", "Decimal number"),
            ("date", "Date"),
        ],
        required=True,
        default="text",
        help="How the Odoo value is shaped for the catalog.",
    )
    unit_field = fields.Char(
        string="Unit from field",
        help="Measurements only: dotted path to a unit of measure whose "
        "UN/CEFACT code is used. Falls back to the fixed unit below.",
    )
    unit_code = fields.Char(
        string="Fixed unit",
        help="Measurements only: UN/CEFACT Recommendation 20 code, e.g. KGM, GRM, MLT.",
    )
    note = fields.Char(help="Why this row exists — shown to whoever edits the mapping.")

    @api.model
    def _selection_model_name(self):
        """Models that can be published. Extended by adding a value here."""
        return [
            ("product.product", "Product"),
            ("res.partner", "Contact"),
        ]

    @api.depends("odoo_field", "gs1_path")
    def _compute_name(self):
        for mapping in self:
            mapping.name = "%s → %s" % (mapping.odoo_field or "?", mapping.gs1_path or "?")

    @api.constrains("gs1_path")
    def _check_gs1_path(self):
        for mapping in self:
            path = (mapping.gs1_path or "").strip()
            if not path or path.startswith(".") or path.endswith(".") or ".." in path:
                raise ValidationError(_("'%s' is not a usable GS1 term path.", mapping.gs1_path))

    @api.constrains("value_type", "unit_field", "unit_code")
    def _check_unit(self):
        for mapping in self:
            if mapping.value_type == "quantity" and not (mapping.unit_field or mapping.unit_code):
                raise ValidationError(
                    _(
                        "The measurement '%s' needs a unit: either a field to read "
                        "it from or a fixed UN/CEFACT code.",
                        mapping.name,
                    )
                )

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------
    #
    # The mixin caches which Odoo fields are worth watching, keyed by model. Any
    # change here can change that answer, so the cache goes when a row does.

    def _clear_watched_fields_cache(self):
        self.env.registry.clear_cache()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self._clear_watched_fields_cache()
        return records

    def write(self, vals):
        result = super().write(vals)
        self._clear_watched_fields_cache()
        return result

    def unlink(self):
        result = super().unlink()
        self._clear_watched_fields_cache()
        return result

    # ------------------------------------------------------------------
    # Building a payload
    # ------------------------------------------------------------------

    @api.model
    def build_payload(self, record):
        """The catalog document for one record, from the active mapping rows.

        Rows that read an empty field are skipped rather than sent as ``null``:
        the catalog treats ``PUT`` as a merge where ``null`` means "leave alone",
        so an empty value carries no meaning and only makes the payload noisy.
        """
        record.ensure_one()
        payload = {}
        mappings = self.sudo().search([("model_name", "=", record._name)])
        for mapping in mappings:
            try:
                value = mapping._extract(record)
            except Exception:
                # One misconfigured row must not cost the whole record. The
                # mapping is user-editable, so a bad path is a configuration
                # mistake to report, not a crash to propagate.
                _logger.exception(
                    "OpenEPCIS mapping %s failed on %s#%s", mapping.name, record._name, record.id
                )
                continue
            if value is None:
                continue
            mapping._place(payload, value)
        return payload

    def _extract(self, record):
        """The value of this row's Odoo field, shaped for the catalog."""
        self.ensure_one()
        if self.value_type == "localized":
            return self._extract_localized(record)

        raw = self._traverse(record, self.odoo_field)
        if raw is None or raw is False or raw == "":
            return None

        if self.value_type == "text":
            return self._as_text(raw)
        if self.value_type == "quantity":
            return self._as_quantity(record, raw)
        if self.value_type == "boolean":
            return bool(raw)
        if self.value_type == "boolean_text":
            # Several catalog fields are declared as strings holding "true"/"false"
            # rather than as booleans; sending a real boolean fails validation.
            return "true" if raw else "false"
        if self.value_type == "integer":
            return int(raw)
        if self.value_type == "float":
            return float(raw)
        if self.value_type == "date":
            return fields.Date.to_string(raw) if raw else None
        return self._as_text(raw)

    def _has_value(self, record):
        """Whether this row would contribute anything, without building the value.

        Used by the readiness check, which runs per record in a list view. The
        cheap answer matters: :meth:`_extract` reads a localised field once per
        installed language, and doing that to find out whether a box is filled
        in would be extravagant.
        """
        self.ensure_one()
        raw = self._traverse(record, self.odoo_field)
        if raw is None or raw is False or raw == "":
            return False
        if self.value_type == "quantity":
            # A unit that carries no UN/CEFACT code cannot be published, so the
            # readiness check must not call this satisfied — otherwise the form
            # shows a filled-in measurement while the document goes out without
            # it, and nothing anywhere says so.
            return bool(float(raw)) and bool(self._unit_code(record))
        return True

    def _extract_localized(self, record):
        """``{"de": "...", "en": "..."}`` over the languages actually installed.

        Reads the record once per language, which is the only way Odoo exposes
        translations of a stored field. Batches stay small for this reason.
        """
        values = {}
        for lang in self.env["res.lang"].sudo().search([]):
            tag = language_tag(lang.code)
            if not tag or tag in values:
                continue
            raw = self._traverse(record.with_context(lang=lang.code), self.odoo_field)
            text = self._as_text(raw)
            if text:
                values[tag] = text
        return values or None

    def _unit_code(self, record):
        """The UN/CEFACT code this row would send, or an empty string.

        Empty means the unit has no code — either because none is configured or
        because the chosen unit of measure has no ``openepcis_rec20_code``. Odoo
        does not ship a code for every unit it ships, and inventing one would put
        a wrong unit on a published measurement.
        """
        self.ensure_one()
        unit = self.unit_code or ""
        if self.unit_field:
            uom = self._traverse(record, self.unit_field)
            if uom is not None and uom is not False and getattr(uom, "_name", None) == "uom.uom":
                unit = uom.openepcis_rec20_code or unit
        return unit

    def _as_quantity(self, record, raw):
        value = float(raw)
        if not value:
            # A measurement of zero is Odoo's "not filled in" for weight and
            # volume, and sending it would satisfy a requirement falsely.
            return None
        unit = self._unit_code(record)
        if not unit:
            # A quantity without a unit is not a measurement, so it is dropped
            # rather than sent half-formed. _has_value agrees with this, which is
            # what stops the omission from being silent: the readiness line
            # reports the term as still needed instead of the value vanishing
            # between a filled-in form and the published document.
            return None
        return {"value": value, "unitCode": unit}

    @api.model
    def _as_text(self, raw):
        if raw is None or raw is False:
            return None
        if hasattr(raw, "_name"):  # a recordset: use its display name
            return raw.display_name or None
        text = str(raw).strip()
        return text or None

    @api.model
    def _traverse(self, record, path):
        """Follow a dotted field path, stopping at the first empty relation."""
        value = record
        for part in (path or "").split("."):
            part = part.strip()
            if not part or value is None or value is False:
                return None
            if hasattr(value, "_name"):
                if not value:  # empty recordset
                    return None
                value = value[:1]  # a to-many yields its first record
                if part not in value._fields:
                    raise KeyError("%s has no field %s" % (value._name, part))
            value = value[part] if hasattr(value, "_name") else getattr(value, part)
        return value

    def _place(self, payload, value):
        """Write ``value`` into ``payload`` at this row's GS1 path."""
        self.ensure_one()
        parts = [p.strip() for p in self.gs1_path.split(".")]
        node = payload
        for part in parts[:-1]:
            node = self._descend(node, part)
        last = parts[-1]
        if last.endswith("[]"):
            node.setdefault(last[:-2], [])
            node[last[:-2]] = [value]
        else:
            node[last] = value

    @api.model
    def _descend(self, node, part):
        """One step down the path, creating the container the segment implies."""
        if part.endswith("[]"):
            key = part[:-2]
            existing = node.get(key)
            if not isinstance(existing, list) or not existing:
                node[key] = [{}]
            return node[key][0]
        if not isinstance(node.get(part), dict):
            node[part] = {}
        return node[part]
