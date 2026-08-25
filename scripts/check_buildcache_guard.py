#!/usr/bin/env python3
"""Execute the CI workflow's cache-export guard and assert exactly one writer.

Two concurrent `buildctl` jobs exporting to one unqualified `:buildcache` ref
race on the blob write. One loses, with `error writing layer blob: unknown`,
after its image has already been pushed. Merging a pull request and pushing the
release tag are two pushes seconds apart, so the two jobs overlap routinely.

Three ways of "checking" this do not work, each learned the expensive way:

* **A green `docker` job proves nothing.** caldav-mcp tasks 16484 and 16485
  overlapped, both exported to one ref, and both passed.
* **An unchanged `:buildcache` manifest digest proves nothing.** The manifest is
  content-addressed, so a full cache hit re-exports identical bytes to an
  identical digest.
* **Grepping the job log proves nothing.** Log retention on this Forgejo is
  sporadic; `grep -c "exporting cache to registry"` returns 0 when the log is
  missing, and 0 is also the answer that means "correct".

What does work is running the guard. This script pulls the `docker` job's build
script out of the YAML, runs it under four refs with `buildctl` stubbed, and
counts the `--export-cache` arguments it was actually invoked with.

It then re-runs itself against three deliberately broken variants of the same
script and requires each to be rejected. A check that has never failed is a
hypothesis; this one fails on every CI run, on purpose.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
WORKFLOW: Final = REPO_ROOT / ".forgejo" / "workflows" / "ci.yml"

BUILD_STEP_NAME: Final = "Build (and push on tag)"
DOCKER_JOB: Final = "docker"

# The one ref that may write to the cache, and the refs that must not.
EXPORTING_REF: Final = "refs/heads/master"
NON_EXPORTING_REFS: Final = (
    "refs/tags/v0.2.0",
    "refs/pull/1/head",
    "refs/heads/feat/some-branch",
)

# `buildctl` and `node` are not installed where this runs, and neither is a
# buildkitd. Both are replaced by stubs: `node` returns a plausible gateway,
# `buildctl` records its argv and exits. Everything else in the script — the
# `sed` on pyproject.toml, the `git show`, the ref comparisons — runs for real.
STUB_PREAMBLE: Final = """
node() { echo "172.17.0.1"; }
sudo() { "$@"; }
buildctl() {
  printf '%s\\n' "$@" > "$GUARD_ARGV_OUT"
  return 0
}
"""


class GuardFailure(Exception):
    """The workflow's guard did not behave as the deployment contract requires."""


def find_bash() -> str:
    """A bash new enough to expand an empty array under `set -u`.

    `"${EXPORT_CACHE_ARG[@]}"` on an empty array is an unbound-variable error
    before bash 4.4. The runner is ubuntu-22.04 (bash 5.1) so the workflow is
    correct there, but macOS ships bash 3.2 and would report a guard failure
    that is really a shell-version failure. Fail with the real reason instead.
    """
    for candidate in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "bash"):
        path = shutil.which(candidate)
        if path is None:
            continue
        out = subprocess.run(
            [path, "-c", "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
            capture_output=True,
            text=True,
        )
        major, _, minor = out.stdout.strip().partition(".")
        if major.isdigit() and minor.isdigit() and (int(major), int(minor)) >= (4, 4):
            return path
    raise GuardFailure(
        "no bash >= 4.4 found. The workflow's build script uses empty-array "
        "expansion under `set -u`, which older bash rejects. On macOS: "
        "`brew install bash`."
    )


def extract_build_script(workflow_path: Path = WORKFLOW) -> str:
    """Pull the `docker` job's build step out of the workflow YAML."""
    document = yaml.safe_load(workflow_path.read_text())
    try:
        steps = document["jobs"][DOCKER_JOB]["steps"]
    except (KeyError, TypeError) as exc:
        raise GuardFailure(
            f"{workflow_path} has no jobs.{DOCKER_JOB}.steps"
        ) from exc

    for step in steps:
        if step.get("name") == BUILD_STEP_NAME:
            run = step.get("run")
            if not run:
                raise GuardFailure(f"step {BUILD_STEP_NAME!r} has no `run` block")
            return str(run)

    raise GuardFailure(
        f"no step named {BUILD_STEP_NAME!r} in jobs.{DOCKER_JOB}; "
        "if it was renamed, rename it here too rather than deleting this check"
    )


def buildctl_argv(script: str, ref: str, tmp: Path) -> list[str]:
    """Run the build script under ``ref`` and return the argv buildctl received."""
    argv_out = tmp / f"argv-{ref.replace('/', '_')}.txt"
    result = subprocess.run(
        [find_bash(), "-c", STUB_PREAMBLE + "\n" + script],
        cwd=REPO_ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp),
            "GITHUB_REF": ref,
            "GITHUB_SHA": "0123456789abcdef0123456789abcdef01234567",
            "GITHUB_EVENT_NAME": "pull_request" if ref.startswith("refs/pull/") else "push",
            "GUARD_ARGV_OUT": str(argv_out),
        },
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GuardFailure(
            f"build script exited {result.returncode} under {ref}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    if not argv_out.exists():
        raise GuardFailure(f"buildctl was never invoked under {ref}")
    return argv_out.read_text().splitlines()


def export_arg_count(argv: list[str]) -> int:
    return argv.count("--export-cache")


def import_arg_count(argv: list[str]) -> int:
    return argv.count("--import-cache")


def check_guard(script: str, tmp: Path) -> list[str]:
    """Return a list of contract violations. Empty means the guard is correct."""
    failures: list[str] = []

    exporting = buildctl_argv(script, EXPORTING_REF, tmp)
    count = export_arg_count(exporting)
    if count != 1:
        failures.append(
            f"{EXPORTING_REF}: expected exactly 1 --export-cache, got {count}. "
            "The branch build is the single writer; without it the cache is "
            "never populated and every build is cold."
        )
    if import_arg_count(exporting) != 1:
        failures.append(f"{EXPORTING_REF}: expected exactly 1 --import-cache")

    for ref in NON_EXPORTING_REFS:
        argv = buildctl_argv(script, ref, tmp)
        count = export_arg_count(argv)
        if count != 0:
            failures.append(
                f"{ref}: expected 0 --export-cache, got {count}. "
                "A second writer to the ref races the branch build's blob "
                "write and fails a run whose image is already published."
            )

    return failures


def check_static(workflow_path: Path = WORKFLOW) -> list[str]:
    """Count export sites in the raw YAML, not just the ones a ref reaches."""
    failures: list[str] = []
    text = workflow_path.read_text()

    sites = len(re.findall(r"--export-cache", text))
    if sites != 1:
        failures.append(
            f"{workflow_path.name}: found {sites} --export-cache sites, expected 1. "
            "typst-mcp had one export site and still had two writers; more than "
            "one site cannot be safe regardless of how it is guarded."
        )

    if "refs/heads/main" in text:
        failures.append(
            f"{workflow_path.name}: guards on refs/heads/main. This repository's "
            "default branch is master, so that guard never fires."
        )

    return failures


# Deliberately broken variants. Each is a real shape that shipped somewhere in
# this fleet; the harness must reject all three.
BROKEN_VARIANTS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "guarded on event_name instead of the branch ref",
        'elif [[ "$GITHUB_REF" == refs/heads/master ]]; then',
        'elif [[ "$GITHUB_EVENT_NAME" != "pull_request" ]]; then',
    ),
    (
        "second writer on the tag build (the m365-mcp#4 shape)",
        '\nelif [[ "$GITHUB_REF" == refs/heads/master ]]; then',
        '\n  EXPORT_CACHE_ARG=(--export-cache "type=registry,ref=${CACHE_REF},mode=max")'
        '\nelif [[ "$GITHUB_REF" == refs/heads/master ]]; then',
    ),
    (
        "guarded on refs/heads/main, which this repo does not have",
        'elif [[ "$GITHUB_REF" == refs/heads/master ]]; then',
        'elif [[ "$GITHUB_REF" == refs/heads/main ]]; then',
    ),
)


def self_test(script: str, tmp: Path) -> list[str]:
    """Break the guard three ways and require the harness to catch each."""
    failures: list[str] = []
    for name, old, new in BROKEN_VARIANTS:
        if old not in script:
            failures.append(
                f"self-test {name!r}: anchor not found in the build script. "
                "The script changed shape; update this harness rather than "
                "deleting the case."
            )
            continue
        broken = script.replace(old, new, 1)
        if broken == script:
            failures.append(f"self-test {name!r}: mutation was a no-op")
            continue
        caught = check_guard(broken, tmp)
        if not caught:
            failures.append(
                f"self-test {name!r}: harness accepted a script it must reject. "
                "The check is not measuring what it claims to."
            )
        else:
            print(f"  self-test rejected: {name}")
            for line in caught:
                print(f"      {line.splitlines()[0]}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow", type=Path, default=WORKFLOW, help="path to the workflow YAML"
    )
    parser.add_argument(
        "--no-self-test",
        action="store_true",
        help="skip the deliberately-broken variants (not for CI)",
    )
    args = parser.parse_args()

    import tempfile

    script = extract_build_script(args.workflow)

    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)

        print(f"buildcache guard: {args.workflow}")
        static_failures = check_static(args.workflow)

        print(f"  {EXPORTING_REF}")
        exporting = buildctl_argv(script, EXPORTING_REF, tmp)
        print(
            f"      --export-cache x{export_arg_count(exporting)}"
            f"  --import-cache x{import_arg_count(exporting)}"
        )
        for ref in NON_EXPORTING_REFS:
            argv = buildctl_argv(script, ref, tmp)
            print(f"  {ref}")
            print(
                f"      --export-cache x{export_arg_count(argv)}"
                f"  --import-cache x{import_arg_count(argv)}"
            )

        failures = static_failures + check_guard(script, tmp)

        if not args.no_self_test:
            print("self-test (breaking the guard on purpose):")
            failures += self_test(script, tmp)

    if failures:
        sys.stdout.flush()
        print("\nFAIL", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nOK: exactly one writer to :buildcache, on refs/heads/master")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
