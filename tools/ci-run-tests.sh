#!/bin/sh
# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
# Install every addon in this checkout into a fresh database and run its tests.
#
# Two CI jobs call this: the ordinary run against this branch's Odoo, and the
# port check that carries the same change onto the other branch and runs it
# there. One copy on purpose — two would drift, and the port check would then
# quietly be testing something other than what the branch actually runs.
#
# Usage: tools/ci-run-tests.sh <path-to-this-checkout>
set -eu

root=${1:?usage: ci-run-tests.sh <path-to-this-checkout>}

# The events addon declares epcis-event-hash-generator as an external
# dependency (it recognises our own events coming back by their canonical hash).
# Without it Odoo lists the addon as uninstallable and every test in it is
# silently skipped rather than failed — the worst of both.
#
# --no-deps, and not by preference: the package declares Flask, Flask wants a
# newer Werkzeug, and pip cannot uninstall the Debian-packaged Werkzeug that
# Odoo itself runs on ("RECORD file not found"). Replacing Odoo's Werkzeug to
# satisfy a dependency the package never imports would be the wrong trade. Its
# real imports are pyld and dateutil, and those are installed with their chains.
echo "::group::Installing the canonical hash generator"
pip3 install --no-cache-dir --break-system-packages PyLD python-dateutil
pip3 install --no-cache-dir --break-system-packages --no-deps \
    "epcis-event-hash-generator==1.9.3"
echo "::endgroup::"

# Derived from the directories present, not from a list somebody has to
# remember. Both bridge addons carry auto_install, so Odoo would otherwise
# decide for itself whether they apply — and in a bare database neither
# Manufacturing nor Point of Sale is there to trigger them. Naming them pulls
# their dependencies in and puts their tests in the run; without that their code
# would only ever be imported, never executed. That happened once already.
addons=$(cd "$root" && ls -d openepcis_connector* | paste -sd, -)
tags=$(echo "$addons" | tr ',' '\n' | sed 's|^|/|' | paste -sd, -)
echo "Installing: $addons"

odoo -d ci \
    -i "$addons" \
    --addons-path=/usr/lib/python3/dist-packages/odoo/addons,"$root" \
    --db_host=db --db_user=odoo --db_password=odoo \
    --data-dir=/tmp/odoo-data \
    --test-enable --test-tags "$tags" \
    --stop-after-init --log-level=test 2>&1 | tee odoo.log

# Odoo exits 0 even when tests fail, so the log is the source of truth.
if grep -qE "[0-9]+ failed, [0-9]+ error\(s\)" odoo.log \
   && ! grep -q "0 failed, 0 error(s)" odoo.log; then
    echo "::error::Odoo reported failing tests"
    grep -E "(FAIL|ERROR):" odoo.log || true
    exit 1
fi
grep -q "0 failed, 0 error(s)" odoo.log || {
    echo "::error::No test summary in the log — the run did not complete"
    tail -50 odoo.log
    exit 1
}
