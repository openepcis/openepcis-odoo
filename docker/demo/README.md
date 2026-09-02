<!-- SPDX-License-Identifier: LGPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG -->

# Demo image

Stock Odoo with all six connector addons preinstalled — the base connector, the
`_stock` and `_product_expiry` bridges, the events addon, and the manufacturing
and point-of-sale bridges. It exists so that a demo system, or a throwaway
instance for a screenshot, can be started from one image instead of from a
checkout.

Nothing is installed at build time. Which apps a database gets is the
deployment's decision, not the image's, and four of the addons carry
`auto_install` — Odoo activates them by itself once the app they extend is
there. An image without Manufacturing installed never activates the
manufacturing bridge, and one without a till never reads a sale as a sale.

The addons are copied into `/mnt/extra-addons`, which the official image
already has on its `addons_path`. On top of that one `RUN` installs the
canonical event-hash generator, which the events addon declares as an external
dependency: without it Odoo lists that addon as uninstallable and every test in
it is silently skipped.

## Build

The build context is the **repo root**, so that the addon directories are inside
it; `.containerignore` keeps everything else out — including the test suites,
which have no business in a demo image.

```bash
podman build -f docker/demo/Containerfile -t odoo-demo:19 .
```

Tag it for wherever you keep images, and push it there.

Two things to know before building for a different architecture than the build
machine. Odoo will not start from the wrong one, so pass `--platform` to match
the host that will run it. And that build does need emulation despite being
mostly `COPY` layers: the `RUN` above executes `pip` from the foreign base
image.

The build is deliberately **manual**. This repository's CI installs the addons
into a real Odoo and runs their tests on every pull request; a demo image is
rebuilt when the addons change enough to matter, which is a judgement rather
than an event worth wiring a pipeline for.
