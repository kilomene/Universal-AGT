#!/usr/bin/env python3
"""Lightweight static check for the Universal AGT dashboard (no test harness).

1. `node --check js/app.js` — syntax must be valid.
2. Every element id referenced in app.js (el('...') / getElementById("..."))
   must exist as id="..." in index.html.
3. Every badge class in STATUS_CLASS must be defined in css/style.css.
4. Every panel in the PANELS list must have matching <id>Error / <id>Updated
   elements in index.html.

Exit 0 when everything holds, 1 otherwise (CI-friendly).
"""

import re
import subprocess
import sys
from pathlib import Path

DASH = Path(__file__).resolve().parent
HTML = DASH / "index.html"
JS = DASH / "js" / "app.js"
CSS = DASH / "css" / "style.css"

failures = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, ("— " + detail) if detail and not cond else "")
    if not cond:
        failures.append(name)


def main():
    for p in (HTML, JS, CSS):
        check(f"exists: {p.name}", p.is_file())

    html = HTML.read_text()
    js = JS.read_text()
    css = CSS.read_text()

    # 1. syntax
    r = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True)
    check("node --check js/app.js", r.returncode == 0, r.stderr.strip()[:300])

    # 2. element ids referenced in JS exist in HTML
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r"""\bel\(\s*['"]([^'"]+)['"]\s*\)""", js))
    js_ids |= set(re.findall(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""", js))
    # dynamic ids built as 'apprMsg-' + taskId are created by dossierHtml();
    # verify the prefix contract separately instead of treating it as missing.
    dynamic = {i for i in js_ids if i == "apprMsg-"}
    missing = sorted(i for i in js_ids - html_ids if i not in dynamic)
    check("JS element ids exist in HTML", not missing, f"missing: {missing}")
    check(
        "apprMsg- dynamic id contract",
        "apprMsg-" in js
        and "el('apprMsg-' + taskId)" in js
        and 'id="apprMsg-' in js,
        "dossierHtml must create apprMsg-<taskId> elements read via el('apprMsg-' + taskId)",
    )

    # 3. badge classes
    m = re.search(r"STATUS_CLASS\s*=\s*\{(.*?)\};", js, re.S)
    css_classes = set(re.findall(r"\.(st-[a-z]+)\s*\{", css))
    if m:
        js_classes = set(re.findall(r"['\"](st-[a-z]+)['\"]", m.group(1)))
        missing_cls = sorted(js_classes - css_classes)
        check("STATUS_CLASS badge classes defined in CSS", not missing_cls, f"missing: {missing_cls}")
    else:
        check("STATUS_CLASS found in app.js", False)

    # 4. panel wiring: every PANELS entry needs <id>Error and <id>Updated
    panel_ids = re.findall(r"\{\s*id:\s*'([a-z]+)'", js)
    for pid in panel_ids:
        check(
            f"panel '{pid}' has Error/Updated elements",
            f'id="{pid}Error"' in html and f'id="{pid}Updated"' in html,
        )

    # 5. mutating calls never carry the key as a query param
    check(
        "no api_key query param in mutating calls",
        "apiWrite" in js and not re.search(r"apiWrite\([^)]*api_key", js),
    )
    check(
        "approve/reject/service actions go through apiWrite",
        all(s in js for s in ("apiWrite('POST', '/tasks/'", "apiWrite('POST', '/services/'")),
    )

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("all dashboard static checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
