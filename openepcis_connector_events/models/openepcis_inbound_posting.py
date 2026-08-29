# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""When a partner's event may move our paperwork — and when it may not.

The inbox proper only ever *shows* things, and that is still the default. This
file is the deliberate exception: the one case where a foreign event carries
enough weight to close a transfer of ours, namely when that transfer is already
open, already reserved, and already waiting for exactly this confirmation. The
partner is not telling us something new; he is telling us that the thing we
expected has happened.

Two settings have to agree before anything moves: the operation type says what
may happen at all, the partner says for whom. Neither alone is enough, because
either alone would be a single point of trust — one wrong GLN, or one operation
type set too eagerly, should not be able to post by itself.

Above both of them sits an invariant that no setting can lift:

    an incoming event may only advance a transfer that is already open and
    expected. It never creates a document, never creates stock, never reopens
    something closed, and never touches quantities.

That is what keeps the blast radius bounded. The worst a wrong event can do is
validate a transfer a day early — visible, attributable, reversible by the same
means as any other mistaken validation. Compare that to the alternative, where
a foreign identifier could conjure a movement into a valued stock: there is no
honest way back out of that, which is why it is not on the ladder at all.

And before any of it runs for real, it runs as a rehearsal: while the operation
type is set to observe, the whole decision is made and written down, and the
last step is skipped. Somebody reads a week of that log and then decides.
"""

import logging

from odoo import _, api, fields, models

logger = logging.getLogger(__name__)

#: Transfer states an event may advance. Exactly one: reserved and waiting.
#: ``confirmed`` is deliberately absent — a transfer that is not reserved has no
#: stock behind it, and validating it would create the movement out of nothing.
ADVANCEABLE = ("assigned",)

#: What a partner has to have said, per kind of transfer of ours, before his
#: event closes it.
#:
#: Only outgoing appears here, and that is the whole argument. When we deliver,
#: the event that completes the transfer happens at *his* dock: he receives,
#: and he is the only one who can witness that. When we *receive*, the event
#: that completes the transfer happens at ours — his "shipped" says the goods
#: left him, which is emphatically not that they arrived here. Closing an
#: incoming transfer on a despatch note would book stock we cannot see, for a
#: lorry that may still be on the road. So an incoming transfer is never
#: advanced from outside, no matter how the operation type is set: there is no
#: statement a partner can make that means "it is in your warehouse".
#:
#: Internal transfers are absent for the same reason turned inside out: nobody
#: outside the company witnesses them at all.
ATTESTING_STEPS = {
    "outgoing": ("receiving", "accepting", "arriving"),
}


class OpenepcisInboundEvent(models.Model):
    _inherit = "openepcis.inbound.event"

    observed_only = fields.Boolean(
        readonly=True,
        help="Set when the connector decided it would have posted this event and "
        "deliberately did not, because the operation type is still observing.",
    )
    posting_note = fields.Char(
        readonly=True,
        help="Why this event did or did not move a transfer. Kept on the row so "
        "the observation log can be read without reconstructing the reasoning.",
    )

    # ------------------------------------------------------------------
    # The ladder
    # ------------------------------------------------------------------

    def _consider_posting(self, record):
        """Decide what this event may do, and do it. Never raises.

        Called once a row has been matched to something in this database. The
        outcome is always written down, including — especially — the outcome
        "nothing", because an inbox that silently declines is indistinguishable
        from one that is broken.
        """
        self.ensure_one()
        picking = self._find_transfer(record)
        if not picking:
            return False
        policy = picking.picking_type_id.openepcis_inbound_policy
        if policy in ("ignore", "show"):
            return False

        refusal = self._refuse_posting(picking)
        if refusal:
            self.sudo().write({"posting_note": refusal})
            logger.info("OpenEPCIS inbox: %s not posted — %s", self.event_uuid, refusal)
            return False

        if policy == "propose" or picking.picking_type_id.openepcis_inbound_observe:
            return self._propose(picking, observing=policy == "post")
        return self._post(picking)

    def _refuse_posting(self, picking):
        """The invariant, stated as the one place that can say no.

        Returns a reason, or nothing if the transfer may be advanced. Every
        condition here is deliberately outside the reach of configuration.
        """
        self.ensure_one()
        if picking.state not in ADVANCEABLE:
            return _("transfer is %s, not reserved and waiting") % picking.state
        if picking.company_id != self.company_id:
            return _("transfer belongs to another company")
        expected = ATTESTING_STEPS.get(picking.picking_type_id.code or "")
        if not expected:
            return _("a %s transfer is not something a partner can witness") % (
                picking.picking_type_id.code or "?"
            )
        if self._step_of() not in expected:
            return _("%s does not attest that the transfer completed") % (self.biz_step or "?")
        partner = picking.partner_id
        if not partner:
            return _("transfer names no partner to attribute the event to")
        if not partner.sudo().openepcis_inbound_trusted:
            return _("%s is not allowed to move our transfers") % partner.display_name
        if not self._reporter_is(partner):
            return _("event was reported by %s, not by %s") % (
                self.party_gln or _("nobody"),
                partner.display_name,
            )
        return None

    def _propose(self, picking, observing=False):
        """Ask a human, or — while observing — merely write down that we would."""
        self.ensure_one()
        if observing:
            note = _("Would have posted this transfer. Observing, so nothing was posted.")
            self.sudo().write({"state": "proposed", "observed_only": True, "posting_note": note})
        else:
            note = _("Proposed for confirmation.")
            self.sudo().write({"state": "proposed", "posting_note": note})
        picking.sudo().message_post(
            body=_(
                "EPCIS: %(party)s reported %(step)s for %(what)s. %(note)s",
                party=self.party_gln or _("a partner"),
                step=self.biz_step or _("an event"),
                what=self.epc_ref or _("this transfer"),
                note=note,
            )
        )
        return True

    def _post(self, picking):
        """Validate the transfer — the only write this connector ever does.

        ``button_validate`` can answer with a wizard instead of doing the work:
        a backorder to decide, a quantity to confirm. That answer is a request
        for a human judgement, and a cron is not one. So an action coming back
        is treated as a refusal, not as something to click through in code.
        """
        self.ensure_one()
        try:
            result = picking.sudo().with_context(
                skip_backorder=True,
                picking_ids_not_to_backorder=picking.ids,
            ).button_validate()
        except Exception as error:  # a partner's event must never break the run
            note = _("Refused by Odoo: %s") % error
            self.sudo().write({"posting_note": note})
            logger.warning("OpenEPCIS inbox: %s could not post %s (%s)", self.event_uuid, picking.name, error)
            return False
        if isinstance(result, dict) and result.get("type"):
            note = _("Odoo asked a question a scheduled run cannot answer; left for a person.")
            self.sudo().write({"state": "proposed", "posting_note": note})
            picking.sudo().message_post(body=_("EPCIS: %s") % note)
            return False
        note = _("Posted from an event reported by %s.") % (self.party_gln or _("a partner"))
        self.sudo().write({"state": "booked", "posting_note": note})
        picking.sudo().message_post(body=_("EPCIS: %s") % note)
        logger.info("OpenEPCIS inbox: %s posted %s", self.event_uuid, picking.name)
        return True

    # ------------------------------------------------------------------
    # Finding the transfer, and whether it is ours to advance
    # ------------------------------------------------------------------

    def _find_transfer(self, record):
        """The open transfer this event is about, if there is exactly one.

        Exactly one, deliberately. Two open transfers carrying the same lot is
        an ambiguity, and guessing which one a partner meant is precisely the
        kind of inference that has no business posting to a ledger.
        """
        self.ensure_one()
        moves = self.env["stock.move.line"].sudo()
        if record._name == "stock.lot":
            domain = [("lot_id", "=", record.id)]
        elif record._name == "stock.quant.package":
            domain = ["|", ("package_id", "=", record.id), ("result_package_id", "=", record.id)]
        else:
            return None
        lines = moves.search(
            domain
            + [
                ("picking_id.state", "in", ADVANCEABLE),
                ("picking_id.company_id", "=", self.company_id.id),
            ]
        )
        pickings = lines.mapped("picking_id")
        return pickings if len(pickings) == 1 else None

    def _step_of(self):
        """The bare business step, however it was written.

        CBV values travel as a URN, as a URL, or bare, depending on who built
        the event. All three end in the same token, so that is what is compared.
        """
        self.ensure_one()
        return (self.biz_step or "").rsplit(":", 1)[-1].rsplit("/", 1)[-1]

    def _reporter_is(self, partner):
        """Whether the event was reported by this very partner.

        Without a GLN on either side nothing can be attributed, and an event
        that cannot be attributed must not post. Absence is not agreement.
        """
        self.ensure_one()
        theirs = (partner.sudo().openepcis_gln or "").strip()
        reported = (self.party_gln or "").strip().rsplit("/", 1)[-1]
        return bool(theirs) and bool(reported) and theirs == reported
