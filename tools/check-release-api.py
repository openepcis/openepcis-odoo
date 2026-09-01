#!/usr/bin/env python3
# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Check these addons against an Odoo release they are not running on.

The port to 19.0 cost three CI rounds, and every one of them was avoidable: a
model that had been renamed, a search view whose schema had tightened, and a
handful of anchors that might or might not still exist in the core views we
extend. All three are answerable from the target release's source without an
Odoo, a database, or the right processor architecture — this box is arm64 and
the Odoo images are amd64, so running the other release locally is not an
option at all.

    python tools/check-release-api.py          # the other branch's release
    python tools/check-release-api.py 20       # a release we do not target yet

Three questions, each one a break we actually hit:

* **Do the models we extend still exist?** ``stock.quant.package`` became
  ``stock.package`` in 19, and nothing says so until Odoo refuses to load.
* **Do our views still satisfy the schema?** Odoo 19 stopped allowing ``expand``
  on a search group. The RelaxNG files are the authority, so they are fetched
  and used rather than approximated.
* **Do the anchors we inherit from still exist?** Every ``inherit_id`` and every
  xpath or ``position`` inside it is resolved against the real core view.

What it deliberately does **not** check is whether individual *fields* still
exist — ``stock.move.name``, the break that cost 51 tests. Knowing a model's
fields means resolving the whole inheritance graph across every installed
module, which is a job for a running Odoo rather than for a text search. The
port check in CI answers that one by installing and running the suite.

Sources are read from GitHub and cached under ``.odoo-src-cache``; a second run
costs nothing. Exit status is 0 when the release holds no surprises.
"""

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

try:
    from lxml import etree
except ImportError:
    sys.exit("This needs lxml: pip install lxml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / ".odoo-src-cache"
RAW = "https://raw.githubusercontent.com/odoo/odoo/%s/%s"
API = "https://api.github.com/repos/odoo/odoo/contents/%s?ref=%s"

# Where a module's source lives. Everything else sits under addons/.
ROOTED = {"base": "odoo/addons/base"}

# Modules whose models we use without depending on them explicitly, because
# Odoo always has them.
ALWAYS = ("base", "uom")

# The schemas Odoo validates a view against, by the root tag of its arch. Form
# views have no RelaxNG of their own and are checked by other means inside Odoo,
# so they are only parsed here, not validated.
SCHEMAS = {
    "search": "search_view.rng",
    "list": "list_view.rng",
    "graph": "graph_view.rng",
    "pivot": "pivot_view.rng",
    "calendar": "calendar_view.rng",
    "activity": "activity_view.rng",
}
RNG_DIR = "odoo/addons/base/rng"

MODEL_NAME_RE = re.compile(r"""^\s*_name\s*=\s*['"]([\w.]+)['"]""", re.M)
INHERIT_RE = re.compile(r"""_inherit\s*=\s*['"]([\w.]+)['"]""")
ENV_RE = re.compile(r"""env\[['"]([\w.]+)['"]\]""")


def get(url, binary=False):
    """Fetch a URL once, then from the cache."""
    target = CACHE / re.sub(r"[^A-Za-z0-9._-]", "_", url)
    if target.exists():
        return target.read_bytes() if binary else target.read_text(encoding="utf-8")
    CACHE.mkdir(exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "openepcis-odoo"})
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", "Bearer %s" % token)
    try:
        body = urllib.request.urlopen(request, timeout=60).read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            target.write_bytes(b"")
            return b"" if binary else ""
        sys.exit("Could not read %s: %s" % (url, error))
    target.write_bytes(body)
    return body if binary else body.decode("utf-8")


def module_path(module):
    return ROOTED.get(module, "addons/%s" % module)


def dependencies():
    """The Odoo modules these addons build on, from the manifests themselves."""
    modules = set(ALWAYS)
    ours = {path.parent.name for path in ROOT.glob("openepcis_connector*/__manifest__.py")}
    for path in sorted(ROOT.glob("openepcis_connector*/__manifest__.py")):
        text = path.read_text(encoding="utf-8")
        block = re.search(r'"depends"\s*:\s*\[(.*?)\]', text, re.S)
        if block:
            modules.update(re.findall(r'"([\w.]+)"', block.group(1)))
    return sorted(modules - ours)


# ----------------------------------------------------------------------
# Do the models we extend still exist?
# ----------------------------------------------------------------------


def our_models():
    """Models these addons declare themselves — never in question."""
    found = set()
    for path in ROOT.glob("openepcis_connector*/**/*.py"):
        found.update(MODEL_NAME_RE.findall(path.read_text(encoding="utf-8")))
    return found


def foreign_models():
    """Models these addons extend or reach for, with the file that does it."""
    mine = our_models()
    found = {}
    for path in sorted(ROOT.glob("openepcis_connector*/**/*.py")):
        if "/vendor/" in str(path):
            continue  # a plain library, no Odoo models
        text = path.read_text(encoding="utf-8")
        for name in set(INHERIT_RE.findall(text)) | set(ENV_RE.findall(text)):
            if name not in mine:
                found.setdefault(name, str(path.relative_to(ROOT)))
    return found


def model_index(release, module):
    """Every model declared by one core module, read once and remembered."""
    listing = get(API % ("%s/models" % module_path(module), "%s.0" % release))
    if not listing:
        return {}
    index = {}
    for entry in json.loads(listing):
        if not entry["name"].endswith(".py"):
            continue
        source = get(RAW % ("%s.0" % release, entry["path"]))
        for name in MODEL_NAME_RE.findall(source):
            index[name] = entry["path"]
    return index


def check_models(release, modules, problems):
    wanted = foreign_models()
    known = {}
    for module in modules:
        for name, where in model_index(release, module).items():
            known.setdefault(name, where)
    for name in sorted(wanted):
        if name not in known:
            problems.append(
                "%s: model %r does not exist on Odoo %s — it was renamed or moved"
                % (wanted[name], name, release)
            )
    return len(wanted)


# ----------------------------------------------------------------------
# Do our views still satisfy the schema, and do their anchors still exist?
# ----------------------------------------------------------------------


def our_views():
    """Every ir.ui.view this repository defines, with its arch and its parent."""
    for path in sorted(ROOT.glob("openepcis_connector*/**/*.xml")):
        try:
            tree = etree.parse(str(path))
        except etree.XMLSyntaxError as error:
            yield str(path.relative_to(ROOT)), None, None, None, str(error)
            continue
        for record in tree.iter("record"):
            if record.get("model") != "ir.ui.view":
                continue
            arch = record.find("field[@name='arch']")
            if arch is None:
                continue
            parent = record.find("field[@name='inherit_id']")
            yield (
                str(path.relative_to(ROOT)),
                record.get("id"),
                arch,
                parent is not None and parent.get("ref") or None,
                None,
            )


def validators(release):
    """The release's own RelaxNG files, so the rules are its rules."""
    directory = CACHE / ("rng-%s" % release)
    directory.mkdir(parents=True, exist_ok=True)
    names = set(SCHEMAS.values()) | {"common.rng"}
    for name in names:
        target = directory / name
        if not target.exists():
            target.write_text(
                get(RAW % ("%s.0" % release, "%s/%s" % (RNG_DIR, name))), encoding="utf-8"
            )
    built = {}
    for tag, name in SCHEMAS.items():
        path = directory / name
        if path.stat().st_size:
            built[tag] = etree.RelaxNG(etree.parse(str(path)))
    return built


def check_view_schemas(release, problems):
    """Only views that define an arch of their own — an inherited one is a
    fragment of xpaths and is validated by Odoo after it has been applied."""
    built = validators(release)
    checked = 0
    for path, rid, arch, parent, broken in our_views():
        if broken:
            problems.append("%s: not well-formed XML — %s" % (path, broken))
            continue
        if parent:
            continue
        for child in arch:
            if child.tag not in built:
                continue
            checked += 1
            if not built[child.tag].validate(child):
                for error in built[child.tag].error_log:
                    problems.append(
                        "%s: view %s does not satisfy Odoo %s's <%s> schema — %s"
                        % (path, rid, release, child.tag, error.message)
                    )
    return checked


def core_view(release, module, view_id):
    """The arch of one core view, found by looking through its module."""
    for folder in ("views", "report", "wizard", "data"):
        listing = get(API % ("%s/%s" % (module_path(module), folder), "%s.0" % release))
        if not listing:
            continue
        for entry in json.loads(listing):
            if not entry["name"].endswith(".xml"):
                continue
            source = get(RAW % ("%s.0" % release, entry["path"]))
            if 'id="%s"' % view_id not in source:
                continue
            for record in etree.fromstring(source.encode("utf-8")).iter("record"):
                if record.get("id") == view_id and record.get("model") == "ir.ui.view":
                    arch = record.find("field[@name='arch']")
                    if arch is not None:
                        return arch
    return None


def anchors(arch):
    """What an inherited arch expects to find in its parent."""
    for child in arch:
        if child.tag is etree.Comment:
            continue
        if child.tag == "xpath" and child.get("expr"):
            yield child.get("expr"), child.get("expr")
        elif child.get("position"):
            # Odoo matches the element by tag plus the attributes given.
            attrs = "".join(
                "[@%s='%s']" % (key, value)
                for key, value in sorted(child.attrib.items())
                if key != "position"
            )
            yield (
                ".//%s%s" % (child.tag, attrs),
                "<%s%s position=%s>" % (child.tag, attrs, child.get("position")),
            )


def check_anchors(release, problems):
    checked = 0
    for path, rid, arch, parent, broken in our_views():
        if broken or not parent or "." not in parent:
            continue
        module, view_id = parent.split(".", 1)
        if module.startswith("openepcis_"):
            continue  # ours, and checked by Odoo when it loads
        target = core_view(release, module, view_id)
        if target is None:
            problems.append(
                "%s: view %s inherits %s, which does not exist on Odoo %s"
                % (path, rid, parent, release)
            )
            continue
        for expression, shown in anchors(arch):
            checked += 1
            try:
                hits = target.xpath(expression if expression.startswith(".") else "." + expression)
            except etree.XPathEvalError as error:
                problems.append(
                    "%s: view %s has an unusable xpath %s — %s" % (path, rid, shown, error)
                )
                continue
            if not hits:
                problems.append(
                    "%s: view %s expects %s in %s, which Odoo %s no longer has"
                    % (path, rid, shown, parent, release)
                )
    return checked


def target_release(argv):
    """The release to check against: given, or the branch's counterpart."""
    given = [arg for arg in argv[1:] if not arg.startswith("-")]
    if given:
        return given[0]
    # Imported by path, because the module's file name has hyphens in it.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "idioms", ROOT / "tools" / "check-release-idioms.py"
    )
    idioms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(idioms)
    problems = []
    mine = idioms.target_release(problems)
    if mine is None:
        sys.exit("Cannot tell which release this branch targets: %s" % "; ".join(problems))
    return str(idioms.other(mine))


def release_exists(release):
    return bool(get(RAW % ("%s.0" % release, "odoo/release.py")))


def main(argv):
    release = target_release(argv)
    if not release_exists(release):
        sys.exit("Odoo has no %s.0 branch — nothing to check against." % release)
    print("Checking these addons against Odoo %s." % release, flush=True)
    modules = dependencies()
    print("  core modules: %s" % ", ".join(modules), flush=True)

    problems = []
    models = check_models(release, modules, problems)
    views = check_view_schemas(release, problems)
    found = check_anchors(release, problems)
    print("  %d foreign models, %d view schemas, %d inheritance anchors" % (models, views, found))

    for problem in problems:
        print("  %s" % problem, file=sys.stderr)
    if problems:
        # Run against the release the other maintained branch targets, a
        # difference here is the expected answer rather than a defect: it is
        # the list that branch has already adapted. Run against a release
        # nobody has ported to yet, it is the work ahead.
        print(
            "\n%d thing(s) would need adapting for Odoo %s." % (len(problems), release),
            file=sys.stderr,
        )
        return 1
    print("Nothing in these addons would need adapting for Odoo %s." % release)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
