"""The catalogue is the contract; the URL pattern is not.

These tests are written against that sentence from the integration reference,
because the failure mode they guard is the expensive one: an onboarder that
assumes a shape works perfectly right up until the organisers change something,
and then onboards the wrong estate while reporting success.
"""

from __future__ import annotations

import pytest

from services.adapters.catalogue import (
    CatalogueCamera,
    Endpoint,
    classify_url,
    parse_catalogue,
)

BASE = "https://live.sentinelgujarat.in"

#: The shape the reference describes: id, location, codec, live status, stream
#: properties, and all three URLs.
DOCUMENTED = {
    "cameras": [
        {
            "id": "7",
            "name": "08 Majevadi Gate PTZ-2",
            "location": "Majevadi Gate, Junagadh",
            "codec": "h265",
            "container": "mp4",
            "live": True,
            "width": 1280,
            "height": 720,
            "fps": 25,
            "bitrate_kbps": 2048,
            "rtsp_url": "rtsp://live.sentinelgujarat.in:8554/stream/7",
            "webrtc_url": "http://live.sentinelgujarat.in:8889/stream/7/whep",
            "hls_url": "http://live.sentinelgujarat.in/live/stream/7/index.m3u8",
        }
    ]
}


class TestUrlClassification:
    def test_it_reads_the_url_not_the_field_name(self) -> None:
        assert classify_url("rtsp://h:8554/stream/1") == "rtsp"
        assert classify_url("http://h/live/stream/1/index.m3u8") == "hls"
        assert classify_url("http://h:8889/stream/1/whep") == "webrtc"
        assert classify_url("http://h/stream/1") == "http"

    def test_non_urls_are_not_endpoints(self) -> None:
        assert classify_url("Majevadi Gate") is None
        assert classify_url("") is None
        assert classify_url(None) is None  # type: ignore[arg-type]


class TestDocumentedShape:
    def test_all_three_transports_are_captured(self) -> None:
        camera = parse_catalogue(DOCUMENTED, BASE)[0]
        assert {e.protocol for e in camera.endpoints} == {"rtsp", "webrtc", "hls"}

    def test_stream_properties_come_through(self) -> None:
        """The grid is not uniform, and the pipeline sizes itself from these."""
        camera = parse_catalogue(DOCUMENTED, BASE)[0]
        assert camera.codec == "h265"
        assert (camera.width, camera.height) == (1280, 720)
        assert camera.declared_fps == 25.0
        assert camera.live is True

    def test_whep_is_never_offered_to_a_decoder(self) -> None:
        """It needs an SDP negotiation; handing it to FFmpeg fails confusingly."""
        camera = parse_catalogue(DOCUMENTED, BASE)[0]
        assert "webrtc" not in {e.protocol for e in camera.inference_endpoints}

    def test_rtsp_is_preferred_for_inference(self) -> None:
        camera = parse_catalogue(DOCUMENTED, BASE)[0]
        assert camera.primary.protocol == "rtsp"
        assert camera.adapter == "rtsp"


class TestShapeTolerance:
    """The sandbox has already changed shape once. Field names are discovered,
    not asserted, because there is no second attempt on evaluation day."""

    def test_a_bare_list_works(self) -> None:
        payload = [{"id": "1", "rtsp_url": "rtsp://h/1"}]
        assert len(parse_catalogue(payload, BASE)) == 1

    def test_an_envelope_under_any_plausible_key_works(self) -> None:
        for key in ("cameras", "streams", "data", "items", "results", "feeds"):
            payload = {key: [{"id": "1", "rtsp_url": "rtsp://h/1"}]}
            assert len(parse_catalogue(payload, BASE)) == 1, key

    def test_a_mapping_keyed_by_camera_id_works(self) -> None:
        payload = {"9": {"rtsp_url": "rtsp://h/9"}, "10": {"rtsp_url": "rtsp://h/10"}}
        cameras = parse_catalogue(payload, BASE)
        assert {c.source_id for c in cameras} == {"9", "10"}

    def test_camelcase_and_kebab_field_names_are_the_same_field(self) -> None:
        payload = [{"cameraId": "3", "frameRate": 30, "rtsp_url": "rtsp://h/3"}]
        camera = parse_catalogue(payload, BASE)[0]
        assert camera.source_id == "3"
        assert camera.declared_fps == 30.0

    def test_urls_nested_in_a_block_are_still_found(self) -> None:
        payload = [{"id": "4", "urls": {"rtsp": "rtsp://h/4", "hls": "http://h/4.m3u8"}}]
        camera = parse_catalogue(payload, BASE)[0]
        assert {e.protocol for e in camera.endpoints} == {"rtsp", "hls"}

    def test_relative_paths_are_resolved_against_the_base(self) -> None:
        payload = [{"id": "5", "hls_url": "/live/stream/5/index.m3u8"}]
        camera = parse_catalogue(payload, BASE)[0]
        assert camera.endpoint("hls") == f"{BASE}/live/stream/5/index.m3u8"

    def test_a_resolution_string_is_parsed(self) -> None:
        payload = [{"id": "6", "resolution": "1920x1080", "rtsp_url": "rtsp://h/6"}]
        camera = parse_catalogue(payload, BASE)[0]
        assert (camera.width, camera.height) == (1920, 1080)

    def test_live_status_as_a_word_is_understood(self) -> None:
        assert parse_catalogue([{"id": "1", "status": "online", "rtsp_url": "rtsp://h/1"}],
                               BASE)[0].live is True
        assert parse_catalogue([{"id": "2", "status": "offline", "rtsp_url": "rtsp://h/2"}],
                               BASE)[0].live is False

    def test_bitrate_in_bits_per_second_is_normalised(self) -> None:
        payload = [{"id": "1", "bitrate": 4_000_000, "rtsp_url": "rtsp://h/1"}]
        assert parse_catalogue(payload, BASE)[0].bitrate_kbps == 4000.0

    def test_an_entry_with_neither_a_url_nor_an_id_is_skipped(self) -> None:
        """Nothing to address, and nothing to address it as."""
        assert parse_catalogue([{"location": "somewhere"}], BASE) == []

    def test_parsing_alone_never_invents_a_url(self) -> None:
        """`parse_catalogue` is pure and stays that way.

        An entry with an id but no URL is *kept* — the grid's own catalogue
        became exactly that shape on 31 Aug 2026 — but parsing does not address
        it. Filling in an endpoint is a separate, explicit step
        (`apply_endpoint_templates`) that the caller opts into and that logs
        loudly when it fires, so a synthesised URL can never be mistaken for one
        the catalogue supplied.
        """
        cameras = parse_catalogue([{"id": "cam01", "name": "Chimanbhai Bridge"}], BASE)
        assert [c.source_id for c in cameras] == ["cam01"]
        assert cameras[0].endpoints == []

    def test_an_id_can_be_recovered_from_the_endpoint_when_absent(self) -> None:
        """Never from the position in the list: a position is not an identity,
        and the reference says the set of cameras changes between calls."""
        payload = [{"rtsp_url": "rtsp://h:8554/stream/12"}]
        assert parse_catalogue(payload, BASE)[0].source_id == "stream-12"

    def test_unknown_extra_fields_are_carried_not_dropped(self) -> None:
        payload = [{"id": "1", "rtsp_url": "rtsp://h/1", "future_field": "value"}]
        assert parse_catalogue(payload, BASE)[0].raw["future_field"] == "value"


class TestAdapterSelection:
    def test_an_hls_only_camera_uses_the_hls_adapter(self) -> None:
        camera = parse_catalogue([{"id": "1", "hls_url": "http://h/1.m3u8"}], BASE)[0]
        assert camera.adapter == "hls"

    def test_a_progressive_http_camera_uses_the_http_adapter(self) -> None:
        camera = parse_catalogue([{"id": "1", "url": "http://h/stream/1"}], BASE)[0]
        assert camera.adapter == "http"

    def test_inference_endpoints_are_ordered_rtsp_then_hls(self) -> None:
        camera = CatalogueCamera(
            source_id="1",
            name="c",
            endpoints=[
                Endpoint("hls", "http://h/1.m3u8"),
                Endpoint("rtsp", "rtsp://h/1"),
            ],
        )
        assert [e.protocol for e in camera.inference_endpoints] == ["rtsp", "hls"]


class _FakeHeaders(dict):
    """Case-insensitive `.get`, the one thing the code asks of a headers object."""

    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _FakeOpener:
    """Stands in for the signed-in opener `fetch_catalogue` builds.

    Patched at `opener_for` rather than at `urlopen`, because that is now the
    seam: the client signs in first and reads the catalogue through the session
    that produced. Faking the transport underneath would test a path the code no
    longer takes.
    """

    def __init__(self, body: str, served: str, content_type: str = "") -> None:
        self._body, self._served, self._type = body, served, content_type

    def open(self, url, data=None, timeout=None):  # noqa: A003, ARG002
        import io

        response = io.StringIO(self._body)
        response.geturl = lambda: self._served  # type: ignore[attr-defined]
        response.headers = _FakeHeaders(  # type: ignore[attr-defined]
            {"content-type": self._type} if self._type else {}
        )

        class _Ctx:
            def __enter__(self_inner):
                return response

            def __exit__(self_inner, *exc):
                return False

        return _Ctx()


class TestRedirectFollowing:
    """The grid moved host on 31 Aug 2026 and `urlopen` follows redirects silently.

    A relative endpoint must be resolved against the host that actually served
    the catalogue, not the host we asked. Getting this wrong does not raise —
    it onboards cleanly and stores endpoints pointing at a host that has stopped
    serving them, which is the worst kind of failure to find on evaluation day.
    """

    @staticmethod
    def _fetch(monkeypatch, *, asked: str, served: str, payload: dict):
        import json as _json

        from services.adapters import catalogue as cat

        monkeypatch.setattr(
            cat, "opener_for",
            lambda base, timeout_s=None: _FakeOpener(_json.dumps(payload), served),
        )
        return cat.fetch_catalogue(asked)

    def test_relative_endpoints_resolve_against_the_redirect_target(
        self, monkeypatch
    ) -> None:
        camera = self._fetch(
            monkeypatch,
            asked="https://live.sentinelgujarat.in",
            served="https://live.corp8.cloud/api/ingest",
            payload={"cameras": [{"id": "7", "hls_url": "/live/stream/7/index.m3u8"}]},
        )[0]
        hls = next(e for e in camera.endpoints if e.protocol == "hls")
        assert hls.url == "https://live.corp8.cloud/live/stream/7/index.m3u8"

    def test_absolute_endpoints_are_untouched_by_a_redirect(self, monkeypatch) -> None:
        camera = self._fetch(
            monkeypatch,
            asked="https://live.sentinelgujarat.in",
            served="https://live.corp8.cloud/api/ingest",
            payload={"cameras": [{"id": "7", "rtsp_url": "rtsp://elsewhere:8554/stream/7"}]},
        )[0]
        assert camera.endpoints[0].url == "rtsp://elsewhere:8554/stream/7"

    def test_a_redirect_across_hosts_is_logged_as_a_warning(
        self, monkeypatch, caplog
    ) -> None:
        with caplog.at_level("WARNING"):
            self._fetch(
                monkeypatch,
                asked="https://live.sentinelgujarat.in",
                served="https://live.corp8.cloud/api/ingest",
                payload={"cameras": [{"id": "7", "rtsp_url": "rtsp://h/7"}]},
            )
        assert "redirected" in caplog.text
        assert "live.corp8.cloud" in caplog.text

    def test_no_warning_when_the_host_did_not_change(self, monkeypatch, caplog) -> None:
        with caplog.at_level("WARNING"):
            self._fetch(
                monkeypatch,
                asked="https://live.sentinelgujarat.in",
                served="https://live.sentinelgujarat.in/api/ingest",
                payload={"cameras": [{"id": "7", "rtsp_url": "rtsp://h/7"}]},
            )
        assert "redirected" not in caplog.text


class TestAuthRequired:
    """31 Aug 2026, second move: the grid came back behind a login.

    `cctv.corp8.cloud/api/ingest` answers 302 to `/auth/login`. `urlopen`
    follows it, `json.load` meets HTML, and the resulting decode error says
    nothing at all about authentication — it reads as a parser bug in this
    repository, which is the wrong place to go looking. The distinction matters
    operationally too: "unreachable" means wait, "malformed" means fix the
    parser, and "unauthorised" means go and get credentials.
    """

    @staticmethod
    def _fetch(monkeypatch, *, asked: str, served: str, body: str, content_type: str = ""):
        from services.adapters import catalogue as cat

        monkeypatch.setattr(
            cat, "opener_for",
            lambda base, timeout_s=None: _FakeOpener(body, served, content_type),
        )
        return cat.fetch_catalogue(asked)

    def test_a_redirect_to_a_login_page_raises_auth_required(self, monkeypatch) -> None:
        from services.adapters.catalogue import CatalogueAuthRequired

        with pytest.raises(CatalogueAuthRequired) as caught:
            self._fetch(
                monkeypatch,
                asked="https://live.sentinelgujarat.in",
                served="https://cctv.corp8.cloud/auth/login",
                body="<!doctype html><title>Sign in</title>",
                content_type="text/html; charset=utf-8",
            )
        assert "requires authentication" in str(caught.value)
        assert "cctv.corp8.cloud/auth/login" in str(caught.value)

    def test_html_at_the_same_url_also_raises(self, monkeypatch) -> None:
        """A grid that serves the login page in place, with a 200 and no redirect."""
        from services.adapters.catalogue import CatalogueAuthRequired

        with pytest.raises(CatalogueAuthRequired):
            self._fetch(
                monkeypatch,
                asked="https://grid.example",
                served="https://grid.example/api/ingest",
                body="<!doctype html><title>Sign in</title>",
                content_type="text/html",
            )

    def test_json_is_not_mistaken_for_a_login_page(self, monkeypatch) -> None:
        cameras = self._fetch(
            monkeypatch,
            asked="https://grid.example",
            served="https://grid.example/api/ingest",
            body='{"cameras": [{"id": "3", "rtsp_url": "rtsp://h:8554/3"}]}',
            content_type="application/json",
        )
        assert [c.source_id for c in cameras] == ["3"]

    def test_a_catalogue_with_no_content_type_still_parses(self, monkeypatch) -> None:
        """The header is a backstop, not a requirement. Absent must not mean refused."""
        cameras = self._fetch(
            monkeypatch,
            asked="https://grid.example",
            served="https://grid.example/api/ingest",
            body='{"cameras": [{"id": "4", "rtsp_url": "rtsp://h:8554/4"}]}',
        )
        assert [c.source_id for c in cameras] == ["4"]

    def test_a_camera_path_that_merely_contains_login_is_not_an_auth_redirect(
        self, monkeypatch
    ) -> None:
        """`_looks_like_auth` matches path segments, not substrings.

        A grid whose catalogue lives under, say, `/api/login-gate/ingest` is
        unusual but not signing us in, and refusing it would be a false alarm
        that blocks a working onboard.
        """
        cameras = self._fetch(
            monkeypatch,
            asked="https://grid.example",
            served="https://grid.example/api/login-gate-cam/ingest",
            body='{"cameras": [{"id": "5", "rtsp_url": "rtsp://h:8554/5"}]}',
            content_type="application/json",
        )
        assert [c.source_id for c in cameras] == ["5"]

    def test_a_catalogue_configured_under_an_auth_path_is_not_refused(
        self, monkeypatch
    ) -> None:
        """The redirect signal is a *change* of destination, not a destination.

        A deployment free to set `SENTINEL_CATALOGUE_PATH` may legitimately put
        the catalogue somewhere that matches the hints. With no redirect, the
        asked and served URLs agree, and only the content type gets a vote —
        otherwise the guard would refuse a grid that was answering correctly.
        """
        from services.adapters import catalogue as cat

        monkeypatch.setattr(cat, "CATALOGUE_PATH", "/auth/ingest")
        cameras = self._fetch(
            monkeypatch,
            asked="https://grid.example",
            served="https://grid.example/auth/ingest",
            body='{"cameras": [{"id": "6", "rtsp_url": "rtsp://h:8554/6"}]}',
            content_type="application/json",
        )
        assert [c.source_id for c in cameras] == ["6"]


class TestAccessPasswordSignIn:
    """31 Aug 2026, third move: the whole grid went behind an access-password portal.

    Every path — the catalogue *and* every stream path — answers 302 to
    `/auth/login`, and the portal issues a password on registration. So the
    client signs in first and reads the catalogue through that session.

    The password lives in the environment and nowhere else (invariant 5: this
    repository is a submission artifact and will be read by evaluators), and is
    never logged at any level.
    """

    @staticmethod
    def _opener(
        monkeypatch, *, lands_at: str, probe_lands_at: str | None = None,
        sets_cookie: bool = True, raises=None,
    ):
        """Capture what the sign-in posts, and control where each step lands.

        Two steps, and they must be distinguishable. The client first GETs the login
        path to discover where the form actually lives, because the entry point
        redirects across hosts and a redirected POST silently drops its body, and only
        then POSTs the password there. A fake that answers both the same way cannot
        catch the client posting to the wrong place, which is the bug this whole flow
        exists to avoid.
        """
        import http.cookiejar

        from services.adapters import catalogue as cat

        seen: dict = {}

        class _Recorder:
            addheaders: list = []

            def open(self, url, data=None, timeout=None):  # noqa: A003, ARG002
                if data is None:  # the discovery GET
                    seen["probe_url"] = url
                    landed = probe_lands_at or url
                else:
                    seen["url"] = url
                    seen["data"] = data
                    landed = lands_at
                if raises is not None and data is not None:
                    raise raises

                class _Ctx:
                    def __enter__(self_inner):
                        class _R:
                            @staticmethod
                            def geturl():
                                return landed

                        return _R()

                    def __exit__(self_inner, *exc):
                        return False

                return _Ctx()

        recorder = _Recorder()
        jar = http.cookiejar.CookieJar()
        if sets_cookie:
            jar.set_cookie(
                http.cookiejar.Cookie(
                    0, "session", "x", None, False, "cctv.example", False, False,
                    "/", True, False, None, True, None, None, {},
                )
            )
        monkeypatch.setattr(cat.http.cookiejar, "CookieJar", lambda: jar)
        monkeypatch.setattr(cat.urllib.request, "build_opener", lambda *a: recorder)
        return seen

    def test_no_password_configured_skips_the_sign_in_entirely(self, monkeypatch) -> None:
        """A plain opener, and the caller then gets the ordinary auth refusal.

        Posting an empty password would be a failed login attempt against
        someone else's server on every single run.
        """
        from services.adapters import catalogue as cat

        monkeypatch.delenv(cat.ACCESS_PASSWORD_ENV, raising=False)
        seen = self._opener(monkeypatch, lands_at="https://cctv.example/")
        cat.opener_for("https://cctv.example")
        assert "url" not in seen

    def test_it_posts_the_password_to_the_login_path(self, monkeypatch) -> None:
        from services.adapters import catalogue as cat

        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, "ABCD-EFGH-IJKL")
        seen = self._opener(monkeypatch, lands_at="https://cctv.example/dashboard")
        cat.opener_for("https://cctv.example")
        assert seen["probe_url"] == "https://cctv.example/auth/login"
        assert seen["url"] == "https://cctv.example/auth/login"
        assert b"password=ABCD-EFGH-IJKL" in seen["data"]

    def test_it_posts_the_registered_email_when_one_is_set(self, monkeypatch) -> None:
        """14 Sep 2026: the form grew an email field and a password alone bounced."""
        from services.adapters import catalogue as cat

        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, "ABCD-EFGH-IJKL")
        monkeypatch.setenv(cat.ACCESS_EMAIL_ENV, "someone@example.org")
        seen = self._opener(monkeypatch, lands_at="https://cctv.example/")
        cat.opener_for("https://cctv.example")
        assert b"email=someone%40example.org" in seen["data"]
        assert b"password=ABCD-EFGH-IJKL" in seen["data"]

    def test_without_an_email_only_the_password_is_posted(self, monkeypatch) -> None:
        from services.adapters import catalogue as cat

        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, "ABCD-EFGH-IJKL")
        monkeypatch.delenv(cat.ACCESS_EMAIL_ENV, raising=False)
        seen = self._opener(monkeypatch, lands_at="https://cctv.example/")
        cat.opener_for("https://cctv.example")
        assert b"email" not in seen["data"]

    def test_landing_back_on_the_login_page_means_the_password_was_rejected(
        self, monkeypatch
    ) -> None:
        """The portal answers 200 either way, so the destination is the only signal.

        Without this the failure would surface later as an unexplained redirect
        on the catalogue read, rather than here where it actually happened.
        """
        from services.adapters import catalogue as cat

        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, "WRONG")
        self._opener(monkeypatch, lands_at="https://cctv.example/auth/login")
        with pytest.raises(cat.CatalogueAuthRequired, match="wrong or expired"):
            cat.opener_for("https://cctv.example")

    def test_an_http_error_on_the_post_names_the_env_var(self, monkeypatch) -> None:
        """A rejected POST is a credential problem, so the message says which
        variable to look at."""
        import urllib.error

        from services.adapters import catalogue as cat

        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, "ABCD")
        self._opener(
            monkeypatch, lands_at="",
            raises=urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
        )
        with pytest.raises(cat.CatalogueAuthRequired) as caught:
            cat.opener_for("https://cctv.example")
        assert cat.ACCESS_PASSWORD_ENV in str(caught.value)
        assert "403" in str(caught.value)

    def test_the_password_is_posted_to_where_the_form_redirected_to(
        self, monkeypatch
    ) -> None:
        """The bug this exists to prevent, as a test.

        The documented entry point 301s to whichever host the estate is on. A
        redirected POST does not carry its body — urllib, like every browser,
        turns 301 into a GET — so posting to the documented host sends the
        password nowhere and a *correct* password reports as rejected. Measured
        exactly that on 31 Aug 2026.
        """
        from services.adapters import catalogue as cat

        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, "ABCD-EFGH-IJKL")
        seen = self._opener(
            monkeypatch,
            probe_lands_at="https://cctv.corp8.cloud/auth/login",  # 301 target
            lands_at="https://cctv.corp8.cloud/",
        )
        cat.opener_for("https://live.sentinelgujarat.in")
        assert seen["probe_url"] == "https://live.sentinelgujarat.in/auth/login"
        assert seen["url"] == "https://cctv.corp8.cloud/auth/login"

    def test_the_password_is_never_logged(self, monkeypatch, caplog) -> None:
        import logging

        from services.adapters import catalogue as cat

        secret = "SEKR-ETPA-SSWD"
        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, secret)
        self._opener(monkeypatch, lands_at="https://cctv.example/dashboard")
        with caplog.at_level(logging.DEBUG):
            cat.opener_for("https://cctv.example")
        assert secret not in caplog.text

    def test_a_grid_that_sets_no_cookie_warns_rather_than_failing(
        self, monkeypatch, caplog
    ) -> None:
        """The session might be carried some other way. Warn, do not refuse —
        refusing would block a grid that actually works."""
        import logging

        from services.adapters import catalogue as cat

        monkeypatch.setenv(cat.ACCESS_PASSWORD_ENV, "ABCD")
        self._opener(
            monkeypatch, lands_at="https://cctv.example/dashboard", sets_cookie=False
        )
        with caplog.at_level(logging.WARNING):
            cat.opener_for("https://cctv.example")
        assert "no cookie" in caplog.text

    def test_the_refusal_tells_you_where_to_register(self, monkeypatch) -> None:
        """With no password set, the message should say what to do next."""
        from services.adapters import catalogue as cat

        monkeypatch.delenv(cat.ACCESS_PASSWORD_ENV, raising=False)
        with pytest.raises(cat.CatalogueAuthRequired) as caught:
            cat._refuse_login_page(
                "https://cctv.corp8.cloud/api/ingest",
                "https://cctv.corp8.cloud/auth/login",
                None,
            )
        # The host that served the login, not the one we asked — the documented
        # entry point only forwards, and registering there would be a dead end.
        assert "https://cctv.corp8.cloud/auth/register" in str(caught.value)


class TestEndpointTemplates:
    """Addressing cameras the catalogue describes but does not address.

    Invariant 6 says the catalogue is the contract and the URL pattern is not,
    and that nothing reconstructs a stream URL from a template. That was written
    against a catalogue which *carried* URLs, and the part that matters is
    untouched: the set of cameras still comes from the catalogue and nothing
    enumerates by counting.

    On 31 Aug 2026 the grid replaced its catalogue with `[{id, name}]` — no URLs
    at all — and moved the endpoint patterns into its integration guide, which
    documents them as the contract. With no URL to prefer, a template is the
    only way to reach a camera. So templates exist, they are a *fallback only*,
    they are environment-overridable, and a catalogue that resumes carrying URLs
    silently wins again with no code change.
    """

    def test_a_camera_with_no_endpoints_is_addressed_from_the_templates(self) -> None:
        from services.adapters.catalogue import apply_endpoint_templates

        cameras = parse_catalogue([{"id": "cam07", "name": "Paldi"}], BASE)
        assert apply_endpoint_templates(cameras) == 1
        by_protocol = {e.protocol: e.url for e in cameras[0].endpoints}
        assert by_protocol["rtsp"].endswith("/stream/cam07")
        assert by_protocol["hls"].endswith("/cam07/index.m3u8")
        assert by_protocol["whep"].endswith("/stream/cam07/whep")

    def test_a_camera_the_catalogue_addressed_is_left_completely_alone(self) -> None:
        """The catalogue always wins. This is the half of invariant 6 that did
        not change, and the half that matters."""
        from services.adapters.catalogue import apply_endpoint_templates

        cameras = parse_catalogue(
            [{"id": "cam07", "rtsp_url": "rtsp://elsewhere:8554/real/path"}], BASE
        )
        assert apply_endpoint_templates(cameras) == 0
        assert [e.url for e in cameras[0].endpoints] == ["rtsp://elsewhere:8554/real/path"]

    def test_templates_are_overridable_from_the_environment(self, monkeypatch) -> None:
        """A deployment fact, not a constant. The grid moved hosts twice in one
        day; a template baked into the code would have been a code change."""
        import importlib

        monkeypatch.setenv("SENTINEL_RTSP_TEMPLATE", "rtsp://new-host:8554/{id}")
        monkeypatch.setenv("SENTINEL_HLS_TEMPLATE", "")
        monkeypatch.setenv("SENTINEL_WHEP_TEMPLATE", "")
        import services.adapters.catalogue as cat

        cat = importlib.reload(cat)
        try:
            cameras = cat.parse_catalogue([{"id": "cam09"}], BASE)
            cat.apply_endpoint_templates(cameras)
            assert [e.url for e in cameras[0].endpoints] == ["rtsp://new-host:8554/cam09"]
        finally:
            importlib.reload(cat)

    def test_a_camera_with_no_id_is_never_addressed(self) -> None:
        """`{id}` would interpolate to nothing and produce a URL pointing at the
        stream root — plausible-looking and wrong."""
        from services.adapters.catalogue import CatalogueCamera, apply_endpoint_templates

        camera = CatalogueCamera(source_id="", name="nameless")
        assert apply_endpoint_templates([camera]) == 0
        assert camera.endpoints == []
