<!-- SPDX-License-Identifier: LGPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG -->

# Demo image

Odoo 18 with the `openepcis_connector` addon preinstalled. This is the image the
`demo-odoo` Terraform module in [openepcis-platform] deploys as a standing demo
system on the dev cluster — the module's `image` variable defaults to the tag
built here.

The image is stock `odoo:18` plus one `COPY`: the addon lands in
`/mnt/extra-addons/openepcis_connector`, which is already on the official
image's `addons_path`. Install the "OpenEPCIS Connector" app from Apps after
creating a database (or pass `-i openepcis_connector` when initialising one).

## Build

The build context is the **repo root**, so the addon directory is inside the
context; the root-level `.containerignore` keeps everything else out of it:

```bash
podman build -f docker/demo/Containerfile \
  -t registry.company-group.com/openepcis/odoo-demo:18 .
```

For a specific target architecture (the dev cluster may run a different one
than your build machine — check with `kubectl get nodes -L kubernetes.io/arch`),
pass `--platform`, e.g.:

```bash
podman build --platform linux/amd64 -f docker/demo/Containerfile \
  -t registry.company-group.com/openepcis/odoo-demo:18 .
```

For a proper multi-arch image, build per platform and assemble a manifest list:

```bash
podman build --platform linux/amd64,linux/arm64 \
  --manifest registry.company-group.com/openepcis/odoo-demo:18 \
  -f docker/demo/Containerfile .
```

## Push

Pushing to the GitLab container registry needs a login first
(`read_registry`/`write_registry` scope):

```bash
podman login registry.company-group.com
podman push registry.company-group.com/openepcis/odoo-demo:18
# or, for the manifest list:
podman manifest push registry.company-group.com/openepcis/odoo-demo:18
```

The image build is deliberately **manual** for now — it is not part of CI.

[openepcis-platform]: https://code.company-group.com/openepcis/openepcis-platform
