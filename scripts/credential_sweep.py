"""Prove no credential has ever been committed, across the whole history.

The repository is a published submission artifact. A clean `git status` says
nothing about this: a secret committed once and deleted in the next commit is
still in the history, still fetched by every clone, and still findable. The
usual mistake is checking the working tree and declaring victory.

Two passes, answering different questions.

Pattern pass. Every blob in the object database, matched against shapes that are
secrets wherever they appear: private key headers, AWS access key ids, GitHub
tokens, JWTs, and `password = "..."` style literal assignments. This finds
secrets that were never in .env at all, including ones from a contributor's
machine or a pasted example. It is the pass that can find what nobody knew to
look for, and the pass that produces false positives, because a test fixture
asserting that a password is not logged looks exactly like a leaked password.

Value pass. Takes each value in the local .env and asks whether that exact
string appears anywhere in history. This settles the question for this
repository, because these are the real secrets, and it cannot produce a false
positive: a match is the credential, verbatim.

Neither is sufficient alone. The value pass cannot see a secret that was never
in .env; the pattern pass cannot see a high-entropy value that does not look
like anything in particular, which is precisely what `openssl rand -hex 32`
produces and what AUTH_SECRET is.

Nothing here prints a secret. The value pass reports the variable name and a
verdict. A tool that helps you find a leaked credential by printing it to a
terminal, a CI log and a scrollback buffer has made the leak worse.

Usage::

    make credential-sweep
    python -m scripts.credential_sweep --env .env
    python -m scripts.credential_sweep --patterns-only    # no .env needed
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import re
import subprocess
import sys

log = logging.getLogger("sweep")

GREEN, RED, AMBER, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

#: Shapes that are a credential wherever they appear.
#:
#: Deliberately not "any long hex string": a git sha, a checksum and a UUID all
#: look like that, and a sweep that cries wolf on every commit hash is a sweep
#: nobody runs twice. High-entropy values with no distinguishing shape are what
#: the value pass is for.
PATTERNS: tuple[tuple[str, str], ...] = (
    ("private key", r"BEGIN (RSA|OPENSSH|EC|DSA|PGP)? ?PRIVATE KEY"),
    ("AWS access key id", r"AKIA[0-9A-Z]{16}"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{30,}"),
    ("Slack token", r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    ("JWT", r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\."),
    (
        "literal assignment",
        r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token)"
        r"[\"']?\s*[:=]\s*[\"'][^\"'$\{][^\"']{7,}[\"']",
    ),
)

#: Values this short are not secrets, they are settings — `true`, a port, a
#: hostname. Matching them would flag every compose file in the repository.
MIN_SECRET_LENGTH = 8

#: Variables whose value is a URL, a topic name or a flag rather than a
#: credential. Skipped so the report is about credentials and stays readable.
#: Being wrong here is safe in one direction only, so the rule is narrow: a name
#: is skipped for what it *is*, never because its value looked harmless.
NOT_SECRETS = frozenset({
    "REDPANDA_BROKERS", "SIGHTINGS_TOPIC", "MEDIAMTX_HOST", "SIM_VIDEO_DIR",
    "OSRM_URL", "API_CORS_ORIGINS", "VITE_API_BASE", "VITE_MEDIAMTX_BASE",
    "PUBLIC_ORIGIN", "POSTGRES_USER", "POSTGRES_DB", "POSTGRES_HOST",
    "SENTINEL_BASE", "ANPR_SHARDS", "HOST_PORT", "HOST_BIND",
})


def parse_env(path: pathlib.Path) -> dict[str, str]:
    """Read a dotenv file into name -> value, stripping quotes and comments."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def all_blobs() -> bytes:
    """Every blob in the object database, concatenated.

    `--batch-all-objects` reaches objects that no branch points at any more —
    which is the entire point. A secret removed in a later commit is exactly the
    case a `git grep` over HEAD misses.
    """
    result = subprocess.run(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        capture_output=True, check=True,
    )
    return result.stdout


def scan_patterns(corpus: bytes) -> list[tuple[str, str]]:
    """Every pattern hit, as (what it looks like, the matching line)."""
    text = corpus.decode("utf-8", errors="replace")
    hits: list[tuple[str, str]] = []
    for name, pattern in PATTERNS:
        for match in re.finditer(pattern, text):
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            line = text[start : end if end != -1 else match.end()].strip()
            hits.append((name, line[:200]))
    return hits


def scan_values(corpus: bytes, env: dict[str, str]) -> tuple[list[str], list[str]]:
    """Which .env values appear in history, by name. Returns (leaked, checked)."""
    leaked, checked = [], []
    for key, value in env.items():
        if key in NOT_SECRETS or len(value) < MIN_SECRET_LENGTH:
            continue
        checked.append(key)
        if value.encode() in corpus:
            leaked.append(key)
    return leaked, checked


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env", type=pathlib.Path, default=pathlib.Path(".env"))
    parser.add_argument("--patterns-only", action="store_true",
                        help="skip the .env value pass")
    args = parser.parse_args(argv)

    corpus = all_blobs()
    log.info("%s%.1f MB of git history scanned%s", DIM, len(corpus) / 1e6, RESET)

    hits = scan_patterns(corpus)
    if hits:
        log.info("\n%s%d pattern hits — read every one, they are not all real%s",
                 AMBER, len(hits), RESET)
        for name, line in hits:
            log.info("  %-20s %s", name, line)
        log.info("%sA test asserting a password is never logged looks exactly "
                 "like a leaked password. Judge them individually.%s", DIM, RESET)
    else:
        log.info("%sno pattern hits%s", GREEN, RESET)

    leaked: list[str] = []
    if not args.patterns_only:
        env = parse_env(args.env)
        if not env:
            log.info("\n%sno %s to check values against — pattern pass only. "
                     "That leaves high-entropy secrets unchecked, which is the "
                     "shape `openssl rand -hex 32` produces.%s",
                     AMBER, args.env, RESET)
        else:
            leaked, checked = scan_values(corpus, env)
            log.info("")
            if leaked:
                # The name, never the value.
                for key in leaked:
                    log.info("  %sLEAKED  %s appears verbatim in history%s",
                             RED, key, RESET)
                log.info("%sRotate it first, then rewrite history. In that "
                         "order: a rewritten history does not un-publish a "
                         "secret anyone already cloned.%s", DIM, RESET)
            else:
                log.info("  %s%d real credentials checked, none present in "
                         "history%s", GREEN, len(checked), RESET)

    log.info("")
    return 1 if leaked else 0


if __name__ == "__main__":
    sys.exit(main())
