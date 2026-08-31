# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The inbox: what comes back in, and what it is allowed to do here.

The rule these tests exist to hold is the one that is easiest to erode later:
out of the box, a partner's event is shown and nothing else. Posting exists,
but it has to be switched on deliberately in two places — see
``test_inbound_posting.py``, which is where that ladder and the invariant
underneath it are held. Everything here runs on the default settings, so a
change that made posting happen by itself would break these tests first.
"""

from .common import TEST_PARTNER_GLN, EventCase

OUR_HASH = (
    "ni:///sha-256;f895a478db42352042ceb07ac96a607765697ca0a8e6c3fe76cbdefc537d185a?ver=CBV2.0"
)


def event(uuid, biz_step="receiving", epc=None, recorded="2026-08-27T22:13:29.000Z", party=None):
    body = {
        "type": "ObjectEvent",
        "eventID": uuid,
        "eventTime": "2026-08-27T22:13:00.000Z",
        "recordTime": recorded,
        "action": "OBSERVE",
        "bizStep": biz_step,
        "disposition": "in_progress",
        "epcList": [epc] if epc else [],
    }
    if party:
        body["sourceList"] = [{"type": "https://ref.gs1.org/cbv/SDT-owning_party", "source": party}]
    return body


class TestInbox(EventCase):
    def _poll(self):
        self.env["openepcis.inbound.event"]._cron_poll()

    def _rows(self):
        return self.env["openepcis.inbound.event"].search([])

    # ------------------------------------------------------------------

    def test_an_event_is_received_once_however_often_it_is_read(self):
        self.query.pages = [[event("urn:uuid:1")], [event("urn:uuid:1")]]
        self._poll()
        self._poll()
        self.assertEqual(len(self._rows()), 1, "the event id is the idempotency")

    def test_the_watermark_walks_forward_and_is_read_back_a_little(self):
        self.query.pages = [[event("urn:uuid:1", recorded="2026-08-27T22:13:29.000Z")]]
        self._poll()
        self.assertEqual(self.company.openepcis_events_since, "2026-08-27T22:13:29.000Z")
        self.query.pages = [[]]
        self._poll()
        # 22:13:29 less the five-minute overlap.
        self.assertTrue(self.query.asked[-1].startswith("2026-08-27T22:08:29"))

    def test_a_repository_that_cannot_be_read_leaves_the_watermark_alone(self):
        from odoo.addons.openepcis_connector.vendor.benelog_client.core.errors import (
            BenelogError,
        )

        self.company.openepcis_events_since = "2026-08-27T22:13:29.000Z"
        self.query.error_to_raise = BenelogError("no", status=503)
        self._poll()
        self.assertEqual(
            self.company.openepcis_events_since,
            "2026-08-27T22:13:29.000Z",
            "an unreadable repository is not an empty one",
        )

    def test_our_own_event_coming_back_is_recognised_and_not_retold(self):
        transfer = (
            self.env["openepcis.event"]
            .sudo()
            .create(
                {
                    "name": "ours",
                    "company_id": self.company.id,
                    "idem_key": "urn:uuid:ours",
                    # What we expect the repository to call it. We do not send
                    # this — the document leaves without an eventID — we only
                    # recognise it coming back.
                    "event_hash": OUR_HASH,
                    "event_time": "2026-08-27 22:13:00",
                    "payload": "{}",
                }
            )
        )
        self.assertTrue(transfer)
        self.query.pages = [[event(OUR_HASH)]]
        self._poll()
        self.assertEqual(self._rows().state, "ignored")

    def test_a_fourteen_digit_gtin_finds_a_product_labelled_with_thirteen(self):
        """The spelling a real sender uses has to resolve.

        A Digital Link writes AI 01 with fourteen digits; the barcode on the
        product is the thirteen-digit EAN. Matched exactly, the two never meet
        and the row lands as "unknown identifier" — including for events this
        database sent itself.
        """
        product = self._published_product(tracking="lot")
        lot = self._lot("LOT-952-2", product)
        self.assertEqual(len(product.barcode), 13)

        for spelling in ("0" + product.barcode, product.barcode):
            with self.subTest(spelling=spelling):
                self.env["openepcis.inbound.event"].search([]).unlink()
                self.query.pages = [
                    [
                        event(
                            "urn:uuid:%s" % spelling,
                            epc="https://id.gs1.org/01/%s/10/%s" % (spelling, lot.name),
                            party=TEST_PARTNER_GLN,
                        )
                    ]
                ]
                self._poll()
                row = self._rows()
                self.assertEqual(row.res_model, "stock.lot")
                self.assertEqual(row.res_id, lot.id)

    def test_an_indicator_digit_is_not_padding_and_resolves_to_nothing(self):
        """A leading one names a logistic unit of the item, not the item.

        Stripping it would resolve an event to the wrong product, which is worse
        than resolving it to none.
        """
        product = self._published_product(tracking="lot")
        self._lot("LOT-952-3", product)
        self.query.pages = [
            [
                event(
                    "urn:uuid:indicator",
                    epc="https://id.gs1.org/01/1%s/10/LOT-952-3" % product.barcode,
                    party=TEST_PARTNER_GLN,
                )
            ]
        ]

        self._poll()

        self.assertEqual(self._rows().state, "unmatched")

    def test_an_identifier_this_database_does_not_know_stays_visible(self):
        self.query.pages = [[event("urn:uuid:2", epc="https://id.gs1.org/01/09521234999999/21/9")]]
        self._poll()
        row = self._rows()
        self.assertEqual(row.state, "unmatched")
        self.assertTrue(row.epc_ref, "the raw identifier is kept, not discarded")

    def test_an_event_about_one_of_our_lots_finds_it_and_says_so_there(self):
        product = self._published_product(tracking="lot")
        lot = self._lot("LOT-952-1", product)
        # Fourteen digits, the way a Digital Link spells AI 01 — not the
        # thirteen the barcode field holds. Building this out of
        # product.barcode, as this test used to, asks the inbox to resolve a
        # spelling no real sender produces.
        identifier = "https://id.gs1.org/01/0%s/10/%s" % (product.barcode, lot.name)
        self.query.pages = [[event("urn:uuid:3", epc=identifier, party=TEST_PARTNER_GLN)]]

        self._poll()

        row = self._rows()
        self.assertEqual(row.res_model, "stock.lot")
        self.assertEqual(row.res_id, lot.id)
        self.assertEqual(row.state, "posted")
        self.assertEqual(row.party_gln, TEST_PARTNER_GLN)
        self.assertTrue(
            any("EPCIS" in (m.body or "") for m in lot.message_ids),
            "the lot's chatter is where somebody will actually see it",
        )

    def test_nothing_that_comes_in_moves_stock_unless_somebody_said_so(self):
        product = self._published_product(tracking="lot")
        lot = self._lot("LOT-952-2", product)
        before = sum(self.env["stock.quant"].search([("lot_id", "=", lot.id)]).mapped("quantity"))
        identifier = "https://id.gs1.org/01/%s/10/%s" % (product.barcode, lot.name)
        self.query.pages = [[event("urn:uuid:4", biz_step="shipping", epc=identifier)]]

        self._poll()

        after = sum(self.env["stock.quant"].search([("lot_id", "=", lot.id)]).mapped("quantity"))
        self.assertEqual(before, after, "an observation is not a document by default")
