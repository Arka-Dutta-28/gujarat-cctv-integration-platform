"""The credential sweep: what it finds, and what it must never print.

The repository is published, so "no credential in history" has to be a claim
somebody can re-run rather than a claim somebody made once. These tests fix the
two properties that make the sweep worth running: it looks at the whole object
database rather than the working tree, and it never puts a secret on screen.
"""

from __future__ import annotations

import logging
import pathlib

from scripts.credential_sweep import (
    MIN_SECRET_LENGTH,
    NOT_SECRETS,
    main,
    parse_env,
    scan_patterns,
    scan_values,
)


class TestPatterns:
    def test_a_private_key_header_is_found(self):
        hits = scan_patterns(b"-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
        assert [name for name, _ in hits] == ["private key"]

    def test_an_aws_key_is_found(self):
        assert scan_patterns(b'aws_key = "AKIAIOSFODNN7EXAMPLE"')

    def test_a_github_token_is_found(self):
        assert scan_patterns(b"ghp_" + b"a" * 36)

    def test_a_literal_password_assignment_is_found(self):
        hits = scan_patterns(b'password = "hunter2hunter2"')
        assert [name for name, _ in hits] == ["literal assignment"]

    def test_an_env_indirection_is_not_a_hit(self):
        # `AUTH_SECRET: "${AUTH_SECRET:?generate one}"` is the correct pattern,
        # and flagging it would train the reader to ignore the report.
        assert scan_patterns(b'AUTH_SECRET: "${AUTH_SECRET:?generate one}"') == []

    def test_a_commit_sha_is_not_a_hit(self):
        # Deliberately not "any long hex string": a sweep that fires on every
        # git sha, checksum and UUID is a sweep nobody runs twice.
        assert scan_patterns(b"parent 34f14ec757a44100f1ec1752c76c72e87261ae51") == []

    def test_the_matching_line_is_returned_for_judgement(self):
        # Pattern hits are not verdicts. The reader needs the line to tell a
        # leaked password from a test asserting one is never logged.
        _, line = scan_patterns(b'    secret = "SEKR-ETPA-SSWD"')[0]
        assert line == 'secret = "SEKR-ETPA-SSWD"'


class TestValues:
    def test_a_leaked_value_is_reported_by_name(self):
        leaked, _ = scan_values(b"...POSTGRES_PASSWORD=s3cret-value...",
                                {"POSTGRES_PASSWORD": "s3cret-value"})
        assert leaked == ["POSTGRES_PASSWORD"]

    def test_an_absent_value_is_clean(self):
        leaked, checked = scan_values(b"nothing here",
                                      {"POSTGRES_PASSWORD": "s3cret-value"})
        assert leaked == [] and checked == ["POSTGRES_PASSWORD"]

    def test_settings_are_not_treated_as_credentials(self):
        # A hostname in history is not a leak, and reporting it as one buries
        # the case that is.
        name = next(iter(NOT_SECRETS))
        leaked, checked = scan_values(b"anything", {name: "some-long-value"})
        assert leaked == [] and checked == []

    def test_short_values_are_skipped(self):
        leaked, checked = scan_values(b"true", {"SOMETHING": "true"})
        assert checked == [] and leaked == []
        assert len("true") < MIN_SECRET_LENGTH

    def test_the_value_pass_catches_what_no_pattern_could(self):
        # This is the whole reason both passes exist: `openssl rand -hex 32`
        # produces a value with no shape to match, indistinguishable from a
        # commit sha. Only knowing the actual value finds it.
        secret = "9f2c4e8a1b6d3f705c8e2a4b6d9f1c3e5a7b9d1f3c5e7a9b1d3f5c7e9a1b3d5f"
        assert scan_patterns(secret.encode()) == []
        leaked, _ = scan_values(secret.encode(), {"AUTH_SECRET": secret})
        assert leaked == ["AUTH_SECRET"]


class TestReporting:
    def test_a_leaked_value_is_never_printed(self, tmp_path, caplog, monkeypatch):
        """The one rule. A tool that finds a leak by printing it has widened it.

        The value would otherwise reach a terminal, a scrollback buffer and, if
        this is ever run in CI, a build log that outlives the rotation.

        Asserted against `caplog` rather than `capsys` because the report goes
        through `logging`, and `logging.basicConfig` is a no-op once pytest has
        configured the root logger — so stdout can be empty while the report is
        perfectly visible to whoever ran the command.
        """
        secret = "s3cret-value-that-must-not-appear"
        env = tmp_path / "env"
        env.write_text(f"POSTGRES_PASSWORD={secret}\n", encoding="utf-8")
        monkeypatch.setattr("scripts.credential_sweep.all_blobs",
                            lambda: f"blob {secret} blob".encode())
        with caplog.at_level(logging.INFO, logger="sweep"):
            assert main(["--env", str(env)]) == 1
        assert "POSTGRES_PASSWORD" in caplog.text
        assert secret not in caplog.text

    def test_a_clean_history_exits_zero(self, tmp_path, monkeypatch):
        env = tmp_path / "env"
        env.write_text("POSTGRES_PASSWORD=s3cret-value\n", encoding="utf-8")
        monkeypatch.setattr("scripts.credential_sweep.all_blobs", lambda: b"clean")
        assert main(["--env", str(env)]) == 0

    def test_pattern_hits_alone_do_not_fail_the_run(self, tmp_path, monkeypatch):
        # They need a human to judge them, and a check that fails on every test
        # fixture stops being read. Only a verbatim credential is a failure.
        env = tmp_path / "env"
        env.write_text("POSTGRES_PASSWORD=s3cret-value\n", encoding="utf-8")
        monkeypatch.setattr("scripts.credential_sweep.all_blobs",
                            lambda: b'password = "not-the-real-one"')
        assert main(["--env", str(env)]) == 0


class TestEnvParsing:
    def test_quotes_and_comments_are_stripped(self, tmp_path: pathlib.Path):
        path = tmp_path / "env"
        path.write_text('# a comment\nA="quoted"\nB=bare\n\n', encoding="utf-8")
        assert parse_env(path) == {"A": "quoted", "B": "bare"}

    def test_a_missing_file_is_empty_not_an_error(self, tmp_path: pathlib.Path):
        assert parse_env(tmp_path / "nope") == {}
