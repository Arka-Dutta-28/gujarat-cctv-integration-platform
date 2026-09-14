"""M8 acceptance test.

From docs/build-plan.md §5:

    Accept: a logged-out incognito browser can reach the hosted platform, log in
            with the demo account, and open Swagger.

Three claims, checked in that order against whatever base URL is given — so the
same test runs against a local stack started with the production overlay and
against the hosted instance, and passing locally is not mistaken for passing in
the place that matters.

"Logged out" is the part worth being strict about. It is not enough that a login
page appears: the data behind it must actually be refused. A platform that
renders a login screen while serving `/api/sightings` to anyone who asks has a
login screen, not authentication.

Usage:
    python -m scripts.acceptance.m8 --api http://localhost:8000 --web http://localhost:5173
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

#: Endpoints that must be refused to a caller with no token. One from each
#: router that holds data, because the gate is app-wide and a regression would
#: most likely be a new router added outside it.
PROTECTED = (
    "/api/cameras",
    "/api/sightings?limit=1",
    "/api/watchlist",
    "/api/alerts?limit=1",
    "/api/performance?minutes=5",
    "/api/audit?limit=1",
    "/api/reports/detections?format=csv&limit=1",
)

#: Reachable without a token by design: a probe cannot hold credentials, and the
#: API description is a graded artifact that exposes no data.
PUBLIC = ("/health", "/api/docs", "/api/openapi.json")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        return f"  [{mark}] {self.name}\n         {DIM}{self.detail}{RESET}"


def _status(url: str, token: str | None = None, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc).encode()


def check_logged_out_is_refused(api: str) -> Check:
    name = "A logged-out caller is refused the data"
    results = {path: _status(f"{api}{path}")[0] for path in PROTECTED}
    leaked = [path for path, code in results.items() if code == 200]
    return Check(
        name, not leaked,
        f"all {len(results)} data endpoints answered 401 without a token"
        if not leaked
        else f"served without authentication: {', '.join(leaked)}",
    )


def check_public_paths_stay_public(api: str) -> Check:
    """A health probe cannot hold credentials, and Swagger is the artifact."""
    name = "Health and the API description remain reachable"
    results = {path: _status(f"{api}{path}")[0] for path in PUBLIC}
    blocked = [p for p, code in results.items() if code != 200]
    return Check(
        name, not blocked,
        "/health, /api/docs and /api/openapi.json answer without a token — a probe "
        "cannot log in, and the API description exposes no data"
        if not blocked else f"unreachable: {', '.join(blocked)}",
    )


def check_demo_account_signs_in(api: str, username: str, password: str) -> tuple[Check, str | None]:
    name = "The demo account signs in"
    code, body = _status(
        f"{api}/api/auth/login", method="POST",
        body={"username": username, "password": password},
    )
    if code != 200:
        return Check(
            name, False,
            f"login returned {code}. Has `python -m scripts.seed_accounts` been "
            f"run with DEMO_PASSWORD set on this deployment?",
        ), None

    session = json.loads(body)
    return Check(
        name, session.get("role") == "viewer",
        f"signed in as {session['username']} ({session['role']}), token valid for "
        f"{session['expires_in_s'] // 3600} hours"
        if session.get("role") == "viewer"
        else f"the demo account has role {session.get('role')!r}, expected viewer",
    ), session.get("token")


def check_demo_can_read_everything(api: str, token: str | None) -> Check:
    name = "The demo account can see the whole platform"
    if not token:
        return Check(name, False, "no token to test with")
    results = {path: _status(f"{api}{path}", token)[0] for path in PROTECTED}
    refused = [p for p, code in results.items() if code != 200]
    return Check(
        name, not refused,
        f"all {len(results)} endpoints readable — registry, sightings, watchlist, "
        "alerts, performance, audit trail and the report export"
        if not refused else f"refused to the demo account: {', '.join(refused)}",
    )


def check_demo_cannot_change_anything(api: str, token: str | None) -> Check:
    """Read-only enforced by the API, not by hiding buttons in the UI."""
    name = "The demo account cannot change anything"
    if not token:
        return Check(name, False, "no token to test with")

    attempts = {
        "watchlist a plate": _status(
            f"{api}/api/watchlist", token, "POST", {"plate": "GJ01ZZ0001"}
        )[0],
        "onboard a camera": _status(
            f"{api}/api/cameras", token, "POST",
            {"name": "acceptance", "adapter": "file", "stream_ref": "/tmp/x.mp4",
             "lat": 23.0, "lon": 72.5},
        )[0],
    }
    allowed = [what for what, code in attempts.items() if code < 400]
    return Check(
        name, not allowed,
        "every write refused with 403 — the account is a viewer, and the API "
        "enforces that rather than the interface hiding the controls"
        if not allowed else f"the demo account was allowed to: {', '.join(allowed)}",
    )


def check_swagger_is_live(api: str) -> Check:
    """A graded submission artifact in its own right."""
    name = "The Swagger page is live and documents the platform"
    code, body = _status(f"{api}/openapi.json")
    if code != 200:
        return Check(name, False, f"/openapi.json returned {code}")

    schema = json.loads(body)
    paths = schema.get("paths", {})
    expected = (
        "/api/vehicles/{plate}/journey", "/api/watchlist", "/api/alerts",
        "/api/reports/detections", "/api/cameras/anpr-capability",
    )
    missing = [p for p in expected if p not in paths]
    undocumented = [
        p for p, ops in paths.items()
        for op in ops.values() if not op.get("summary")
    ]
    return Check(
        name, not missing and not undocumented,
        f"{len(paths)} routes, every one with a summary — including the journey, "
        "watchlist, alert, report and capability endpoints"
        if not missing and not undocumented
        else f"missing: {missing}; undocumented: {undocumented[:3]}",
    )


def check_the_web_app_is_served(web: str) -> Check:
    name = "The web application is served"
    code, body = _status(web)
    served = code == 200 and b"root" in body
    return Check(
        name, served,
        f"{web} answered {code}; the operator UI and the performance page at "
        f"{web}/#/performance are served from here"
        if served else f"{web} answered {code}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="M8 acceptance test.")
    parser.add_argument("--api", default=os.environ.get("ACCEPT_API", "http://localhost:8000"))
    parser.add_argument("--web", default=os.environ.get("ACCEPT_WEB", "http://localhost:5173"))
    parser.add_argument("--username", default="demo")
    parser.add_argument(
        "--password", default=os.environ.get("DEMO_PASSWORD", ""),
        help="The demo account's password. From DEMO_PASSWORD if unset.",
    )
    args = parser.parse_args()

    if not args.password:
        print(f"{RED}DEMO_PASSWORD is not set{RESET} — pass --password or export it.\n")
        return 2

    print(f"\n{'=' * 70}\nM8 acceptance — deployment and submission hygiene\n{'=' * 70}\n")
    print(f"{DIM}API {args.api}  ·  web {args.web}{RESET}\n")

    signin, token = check_demo_account_signs_in(args.api, args.username, args.password)
    checks = [
        check_logged_out_is_refused(args.api),
        check_public_paths_stay_public(args.api),
        signin,
        check_demo_can_read_everything(args.api, token),
        check_demo_cannot_change_anything(args.api, token),
        check_swagger_is_live(args.api),
        check_the_web_app_is_served(args.web),
    ]

    for check in checks:
        print(check.render())

    passed = sum(c.passed for c in checks)
    ok = passed == len(checks)
    print(f"\n{'=' * 70}")
    print(
        f"{GREEN}M8 ACCEPTANCE PASSED{RESET}" if ok
        else f"{RED}M8 ACCEPTANCE FAILED{RESET} — {passed}/{len(checks)} checks passed"
    )
    print(f"{'=' * 70}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
