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


# ---------------------------------------------------------------------------
# Endpoint consistency (§46): parse the backend route table out of the
# control-plane sources and verify every frontend API call site in app.js
# against it (method + path). Paths with `:param` segments on either side
# match any single concrete segment.
# ---------------------------------------------------------------------------

API_SRC = DASH.parent / "control-plane" / "api" / "src"


def _parse_backend_routes():
    """Return {(method, path)} with paths like /v1/deployments/:id."""
    routes = set()
    router_prefix = {}  # router var name -> list of /v1/... prefixes

    index = (API_SRC / "index.ts").read_text()
    # v1.use('/prefix', xxxRouter)  and  app.use('/v1/health', healthRouter)
    for m in re.finditer(r"(?:v1|app)\.use\('([^']+)',\s*(\w+)\)", index):
        mount, router = m.group(1), m.group(2)
        if mount == "/v1":
            continue  # the bare v1 router itself
        full = mount if mount.startswith("/v1") else "/v1" + mount
        router_prefix.setdefault(router, []).append(full)
    # one-off: PUT /v1/artifacts/:id/content is registered directly on app
    for m in re.finditer(r"app\.(get|post|put|patch|delete)\(\s*'(/v1/[^']+)'", index):
        routes.add((m.group(1).upper(), m.group(2)))

    # nested mounts: projectsRouter.use('/:id/secrets', secretsRouter)
    nested = []  # (parent_router, subpath, child_router)
    route_files = sorted((API_SRC / "routes").glob("*.ts"))
    for path in route_files:
        text = path.read_text()
        for m in re.finditer(r"(\w+)\.use\('([^']+)',\s*(\w+)\)", text):
            nested.append((m.group(1), m.group(2), m.group(3)))

    for path in route_files:
        text = path.read_text()
        routers = set(re.findall(r"(\w+)\.(?:get|post|put|patch|delete)\('", text))
        own = {}
        for m in re.finditer(r"(\w+)\.(get|post|put|patch|delete)\('([^']+)'", text):
            own.setdefault(m.group(1), []).append(
                (m.group(2).upper(), m.group(3))
            )
        for router, methods in own.items():
            prefixes = router_prefix.get(router, [])
            # nested routers inherit their parent's prefix + subpath
            for parent, subpath, child in nested:
                if child == router:
                    for pp in router_prefix.get(parent, []):
                        prefixes = prefixes + [pp.rstrip("/") + subpath]
            for prefix in prefixes or [""]:
                for method, sub in methods:
                    full = (prefix + sub).replace("//", "/")
                    if len(full) > 1:
                        full = full.rstrip("/")
                    routes.add((method, full))
    return routes


def _normalize_frontend_expr(expr):
    """Turn a JS path expression into a path with :param for variables.

    e.g. "'/services/' + id + '/' + action" -> "/services/:param/:param".
    The leading API base constant is dropped. Returns None when the
    expression has no static string part to anchor on.
    """
    chunks = [c.strip() for c in expr.split("+")]
    if chunks and re.fullmatch(r"[A-Za-z_$][\w$]*", chunks[0]) and not chunks[0].startswith(("'", '"')):
        chunks = chunks[1:]  # the API base constant
    parts = []
    for c in chunks:
        m = re.fullmatch(r"""['"]([^'"]*)['"]""", c)
        if m:
            parts.append(m.group(1))
        else:
            parts.append(":param")
    path = "".join(parts).split("?")[0]
    if not path.startswith("/"):
        return None
    return path or None


def _parse_frontend_calls(js):
    """Return [(method, path, snippet)] for api()/apiWrite()/get()/EventSource call sites."""
    calls = []
    # api('<expr>') -> GET ; the local get('<expr>', key) helper also calls api()
    for m in re.finditer(r"\b(?:api|get)\(\s*([^)]*?)\)", js):
        expr = m.group(1).strip()
        if not expr:
            continue
        # skip api(p.path) / api(path) — dynamic, nothing static to verify
        if re.fullmatch(r"[A-Za-z_$][\w$.]*", expr):
            continue
        path = _normalize_frontend_expr(expr)
        if path:
            calls.append(("GET", path, m.group(0)[:60]))
    # PANELS entries carry their own path literals (consumed via api(p.path))
    for m in re.finditer(r"""\bpath:\s*['"]([^'"]+)['"]""", js):
        path = _normalize_frontend_expr("'" + m.group(1) + "'")
        if path:
            calls.append(("GET", path, "PANELS path: " + m.group(1)[:50]))
    for m in re.finditer(r"""\bapiWrite\(\s*['"]([A-Z]+)['"]\s*,\s*([^)]*?)\)""", js):
        # the path expression ends at the first top-level comma (a body may follow)
        expr = m.group(2).split(",", 1)[0].strip()
        path = _normalize_frontend_expr(expr)
        if path:
            calls.append((m.group(1), path, m.group(0)[:60]))
    for m in re.finditer(r"""['"](/events/stream[^'"]*)['"]""", js):
        calls.append(("GET", m.group(1).split("?")[0], "EventSource " + m.group(1)[:40]))
    return calls


def _path_matches(route_path, call_path):
    # Backend paths carry the /v1 prefix; the frontend's API base is 'v1',
    # so its call paths never include it.
    if route_path.startswith("/v1/"):
        route_path = route_path[3:]
    rs, cs = route_path.split("/"), call_path.split("/")
    if len(rs) != len(cs):
        return False
    return all(r == c or r.startswith(":") or c == ":param" for r, c in zip(rs, cs))


def check_endpoint_consistency(js):
    """Yield (label, detail) — detail is "" when the check passes."""
    try:
        backend = _parse_backend_routes()
    except FileNotFoundError as exc:
        yield ("endpoint consistency: backend sources readable", f"{exc}")
        return
    if not backend:
        yield ("endpoint consistency: backend routes parsed", "no routes found — parser broken?")
        return
    yield ("endpoint consistency: backend routes parsed", "")
    seen = set()
    for method, path, snippet in _parse_frontend_calls(js):
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        ok = any(m == method and _path_matches(rp, path) for m, rp in backend)
        label = f"endpoint {method} {path}"
        yield (label, "" if ok else f"no backend route matches (near: {snippet})")


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

    # 6. endpoint consistency: every frontend API call must match a real
    # backend route (method + path). This is the living contract test for
    # §46 — it fails closed when the dashboard drifts from the API.
    for label, detail in check_endpoint_consistency(js):
        check(label, not detail, detail or "")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("all dashboard static checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
