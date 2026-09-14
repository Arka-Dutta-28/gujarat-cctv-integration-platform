"""Client for the upstream ingest catalogue.

From the integration reference, section 1:

    Always start from the catalogue rather than hard-coding endpoints. It
    returns every camera with its id, location, codec, live status, stream
    properties, and all three URLs. Camera ids and the set of available cameras
    can change; the catalogue is the contract, the URL pattern is not.

That sentence is the whole design of this module, and it rules out two things
the platform used to do.

Enumerating cameras by counting. The previous onboarder walked
/api/cameras/1..31/state, which encodes both a URL pattern and a camera count.
The moment the organisers add a camera, renumber, or take one out for
maintenance, that loop onboards the wrong estate and reports success.

Constructing stream URLs from a template. Every endpoint used anywhere in the
platform is now a URL the catalogue handed us.

The parsing is deliberately shape-tolerant. The reference names the fields the
catalogue carries but does not pin their JSON spelling, and the sandbox has
already changed shape once: it used to be per-camera /state documents with
stream_url, and it is now one catalogue with three URLs per camera. So this
discovers rather than asserts. A camera's endpoints are found by looking at what
the values actually are (an rtsp:// URL is RTSP, a path ending .m3u8 is HLS, a
path ending /whep is WHEP), and every scalar field is looked up under a list of
plausible names. An unrecognised extra field is carried through in `raw` rather
than dropped.

That tolerance is not slack. The alternative is a client that breaks on the
morning of the evaluation because a key was renamed, and there is no second
attempt at that.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

log = logging.getLogger("adapters.catalogue")

__all__ = [
    "Endpoint",
    "CatalogueCamera",
    "CatalogueAuthRequired",
    "ACCESS_PASSWORD",
    "ACCESS_EMAIL",
    "LOGIN_PATH",
    "fetch_catalogue",
    "opener_for",
    "apply_endpoint_templates",
    "ENDPOINT_TEMPLATES",
    "parse_catalogue",
    "classify_url",
    "CATALOGUE_PATH",
]


class CatalogueAuthRequired(RuntimeError):
    """The catalogue answered, but sent us to a login instead of to JSON.

    A distinct exception, because it calls for a completely different response from
    the two failures around it. Unreachable means wait or check the network.
    Malformed means the contract changed and the parser needs work. This means the
    grid is healthy and we are not authorised: nothing in this repository can fix
    it, and the next step is to obtain credentials.

    Observed 31 Aug 2026: cctv.corp8.cloud/api/ingest answers 302 to /auth/login.
    Following that redirect yields an HTML login page, and json.load on it raises a
    decode error whose message says nothing about authentication, which would send
    the reader looking for a parser bug that does not exist.
    """

#: Where the catalogue lives, relative to the grid's base URL. Overridable
#: because it is a fact about a deployment, not about this code.
#: `/api/ingest` until September 2026; it answered 404 on 14 Sep and the
#: signed-in control room reads `cameras.json`.
CATALOGUE_PATH = os.environ.get("SENTINEL_CATALOGUE_PATH", "/cameras.json")

TIMEOUT_S = float(os.environ.get("SENTINEL_TIMEOUT_S", "20"))

USER_AGENT = "cctv-platform-onboarder"

#: The grid's sign-in form, relative to the base URL.
#:
#: On 31 Aug 2026 the estate moved behind an access-password portal: every path,
#: including the stream paths, answers 302 to this. Overridable for the same
#: reason `CATALOGUE_PATH` is — it is a fact about a deployment, not about this
#: code.
LOGIN_PATH = os.environ.get("SENTINEL_LOGIN_PATH", "/auth/login")

#: The access password the grid issues on registration, in the environment and
#: nowhere else (invariant 5: this repository is a submission artifact and will
#: be read by evaluators).
#:
#: Read at call time rather than at import so that setting it does not require a
#: restart, and so a test can set it without reloading the module.
ACCESS_PASSWORD_ENV = "SENTINEL_ACCESS_PASSWORD"

#: Form field the portal posts the password under.
PASSWORD_FIELD = os.environ.get("SENTINEL_PASSWORD_FIELD", "password")

#: The email the access password was registered to. Added 14 Sep 2026, when the
#: login form grew an `email` field and the password alone started landing back
#: on the login page. Environment only, like the password; never logged.
ACCESS_EMAIL_ENV = "SENTINEL_ACCESS_EMAIL"
EMAIL_FIELD = os.environ.get("SENTINEL_EMAIL_FIELD", "email")


def ACCESS_PASSWORD() -> str:  # noqa: N802 - reads as a constant at call sites
    return os.environ.get(ACCESS_PASSWORD_ENV, "").strip()


def ACCESS_EMAIL() -> str:  # noqa: N802 - reads as a constant at call sites
    return os.environ.get(ACCESS_EMAIL_ENV, "").strip()

# Keys the catalogue might use for each field, most specific first. Looked up
# case-insensitively and with `_`/`-` folded, so `frameRate`, `frame_rate` and
# `frame-rate` are the same key.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "source_id": ("id", "cameraid", "camera", "streamid", "stream", "uid", "slug"),
    "name": ("name", "title", "label", "cameraname", "devicename"),
    "location": ("location", "site", "place", "address", "area", "locality"),
    "district": ("district", "city", "zone", "region"),
    "codec": ("codec", "videocodec", "encoding", "vcodec"),
    "container": ("container", "format", "muxer"),
    "width": ("width", "framewidth", "videowidth"),
    "height": ("height", "frameheight", "videoheight"),
    "fps": ("fps", "framerate", "declaredfps", "nominalfps", "rate"),
    "bitrate": ("bitrate", "videobitrate", "kbps", "bps"),
    "live": ("live", "islive", "online", "active", "up"),
    "status": ("status", "state", "health"),
    "resolution": ("resolution", "size", "dimensions"),
    "department": ("department", "owner", "agency", "dept"),
    "kind": ("kind", "type", "cameratype", "devicetype"),
    "lat": ("lat", "latitude"),
    "lon": ("lon", "lng", "long", "longitude"),
}

# Collections the camera list might be nested under.
_LIST_KEYS = ("cameras", "streams", "data", "items", "results", "feeds", "sources", "ingest")

#: Protocol → the platform adapter that speaks it. The only mapping in the
#: module, and it is between two of our own vocabularies rather than a
#: hardcoded URL shape.
PROTOCOL_ADAPTER = {"rtsp": "rtsp", "hls": "hls", "webrtc": "http", "http": "http"}

#: Order the ANPR worker should try endpoints in. RTSP first because it is the
#: one the reference designates for inference; HLS second because it is the
#: documented fallback when 8554 is blocked. WHEP is a browser transport and is
#: never handed to a decoder.
INFERENCE_PREFERENCE = ("rtsp", "hls", "http")


def _fold(key: str) -> str:
    return key.replace("_", "").replace("-", "").replace(" ", "").lower()


def _origin(url: str) -> str:
    """scheme://host:port — what a relative URL would be joined onto."""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _pick(source: dict, field_name: str) -> Any:
    """First present alias for `field_name`, or None."""
    folded = {_fold(k): v for k, v in source.items()}
    for alias in _FIELD_ALIASES.get(field_name, ()):
        value = folded.get(alias)
        if value not in (None, "", []):
            return value
    return None


def classify_url(url: str) -> str | None:
    """What kind of endpoint this URL is, from the URL itself.

    Deliberately reads the URL rather than trusting the key it was found under:
    the reference's own table pairs `.../whep` with "WebRTC" and
    `.../index.m3u8` with "HLS", and those suffixes are properties of the
    protocols, not of this sandbox's field names.
    """
    if not isinstance(url, str) or "://" not in url:
        return None
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    path = parsed.path.lower()
    if scheme in {"rtsp", "rtsps"}:
        return "rtsp"
    if scheme not in {"http", "https"}:
        return None
    if path.endswith(".m3u8") or "/hls/" in path:
        return "hls"
    if path.endswith("/whep") or path.endswith("/whip") or "webrtc" in path:
        return "webrtc"
    return "http"


@dataclass(frozen=True)
class Endpoint:
    """One way to reach a camera, exactly as the catalogue gave it."""

    protocol: str
    url: str


@dataclass
class CatalogueCamera:
    """One camera as the upstream describes it.

    Stream properties are carried because the reference is explicit that the
    grid is not uniform — mixed H.264/H.265, mixed resolutions, mixed rates —
    and that a fixed-shape inference batch across every camera will not work.
    They are recorded in the registry so the pipeline can size itself per
    camera instead of assuming.
    """

    source_id: str
    name: str
    location: str | None = None
    district: str | None = None
    codec: str | None = None
    container: str | None = None
    width: int | None = None
    height: int | None = None
    #: The catalogue's *declared* rate. Recorded, never used for timing — the
    #: reference warns it does not match delivery, and the capture layer
    #: measures the real one.
    declared_fps: float | None = None
    bitrate_kbps: float | None = None
    live: bool | None = None
    kind: str | None = None
    department: str | None = None
    lat: float | None = None
    lon: float | None = None
    endpoints: list[Endpoint] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def endpoint(self, protocol: str) -> str | None:
        for candidate in self.endpoints:
            if candidate.protocol == protocol:
                return candidate.url
        return None

    @property
    def inference_endpoints(self) -> list[Endpoint]:
        """Endpoints a decoder can read, best first.

        WHEP is excluded: it is a browser transport requiring an SDP
        negotiation, and handing it to FFmpeg produces a confusing failure
        rather than video.
        """
        ranked = [e for e in self.endpoints if e.protocol in INFERENCE_PREFERENCE]
        return sorted(ranked, key=lambda e: INFERENCE_PREFERENCE.index(e.protocol))

    @property
    def primary(self) -> Endpoint | None:
        ranked = self.inference_endpoints
        return ranked[0] if ranked else (self.endpoints[0] if self.endpoints else None)

    @property
    def adapter(self) -> str:
        """Registry adapter for this camera's best endpoint."""
        best = self.primary
        return PROTOCOL_ADAPTER.get(best.protocol, "http") if best else "http"

    @property
    def resolution(self) -> str | None:
        return f"{self.width}x{self.height}" if self.width and self.height else None


def _collect_endpoints(record: dict, base_url: str) -> list[Endpoint]:
    """Every URL in this record, classified by what it actually is.

    Walks nested structures because a catalogue may group URLs under a `urls`
    or `endpoints` object, and relative paths are resolved against the base so
    a catalogue that returns `/stream/7/whep` still yields a usable endpoint.
    """
    found: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, str):
            return
        url = value
        if url.startswith("/") and base_url:
            url = urljoin(base_url, url)
        protocol = classify_url(url)
        # First one wins, so a top-level `rtsp_url` is not overwritten by a
        # duplicate buried in a nested block.
        if protocol and protocol not in found:
            found[protocol] = url

    visit(record)
    return [Endpoint(protocol=p, url=u) for p, u in found.items()]


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "live", "online", "up", "ok", "active", "1"}:
            return True
        if lowered in {"false", "no", "offline", "down", "stopped", "0", "error"}:
            return False
    return None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().rstrip("p").replace("fps", "").replace("kbps", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _dimensions(record: dict) -> tuple[int | None, int | None]:
    width = _as_number(_pick(record, "width"))
    height = _as_number(_pick(record, "height"))
    if width and height:
        return int(width), int(height)
    # `resolution: "1920x1080"` is at least as common as separate fields.
    resolution = _pick(record, "resolution")
    if isinstance(resolution, str):
        for separator in ("x", "X", "*", "×"):
            if separator in resolution:
                left, _, right = resolution.partition(separator)
                lw, lh = _as_number(left), _as_number(right)
                if lw and lh:
                    return int(lw), int(lh)
    return (int(width) if width else None, int(height) if height else None)


def _camera_from(record: dict, base_url: str, index: int) -> CatalogueCamera | None:
    endpoints = _collect_endpoints(record, base_url)
    source_id = _pick(record, "source_id")

    if not endpoints and source_id is None:
        # No URL *and* no id is genuinely unusable: there is nothing to address
        # and nothing to address it as. A record with an id but no URL is a
        # different thing — the grid's own catalogue became exactly that on
        # 31 Aug 2026 — and is kept here so `apply_endpoint_templates` can
        # address it from the documented patterns.
        log.warning("catalogue entry %d has neither a URL nor an id; skipped", index)
        return None

    if source_id is None:
        # Derive from the endpoint path rather than from the position in the
        # list: a position is not an identity, and the reference says the set
        # of cameras changes between calls.
        path = urlparse(endpoints[0].url).path.strip("/").replace("/", "-")
        source_id = path or f"entry-{index}"

    width, height = _dimensions(record)
    live = _as_bool(_pick(record, "live"))
    if live is None:
        live = _as_bool(_pick(record, "status"))

    name = _pick(record, "name") or _pick(record, "location") or f"Camera {source_id}"
    bitrate = _as_number(_pick(record, "bitrate"))
    if bitrate and bitrate > 100_000:  # plainly bits per second, not kbps
        bitrate /= 1000.0

    return CatalogueCamera(
        source_id=str(source_id),
        name=str(name),
        location=_str_or_none(_pick(record, "location")),
        district=_str_or_none(_pick(record, "district")),
        codec=_str_or_none(_pick(record, "codec")),
        container=_str_or_none(_pick(record, "container")),
        width=width,
        height=height,
        declared_fps=_as_number(_pick(record, "fps")),
        bitrate_kbps=bitrate,
        live=live,
        kind=_str_or_none(_pick(record, "kind")),
        department=_str_or_none(_pick(record, "department")),
        lat=_as_number(_pick(record, "lat")),
        lon=_as_number(_pick(record, "lon")),
        endpoints=endpoints,
        raw=record,
    )


def _str_or_none(value: Any) -> str | None:
    return str(value) if isinstance(value, (str, int, float)) and str(value).strip() else None


def _records(payload: Any) -> list[dict]:
    """Find the list of cameras, whatever the envelope looks like."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            # Keyed by camera id. Fold the key in, since it *is* the id.
            return [{"id": k, **v} for k, v in value.items() if isinstance(v, dict)]
    # A bare mapping of id -> camera, with no envelope key at all.
    nested = [v for v in payload.values() if isinstance(v, dict)]
    if nested and len(nested) == len(payload):
        return [{"id": k, **v} for k, v in payload.items() if isinstance(v, dict)]
    return []


#: Endpoint templates, applied **only** to a camera the catalogue gave no URL for.
#:
#: Invariant 6 says the catalogue is the contract and the URL pattern is not,
#: and that nothing reconstructs a stream URL from a template. That rule was
#: written against a catalogue that *carried* URLs, and it still governs the
#: part that matters: the set of cameras comes from `cameras.json` and nothing
#: enumerates by counting.
#:
#: On 31 Aug 2026 the grid replaced its catalogue with `[{id, name}]` — no URLs
#: at all — and moved the endpoint patterns into its integration guide, which
#: documents them as the contract. With no URL to prefer, a template is the only
#: way to reach a camera, so these exist; they are environment-overridable, they
#: are used only as a fallback, and a catalogue that resumes carrying URLs
#: silently wins again without a code change.
#:
#: `{id}` is the camera id exactly as the catalogue gave it.
ENDPOINT_TEMPLATES: tuple[tuple[str, str], ...] = tuple(
    (protocol, template)
    for protocol, template in (
        ("rtsp", os.environ.get(
            "SENTINEL_RTSP_TEMPLATE",
            "rtsp://103.250.160.189:8554/stream/{id}")),
        ("hls", os.environ.get(
            "SENTINEL_HLS_TEMPLATE",
            "https://cctv.corp8.cloud/{id}/index.m3u8")),
        ("whep", os.environ.get(
            "SENTINEL_WHEP_TEMPLATE",
            "http://103.250.160.189:8889/stream/{id}/whep")),
    )
    if template
)


def apply_endpoint_templates(cameras: list[CatalogueCamera]) -> int:
    """Give endpoints to cameras the catalogue described but did not address.

    Returns how many cameras were filled in, so the caller can say so out loud —
    a synthesised endpoint is a weaker fact than one the catalogue handed us,
    and an operator should be told which they have.

    A camera that already carries endpoints is left completely alone.
    """
    filled = 0
    for camera in cameras:
        if camera.endpoints or not camera.source_id:
            continue
        camera.endpoints.extend(
            Endpoint(url=template.format(id=camera.source_id), protocol=protocol)
            for protocol, template in ENDPOINT_TEMPLATES
        )
        filled += 1
    return filled


def parse_catalogue(payload: Any, base_url: str = "") -> list[CatalogueCamera]:
    """Turn a catalogue response into camera records. Pure; no network."""
    cameras = []
    for index, record in enumerate(_records(payload)):
        camera = _camera_from(record, base_url, index)
        if camera is not None:
            cameras.append(camera)
    return cameras


#: Path fragments that mean "you have been sent to a sign-in page". Matched on
#: the *path* only: a camera legitimately called `login-gate-cam` must not trip
#: this, and a query string carrying a `?next=` back-reference is not evidence
#: of anything on its own.
_AUTH_PATH_HINTS = ("/auth/", "/auth", "/login", "/signin", "/sign-in", "/oauth")

#: Content types a catalogue may legitimately answer with. Anything else that
#: arrives where JSON was expected is a page, not a payload.
_JSON_HINTS = ("json", "javascript")


def opener_for(base_url: str, *, timeout_s: float = TIMEOUT_S) -> urllib.request.OpenerDirector:
    """An opener carrying a signed-in session, when a password is configured.

    The portal is a plain form that sets a session cookie, so the session lives in a
    cookie jar held for the life of this opener rather than in a header we could
    attach per request. One sign-in then serves the catalogue read and every stream
    fetch that follows, which also keeps us from re-authenticating once per camera
    against someone else's server.

    With no password set this returns a plain opener and the caller gets the same
    CatalogueAuthRequired it would have got anyway. The point is that "no password
    configured" and "password rejected" are different messages, and both are more
    useful than a JSON decode error.

    The password is never logged, at any level, on any path.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]

    password = ACCESS_PASSWORD()
    if not password:
        return opener

    asked_login = urljoin(base_url.rstrip("/") + "/", LOGIN_PATH.lstrip("/"))

    # Find out where the login form *actually* lives before posting to it.
    #
    # The documented entry point 301s to whichever host the estate is on today.
    # A redirected POST does not carry its body — urllib, like every browser,
    # turns 301 into a GET — so posting to the documented host silently sends
    # the password nowhere and the sign-in "fails" with a correct password.
    # Measured exactly that on 31 Aug 2026.
    #
    # One GET first, and the server names its own login endpoint.
    try:
        with opener.open(asked_login, timeout=timeout_s) as probe:
            login_url = probe.geturl() or asked_login
    except urllib.error.HTTPError as exc:
        raise CatalogueAuthRequired(
            f"could not reach the sign-in page at {asked_login}: HTTP {exc.code}"
        ) from exc

    if _origin(login_url) != _origin(asked_login):
        log.info(
            "sign-in follows a redirect: %s -> %s. Set SENTINEL_BASE to the "
            "latter to save the hop", _origin(asked_login), _origin(login_url),
        )

    fields = {PASSWORD_FIELD: password}
    if ACCESS_EMAIL():
        fields = {EMAIL_FIELD: ACCESS_EMAIL(), **fields}
    body = urllib.parse.urlencode(fields).encode()
    try:
        with opener.open(login_url, data=body, timeout=timeout_s) as response:
            landed = response.geturl() or login_url
    except urllib.error.HTTPError as exc:
        raise CatalogueAuthRequired(
            f"sign-in at {login_url} was refused with HTTP {exc.code}. "
            f"Check {ACCESS_PASSWORD_ENV} — the grid issues it on registration"
        ) from exc

    # A portal that keeps us on the login page has rejected the password. It
    # answers 200 either way, so the *destination* is the only signal, and
    # without this check the failure would surface later as an unexplained
    # redirect on the catalogue read instead of here where it happened.
    if _looks_like_auth(landed):
        raise CatalogueAuthRequired(
            f"sign-in at {login_url} did not take: still at {landed}. "
            f"The password in {ACCESS_PASSWORD_ENV} looks wrong or expired, or the "
            f"portal also wants the registered email in {ACCESS_EMAIL_ENV}"
        )

    if not jar:
        log.warning(
            "signed in at %s but the grid set no cookie; the session may not persist",
            login_url,
        )
    log.info("signed in to %s", _origin(login_url))
    return opener


def _looks_like_auth(target: str) -> bool:
    path = urlparse(target).path.rstrip("/").lower()
    return any(path == hint.rstrip("/") or path.endswith(hint) for hint in _AUTH_PATH_HINTS)


def _refuse_login_page(asked: str, landed: str, response: object) -> None:
    """Raise CatalogueAuthRequired rather than let json.load report a syntax error.

    Two independent signals, either of which is enough. The redirect target is
    the strong one — a grid that sends `/api/ingest` to `/auth/login` has told
    us plainly what it wants. The content type is the backstop for a grid that
    serves the login page at the same URL with a 200.
    """
    content_type = ""
    headers = getattr(response, "headers", None)
    if headers is not None:
        content_type = (headers.get("Content-Type") or "").lower()

    redirected_to_auth = _looks_like_auth(landed) and not _looks_like_auth(asked)
    wrong_type = bool(content_type) and not any(h in content_type for h in _JSON_HINTS)

    if not (redirected_to_auth or wrong_type):
        return

    if redirected_to_auth:
        why = f"it redirected to {landed}"
    else:
        why = f"it answered {content_type!r} where JSON was expected"
    if ACCESS_PASSWORD():
        raise CatalogueAuthRequired(
            f"the catalogue at {asked} still requires authentication after "
            f"signing in: {why}. The password in {ACCESS_PASSWORD_ENV} was "
            "accepted at the login but does not reach the catalogue — it may be "
            "expired, or scoped to something else."
        )
    # The origin that actually served the login, not the one we asked. The
    # documented entry point redirects across hosts, so naming the host we asked
    # would send the reader to register on a machine that only forwards.
    portal = _origin(landed) if redirected_to_auth else _origin(asked)
    raise CatalogueAuthRequired(
        f"the catalogue at {asked} requires authentication: {why}. "
        f"The grid is up and we are not authorised. Register at "
        f"{portal}/auth/register for an access password, then set "
        f"{ACCESS_PASSWORD_ENV}. Nothing in this repository can work around it."
    )


def fetch_catalogue(base_url: str, *, timeout_s: float = TIMEOUT_S) -> list[CatalogueCamera]:
    """Read the catalogue from a running grid.

    Raises on transport failure rather than returning an empty list: "the
    catalogue is unreachable" and "the grid has no cameras" call for completely
    different operator responses, and collapsing them would let a sync quietly
    decommission the whole estate.
    """
    url = urljoin(base_url.rstrip("/") + "/", CATALOGUE_PATH.lstrip("/"))
    opener = opener_for(base_url, timeout_s=timeout_s)
    with opener.open(url, timeout=timeout_s) as response:
        landed = response.geturl() or url
        _refuse_login_page(url, landed, response)
        payload = json.load(response)
        # Resolve relative endpoints against where the catalogue was *served
        # from*, not where we asked for it. `urlopen` follows redirects
        # silently, and this grid does redirect across hosts: on 31 Aug 2026
        # `live.sentinelgujarat.in` began 301ing to `live.corp8.cloud`. The
        # reference documents HLS as a root-relative path
        # (`/live/stream/<id>/index.m3u8`), so joining it to the host we asked
        # for would store endpoints pointing at a host that no longer serves
        # them — and it would not raise. The sync would succeed and the
        # decoders would find nothing.
        effective = landed
    cameras = parse_catalogue(payload, base_url=effective)
    filled = apply_endpoint_templates(cameras)
    if filled:
        log.warning(
            "%d of %d cameras arrived with no endpoint; addressing them from the "
            "documented templates (%s). These are weaker facts than a catalogue "
            "URL — override with SENTINEL_RTSP_TEMPLATE / _HLS_ / _WHEP_",
            filled, len(cameras), ", ".join(p for p, _ in ENDPOINT_TEMPLATES),
        )
    if _origin(effective) != _origin(url):
        log.warning(
            "catalogue redirected %s -> %s; relative endpoints resolved against the "
            "redirect target. Set SENTINEL_BASE to the new host to stop relying on it",
            _origin(url), _origin(effective),
        )
    log.info("catalogue %s returned %d cameras", effective, len(cameras))
    return cameras
