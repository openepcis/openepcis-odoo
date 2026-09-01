<!-- SPDX-License-Identifier: LGPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG -->

# Demo image

Odoo 18 with the connector addons preinstalled — `openepcis_connector`, plus the
`_stock` and `_product_expiry` bridges. This is the image the `demo-odoo`
Terraform module in [openepcis-platform] deploys as a standing demo system on
the dev cluster; the module's `image` variable defaults to the tag built here,
and its module list installs the apps.

The image is stock `odoo:19` plus one `COPY` layer per addon into
`/mnt/extra-addons`, which is already on the official image's `addons_path`.
Nothing is installed at build time: which apps a database gets is the
deployment's decision, not the image's, and the two bridges carry `auto_install`
so Odoo activates them by itself once `stock` and `product_expiry` are there.

## Where it lives

```
registry.company-group.com/openepcis/openepcis-connectors/odoo-demo:18
```

Under the openepcis-connectors project, beside the UnoPim demo image, although
it is built from neither. A GitLab deploy token reaches exactly one project, and
the dev cluster already holds one for that project in its
`docker-registry-credentials` secret. Giving the demo images of the connector
suite one registry path is a smaller thing to explain than a second credential
in the namespace.

## Build

The build context is the **repo root**, so the addon directories are inside the
context; `.containerignore` keeps everything else out — including the test
suites, which have no business in a demo image.

The dev cluster runs `amd64`; a build machine may not, and Odoo will not start
from the wrong architecture. Pass `--platform` and check with
`kubectl get nodes -L kubernetes.io/arch` if unsure:

```bash
podman build --platform linux/amd64 -f docker/demo/Containerfile \
  -t registry.company-group.com/openepcis/openepcis-connectors/odoo-demo:18 .
```

No emulation is needed for a cross-architecture build here: the image only
copies files, so nothing from the foreign base image is ever executed.

## Push

```bash
podman login registry.company-group.com   # needs write_registry
podman push registry.company-group.com/openepcis/openepcis-connectors/odoo-demo:18
```

The build is deliberately **manual**. This repository's CI runs on GitHub while
the registry is GitLab's, and a demo image is rebuilt when the addon changes
enough to matter — a judgement, not an event worth wiring a pipeline for.

[openepcis-platform]: https://code.company-group.com/openepcis/openepcis-platform
