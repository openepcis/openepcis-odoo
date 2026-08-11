#!/bin/sh
# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
# Refresh the vendored copy of benelog_client from its repository.
#
# Usage: tools/vendor_benelog_client.sh [path-or-url] [ref]
#   path-or-url  a local clone or the git URL
#                (default: ../benelog-client-python next to this repo)
#   ref          branch, tag or commit to vendor (default: main)
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source=${1:-"$here/../benelog-client-python"}
ref=${2:-main}
target="$here/openepcis_connector/vendor/benelog_client"

workdir=""
if [ ! -d "$source" ]; then
    workdir=$(mktemp -d)
    trap 'rm -rf "$workdir"' EXIT
    git clone --quiet --depth 1 --branch "$ref" "$source" "$workdir"
    source="$workdir"
fi

commit=$(git -C "$source" rev-parse --short HEAD)

rm -rf "$target"
cp -R "$source/src/benelog_client" "$target"
cp "$source/LICENSE" "$target/LICENSE"
find "$target" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

sed -i.bak "s/^Vendored commit: .*/Vendored commit: \`$commit\`/" \
    "$here/openepcis_connector/vendor/README.md"
rm -f "$here/openepcis_connector/vendor/README.md.bak"

echo "vendored benelog_client @ $commit into $target"
