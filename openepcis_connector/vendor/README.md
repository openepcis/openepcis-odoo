<!-- Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3). -->

# Vendored libraries

## benelog_client

A verbatim copy of the `benelog_client` package from
[benelog-client-python](https://code.company-group.com/openepcis/benelog-client-python),
licensed Apache-2.0 (see `benelog_client/LICENSE`). Apache-2.0 combines one-way
into this addon's LGPL-3; the vendored files keep their own headers and licence.

Vendored so the addon stays a drop-in: it must install on any Odoo, including
Odoo Online, without a Python package beyond what Odoo ships. The library's
only dependency is `requests`, which Odoo depends on itself.

**Never edit files under `vendor/` by hand.** Fix the library upstream and
re-vendor:

```bash
tools/vendor_benelog_client.sh [path-or-url] [ref]
```

The script records the vendored commit below.

Vendored commit: `6f61cef`
