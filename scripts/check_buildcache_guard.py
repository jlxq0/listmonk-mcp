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

It then re-runs itself against fifteen deliberately broken variants — three of
the build script, twelve of the workflow that reaches it — and requires each to
be rejected. A check that has never failed is a hypothesis; this one fails on
every CI run, on purpose.

The workflow-level cases exist because the script-level ones are not enough. An
export guard reading `refs/heads/main` is caught; a push trigger reading
`branches: [main]` was not, because it is not that literal string, and it is the
worse fault: the workflow never fires on master, nothing is built, the cache is
never written, and this script stops running. The check that would have caught
it is the thing the break disables. Two ways of breaking it found that; one
would have left the guard looking correct.

The branch list is not the only key with that property. `paths-ignore: ["**"]`
alongside a correct `branches: [master]`, and `if: false` on the `docker` job,
both leave every ref-level assertion here passing against a workflow that
builds nothing. A `strategy: matrix` on the `docker` job is the inverse and
worse: one push becomes N concurrent instances all exporting to the same ref,
which is the race this file exists for, reintroduced from a direction none of
the ref-level checks look. So does `if: false` on the build step, and on the
step that runs this file — the last one being the same shape once more, a
break that removes its own detector. Each has a self-test case.

Not every step-level `if:` is a fault: the registry login carries one, because
a pull request has no credentials to log in with. Only the two steps that must
run unconditionally are named.

Naming faults one at a time does not terminate. Four cross-engine passes over
this file produced four different keys and each fix invited the next, so the
last check is fail-closed rather than specific: the workflow may carry what it
carries today and nothing else, and anything new is a failure until someone has
read it. Keys were not enough either — a fifth pass produced
`runs-on: <label-no-runner-has>`, which keeps every allowed key and schedules
nothing — so whole mappings are pinned by value. The named checks above are
kept for what they explain, not for what they catch.

Where this stops. The property defended here is that the workflow cannot report
success while having built nothing or exported twice. A sixth pass proposed
`exit 1` in the `Install buildctl` step, which does stop the build — and turns
the run red, which is reported by the CI status itself and blocks the merge. It
is out of scope by construction, and so is every other edit whose effect is a
failing job. Only silent breaks belong here. `continue-on-error: true`, which
would convert a red run into a green one, is refused by the job-header pin.
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
DEFAULT_BRANCH: Final = "master"
EXPORTING_REF: Final = f"refs/heads/{DEFAULT_BRANCH}"
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


def push_trigger(workflow_path: Path) -> dict[str, object] | str:
    """The `on.push` mapping, or a string describing why there is none.

    PyYAML follows YAML 1.1, where the bare key `on:` is the boolean `True`.
    `document["on"]` raises KeyError and `document.get("on", {})` silently
    returns an empty mapping — which is how a check of this shape fails open.
    Read the `True` key.
    """
    document = yaml.safe_load(workflow_path.read_text())
    triggers = document.get(True, document.get("on"))
    if not isinstance(triggers, dict):
        return "no `on:` block"
    push = triggers.get("push")
    if not isinstance(push, dict):
        return "no `on.push` block"
    return push


def push_branches(workflow_path: Path) -> object:
    """The `on.push.branches` list, or a sentinel describing why there is none."""
    push = push_trigger(workflow_path)
    if isinstance(push, str):
        return push
    if "branches" not in push:
        return "`on.push` has no `branches` filter"
    return push["branches"]


# Trigger filters that suppress a run the branch list says will happen. Each
# leaves `on.push.branches` reading exactly `[master]` while the workflow does
# not fire, which is the same fault as `branches: [main]` wearing a different
# key. Named rather than allow-listed, because a new filter key added by
# Forgejo should be read by a person before it is trusted.
SUPPRESSING_PUSH_FILTERS: Final = (
    "paths",
    "paths-ignore",
    "branches-ignore",
    "tags-ignore",
)

# Jobs that must run unconditionally. A job-level `if:` is evaluated before any
# step, so `if: false` on `docker` builds nothing while every ref-level check in
# this script still passes — it runs the build script directly and never asks
# whether the job carrying it was entered.
UNCONDITIONAL_JOBS: Final = ("python", DOCKER_JOB)

# Steps that must run unconditionally, named per job. A blanket "no step-level
# `if:` in the docker job" would be wrong: the registry login legitimately
# carries `if: github.event_name != 'pull_request'`, because a pull request has
# no credentials to log in with. These two are different — `if: false` on the
# build step skips the build while this script executes the step's `run:` block
# regardless, and `if: false` on the guard step stops this file running at all.
UNCONDITIONAL_STEPS: Final = (
    (DOCKER_JOB, BUILD_STEP_NAME),
    ("python", "buildcache guard"),
)

# Naming faults one at a time does not terminate. Four cross-engine passes over
# this file produced four different keys — `paths-ignore`, a job `if:`, a
# `strategy:` matrix, a step `if:` — and each fix invited the next. The keys
# that decide whether a job runs, or how many times, are an open set, and a new
# Forgejo release can add one.
#
# So the shape below is fail-closed: these are the keys the workflow carries
# today, and anything else is a failure until a person has read it and either
# ruled it harmless here or written the specific check. `needs: nonexistent`
# never schedules the docker job and is a key that already exists, which is why
# the value is pinned and not only the key.
# Keys were not enough: `runs-on: some-label-no-runner-has` keeps the key and
# never schedules the job. So these pin whole mappings by value, everything
# except the parts with their own checks — a job's `steps`, a step's `run`.
EXPECTED_JOB_HEADER: Final = {
    "python": {"runs-on": "ubuntu-22.04"},
    DOCKER_JOB: {
        "runs-on": "ubuntu-22.04",
        "needs": "python",
        "permissions": {"contents": "read", "packages": "write"},
    },
}
EXPECTED_PUSH: Final = {"branches": [DEFAULT_BRANCH], "tags": ["v*"]}
DOCKER_NEEDS: Final = "python"


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

    # The export guard is only reachable if the workflow runs on the branch at
    # all. `branches: [main]` is not the literal `refs/heads/main`, so the grep
    # above passes it — while the workflow never fires on master, no image is
    # built, the cache is never written, and this very script stops running.
    # The check that would have caught it is the thing the break disables.
    branches = push_branches(workflow_path)
    if branches != [DEFAULT_BRANCH]:
        failures.append(
            f"{workflow_path.name}: on.push.branches is {branches!r}, expected "
            f"[{DEFAULT_BRANCH!r}]. This repository's default branch is "
            f"{DEFAULT_BRANCH}; any other value means the workflow never runs "
            "on it, so nothing is built and this guard never executes."
        )

    # A correct branch list is not enough: `paths-ignore: ["**"]` alongside it
    # filters out every master push while `branches` still reads `[master]`.
    push = push_trigger(workflow_path)
    if isinstance(push, dict):
        for key in SUPPRESSING_PUSH_FILTERS:
            if key in push:
                failures.append(
                    f"{workflow_path.name}: on.push carries `{key}: "
                    f"{push[key]!r}`. It can suppress a push to "
                    f"{DEFAULT_BRANCH} that the branch list says will run, "
                    "which leaves every check in this script passing against a "
                    "workflow that never fires."
                )

    document = yaml.safe_load(text)
    jobs = document.get("jobs") if isinstance(document, dict) else None
    for job_name in UNCONDITIONAL_JOBS:
        job = (jobs or {}).get(job_name)
        if not isinstance(job, dict):
            failures.append(
                f"{workflow_path.name}: no `{job_name}` job. If it was renamed, "
                "rename it here too rather than deleting this check."
            )
        elif "if" in job:
            failures.append(
                f"{workflow_path.name}: job `{job_name}` carries `if: "
                f"{job['if']!r}`. A job-level condition is evaluated before any "
                "step, so a false one skips the build while this script — which "
                "runs the build script directly — still passes."
            )

    # A matrix multiplies one job into N concurrent instances, each running the
    # same build script under the same ref. On master that is N writers to one
    # cache ref from a single push — the exact race this file exists for, and
    # invisible to every ref-level check here, which executes the script once.
    for job_name, step_name in UNCONDITIONAL_STEPS:
        job = (jobs or {}).get(job_name)
        steps = job.get("steps") if isinstance(job, dict) else None
        matching = [
            step
            for step in (steps or [])
            if isinstance(step, dict) and step.get("name") == step_name
        ]
        if not matching:
            failures.append(
                f"{workflow_path.name}: job `{job_name}` has no step named "
                f"{step_name!r}. If it was renamed, rename it here too rather "
                "than deleting this check."
            )
        elif any("if" in step for step in matching):
            condition = next(step["if"] for step in matching if "if" in step)
            failures.append(
                f"{workflow_path.name}: step {step_name!r} in job `{job_name}` "
                f"carries `if: {condition!r}`. A false condition skips it while "
                "this script, which extracts and runs the step's `run:` block "
                "directly, never reads the condition."
            )

    docker_job = (jobs or {}).get(DOCKER_JOB)
    if isinstance(docker_job, dict) and "strategy" in docker_job:
        failures.append(
            f"{workflow_path.name}: job `{DOCKER_JOB}` carries a `strategy:` "
            "block. A matrix runs it once per combination, concurrently, all "
            "exporting to the same ref from one push. If a matrix is genuinely "
            "wanted, the cache ref has to be qualified per combination first."
        )

    failures += check_no_unread_keys(workflow_path.name, push, jobs)

    return failures


def check_no_unread_keys(
    name: str, push: dict[str, object] | str, jobs: object
) -> list[str]:
    """Fail on anything nobody has read, rather than on a list of known faults.

    The named checks above each say why one key is dangerous. This one says
    nothing about danger: it says the workflow carries something no check here
    understands, and a check that does not understand a value cannot vouch for
    it. Keys alone were not enough — `runs-on: a-label-no-runner-has` keeps
    every allowed key and schedules nothing — so whole mappings are pinned by
    value, minus the parts with their own checks.
    """
    failures: list[str] = []

    if isinstance(push, dict) and push != EXPECTED_PUSH:
        failures.append(
            f"{name}: on.push is {push!r}, expected {EXPECTED_PUSH!r}. Trigger "
            "filters decide whether a push to master runs at all, and nothing "
            "here knows what a new one does. Read it, then either widen this "
            "or write the check."
        )

    for job_name, expected in EXPECTED_JOB_HEADER.items():
        job = (jobs or {}).get(job_name) if isinstance(jobs, dict) else None
        if not isinstance(job, dict):
            continue  # already reported as a missing job
        header = {key: value for key, value in job.items() if key != "steps"}
        if header != expected:
            failures.append(
                f"{name}: job `{job_name}` header is {header!r}, expected "
                f"{expected!r}. Everything outside `steps:` decides whether the "
                "job is scheduled and how many instances run, which is the "
                "whole subject of this file."
            )
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("name") not in {
                step_name
                for owner, step_name in UNCONDITIONAL_STEPS
                if owner == job_name
            }:
                continue
            around = {key: value for key, value in step.items() if key != "run"}
            if around != {"name": step.get("name")}:
                failures.append(
                    f"{name}: step {step.get('name')!r} in job `{job_name}` "
                    f"carries {around!r} around its `run:` block. This script "
                    "runs that block and reads nothing else about the step, so "
                    "anything else on it is unchecked."
                )

    docker_job = (jobs or {}).get(DOCKER_JOB) if isinstance(jobs, dict) else None
    if isinstance(docker_job, dict) and docker_job.get("needs") != DOCKER_NEEDS:
        failures.append(
            f"{name}: job `{DOCKER_JOB}` needs {docker_job.get('needs')!r}, "
            f"expected {DOCKER_NEEDS!r}. A `needs:` naming a job that does not "
            "exist is never scheduled, so the build silently does not happen "
            "while every other check here passes."
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


# Deliberately broken *workflows*. These are YAML-level breaks that leave the
# build script untouched, so `check_guard` cannot see them: it runs the script
# directly and never asks whether a push to master reaches it. `check_static`
# is the only thing standing between these and a repository with no CI.
BROKEN_WORKFLOWS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "push trigger copied from a `main` repo, export guard left correct",
        "branches: [master]",
        "branches: [main]",
    ),
    (
        "push trigger restricted to a branch that does not exist",
        "branches: [master]",
        "branches: [release]",
    ),
    (
        "push trigger's branch filter dropped entirely",
        "    branches: [master]\n",
        "",
    ),
    (
        "path filter suppressing every push the branch list admits",
        "    branches: [master]\n",
        '    branches: [master]\n    paths-ignore: ["**"]\n',
    ),
    (
        "docker job gated off while every ref-level check still passes",
        "  docker:\n    runs-on:",
        "  docker:\n    if: false\n    runs-on:",
    ),
    (
        "matrix multiplying one push into two concurrent exporters",
        "  docker:\n    runs-on:",
        "  docker:\n    strategy:\n      matrix:\n        slot: [1, 2]\n    runs-on:",
    ),
    (
        "build step gated off while its `run:` block still passes every check",
        "      - name: Build (and push on tag)\n",
        "      - name: Build (and push on tag)\n        if: false\n",
    ),
    (
        "the guard step itself gated off, so this file stops running on CI",
        "      - name: buildcache guard\n",
        "      - name: buildcache guard\n        if: false\n",
    ),
    (
        "docker job waiting on a job that does not exist",
        "    needs: python\n",
        "    needs: nonexistent\n",
    ),
    (
        "a job-level key no check here has read",
        "    needs: python\n",
        "    needs: python\n    continue-on-error: true\n",
    ),
    (
        "a trigger filter no check here has read",
        "    branches: [master]\n",
        "    branches: [master]\n    types: [deleted]\n",
    ),
    (
        "docker job pinned to a runner label that does not exist",
        "  docker:\n    runs-on: ubuntu-22.04\n",
        "  docker:\n    runs-on: ubuntu-24.04-arm-nonexistent\n",
    ),
)


def self_test_workflow(workflow_path: Path, tmp: Path) -> list[str]:
    """Break the workflow YAML three ways and require `check_static` to catch each."""
    failures: list[str] = []
    text = workflow_path.read_text()
    for index, (name, old, new) in enumerate(BROKEN_WORKFLOWS):
        if old not in text:
            failures.append(
                f"self-test {name!r}: anchor {old!r} not found in {workflow_path.name}. "
                "The workflow changed shape; update this harness rather than "
                "deleting the case."
            )
            continue
        broken_path = tmp / f"broken-workflow-{index}.yml"
        broken_path.write_text(text.replace(old, new, 1))
        caught = check_static(broken_path)
        if not caught:
            failures.append(
                f"self-test {name!r}: harness accepted a workflow it must reject. "
                "A workflow that never runs on master builds nothing, and this "
                "check is one of the things it stops running."
            )
        else:
            print(f"  self-test rejected: {name}")
            for line in caught:
                print(f"      {line.splitlines()[0]}")
    return failures


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
        print(f"  on.push.branches = {push_branches(args.workflow)!r}")
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
            failures += self_test_workflow(args.workflow, tmp)

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
