# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Queueing, publishing, and what happens when publishing fails.

The behaviours pinned here are the ones that make the connector safe to install
on a live database: nothing is published without being asked for, saving never
blocks on the network, a bad record does not take a batch down with it, and a
retry can never create a duplicate.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.openepcis_sync_mixin import MAX_ATTEMPTS
from ..utils.exceptions import OpenepcisError
from .common import TEST_GTIN, TEST_GTIN_2, OpenepcisCase


@tagged("post_install", "-at_install")
class TestQueueing(OpenepcisCase):
    def test_nothing_is_published_without_being_asked(self):
        product = self._product()
        self.assertFalse(product.openepcis_publish)
        self.assertEqual(product.openepcis_state, "not_synced")

    def test_opting_in_queues_the_record(self):
        product = self._product()
        product.openepcis_publish = True
        self.assertEqual(product.openepcis_state, "queued")

    def test_saving_does_not_call_the_resolver(self):
        calls = self.capture()
        product = self._product()
        product.openepcis_publish = True
        product.name = "Renamed"
        self.assertEqual(calls, [], "saving must never wait on the network")

    def test_changing_a_mapped_field_re_queues(self):
        product = self._product()
        product.openepcis_publish = True
        product.with_context(openepcis_syncing=True).openepcis_state = "synced"
        product.openepcis_brand_name = "Another brand"
        self.assertEqual(product.openepcis_state, "queued")

    def test_changing_an_unmapped_field_leaves_it_alone(self):
        product = self._product()
        product.openepcis_publish = True
        product.with_context(openepcis_syncing=True).openepcis_state = "synced"
        product.sale_ok = not product.sale_ok
        self.assertEqual(product.openepcis_state, "synced")

    def test_recording_an_outcome_does_not_re_queue(self):
        product = self._product()
        product.openepcis_publish = True
        product._openepcis_record_success()
        self.assertEqual(product.openepcis_state, "synced")

    def test_a_record_without_a_key_is_not_queued(self):
        product = self._product(barcode=False)
        product.openepcis_publish = True
        self.assertEqual(product.openepcis_state, "not_synced")


@tagged("post_install", "-at_install")
class TestPublishing(OpenepcisCase):
    def test_publishes_with_put_to_the_key_path(self):
        calls = self.capture()
        product = self._product()
        product.openepcis_publish = True
        product._openepcis_sync()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "PUT")
        self.assertEqual(calls[0]["path"], "/products/%s" % TEST_GTIN)
        self.assertEqual(product.openepcis_state, "synced")
        self.assertTrue(product.openepcis_last_sync)

    def test_put_rather_than_post_so_a_repeat_is_harmless(self):
        # POST answers 409 for a key the catalog already holds; PUT is
        # create-or-update, which is the only safe verb for a retrying queue.
        calls = self.capture()
        product = self._product()
        product.openepcis_publish = True
        product._openepcis_sync()
        product._openepcis_sync()
        self.assertEqual({call["method"] for call in calls}, {"PUT"})

    def test_the_payload_carries_the_key(self):
        calls = self.capture()
        product = self._product()
        product.openepcis_publish = True
        product._openepcis_sync()
        self.assertEqual(calls[0]["payload"]["gtin"], TEST_GTIN)

    def test_a_bad_check_digit_is_caught_before_any_call(self):
        calls = self.capture()
        product = self._product(barcode="9520000000007")  # correct digit is 4
        product.openepcis_publish = True
        product._openepcis_sync()

        self.assertEqual(calls, [], "an invalid key must not reach the network")
        self.assertEqual(product.openepcis_state, "error")
        self.assertIn("4", product.openepcis_error)

    def test_one_bad_record_does_not_stop_the_batch(self):
        self.capture()
        good = self._product()
        bad = self._product(barcode="9520000000007")
        products = good | bad
        products.openepcis_publish = True
        products._openepcis_sync()

        self.assertEqual(good.openepcis_state, "synced")
        self.assertEqual(bad.openepcis_state, "error")

    def test_a_terminal_failure_is_recorded_not_raised(self):
        self.capture(error=OpenepcisError("Missing GS1-required fields", status=422))
        product = self._product()
        product.openepcis_publish = True
        product._openepcis_sync()

        self.assertEqual(product.openepcis_state, "error")
        self.assertIn("Missing GS1-required fields", product.openepcis_error)

    def test_a_retryable_failure_stays_queued_then_gives_up(self):
        self.capture(error=OpenepcisError("Gateway timeout", status=504))
        product = self._product()
        product.openepcis_publish = True

        for attempt in range(1, MAX_ATTEMPTS):
            product._openepcis_sync()
            self.assertEqual(product.openepcis_state, "queued", "attempt %s" % attempt)

        product._openepcis_sync()
        self.assertEqual(product.openepcis_state, "error", "must not retry for ever")

    def test_cron_only_takes_queued_records(self):
        calls = self.capture()
        queued = self._product()
        queued.openepcis_publish = True
        untouched = self._product(barcode=TEST_GTIN_2)

        self.env["product.product"]._openepcis_cron_sync()

        self.assertEqual(len(calls), 1)
        self.assertEqual(queued.openepcis_state, "synced")
        self.assertEqual(untouched.openepcis_state, "not_synced")

    def test_cron_does_nothing_while_publishing_is_switched_off(self):
        calls = self.capture()
        product = self._product()
        product.openepcis_publish = True
        self.company.openepcis_enabled = False

        self.env["product.product"]._openepcis_cron_sync()

        self.assertEqual(calls, [])
        self.assertEqual(product.openepcis_state, "queued")


@tagged("post_install", "-at_install")
class TestDigitalLink(OpenepcisCase):
    def test_link_appears_only_once_published(self):
        self.capture()
        product = self._product()
        product.openepcis_publish = True
        self.assertFalse(product.openepcis_digital_link)

        product._openepcis_sync()
        self.assertEqual(
            product.openepcis_digital_link, "https://id.example.test/01/%s" % TEST_GTIN
        )

    def test_opening_an_unpublished_link_explains_itself(self):
        product = self._product()
        with self.assertRaises(UserError):
            product.action_openepcis_open_digital_link()


@tagged("post_install", "-at_install")
class TestTemplateMirror(OpenepcisCase):
    def test_template_reports_the_worst_news_from_its_variants(self):
        self.capture()
        template = self._product().product_tmpl_id
        template.openepcis_publish = True
        self.assertEqual(template.openepcis_state, "queued")

        template.product_variant_ids._openepcis_sync()
        self.assertEqual(template.openepcis_state, "synced")

    def test_publishing_a_template_publishes_every_variant(self):
        calls = self.capture()
        template = self._product().product_tmpl_id
        template.action_openepcis_publish()
        self.assertEqual(len(calls), len(template.product_variant_ids))
