#!/usr/bin/env python3
# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Compare doc/api-contract.json against a running resolver.

The resolver publishes neither an OpenAPI file nor a client SDK — the spec exists
only at runtime, under ``/q/openapi``. So the connector's assumptions about paths
and verbs are written down in ``doc/api-contract.json``, and this script checks
them against a real deployment.

Not part of the default CI run, because it needs a reachable deployment. Run it
when a resolver release lands, or before publishing a new version of the addon:

    python tools/check-contract.py https://id.dev.epcis.cloud

The contract file defaults to this repo's ``doc/api-contract.json`` but can be
overridden, so other connectors can reuse the script against their own endpoint
subset without forking it:

    python tools/check-contract.py --contract path/to/api-contract.json https://…

Exit status is 0 when every endpoint the connector uses is still there, 1 when
something has moved. A difference means the connector needs attention — do not
"fix" it by editing the contract.
"""

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

DEFAULT_CONTRACT = pathlib.Path(__file__).resolve().parent.parent / "doc" / "api-contract.json"
TIMEOUT = 30


def fetch(base_url):
    url = "%s/q/openapi?format=json" % base_url.rstrip("/")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310
            return json.load(response)
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.exit("Could not read %s: %s" % (url, exc))


def main(argv):
    parser = argparse.ArgumentParser(
        prog="check-contract.py",
        description="Compare an api-contract.json against a running resolver.",
    )
    parser.add_argument("base_url", help="resolver base URL, e.g. https://id.dev.epcis.cloud")
    parser.add_argument(
        "--contract",
        type=pathlib.Path,
        default=DEFAULT_CONTRACT,
        help="contract file to check (default: %(default)s)",
    )
    args = parser.parse_args(argv[1:])

    if not args.contract.is_file():
        sys.exit("Contract file not found: %s" % args.contract)

    contract = json.loads(args.contract.read_text())
    live = fetch(args.base_url).get("paths", {})

    problems = []
    checked = 0
    for path, verbs in contract["endpoints"].items():
        entry = live.get(path)
        if entry is None:
            problems.append("path gone: %s" % path)
            continue
        for verb in verbs:
            if verb.startswith("_"):
                continue
            checked += 1
            if verb not in entry:
                problems.append("verb gone: %s %s" % (verb.upper(), path))

    if problems:
        print("The resolver no longer matches what the connector expects:\n")
        for problem in problems:
            print("  %s" % problem)
        print("\n%s of %s checks passed." % (checked - len(problems), checked))
        return 1

    print(
        "%s endpoint/verb pairs still present across %s paths."
        % (checked, len(contract["endpoints"]))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
