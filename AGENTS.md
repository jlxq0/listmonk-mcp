# AGENTS.md

Constraints and pitfalls for `listmonk-mcp`. Anything discoverable from the code
is deliberately absent — read the code for that.

## This is a fork, and the fork question is live

Upstream is `rhnvrm/listmonk-mcp`. **Until pull request #2 merges, `master` is
byte-identical to upstream** — the fork carries zero divergence, and every
change made here sits unmerged on `feat/comprehensive-api-coverage`. Verified
2026-08-26: `origin/master` and `rhnvrm/listmonk-mcp@HEAD` are both `3e1cf0d`.

Read that as present tense until you have checked. Anything built on `master`
before #2 lands ships upstream's server without the 41 extra tools, and nothing
about the resulting image says so.

That is worth not losing by accident. **State in every pull request whether the
change is a patch this fork carries or something to send upstream.** A fork that
accumulates carried patches by default becomes unmergeable without anyone
deciding it should.

Rough division as things stand: the ~41 extra tools are a broad API-surface
expansion and a conversation with the upstream maintainer; the streamable-HTTP
transport, the Dockerfile and CI are deployment infrastructure for this fleet
and would not be wanted upstream; individual API bug fixes are upstreamable
as-is.

## The default branch is `master`, not `main`

The rest of the fleet uses `main`. **Every ref guard in
`.forgejo/workflows/ci.yml` must say `master`.** Copying a `main` guard in from
`caldav-mcp` or `jmap-mcp` disables it silently: the condition simply never
fires, and nothing reports an error. `scripts/check_buildcache_guard.py` fails
on a literal `refs/heads/main` anywhere in the workflow for exactly this reason.

**That literal is not the whole surface, and the part it misses is worse.** The
push trigger's branch filter is `branches: [master]`, which is not the string
`refs/heads/main`. Changing it to `branches: [main]` — the same copy-from-another-repo
mistake, one line higher in the same file — left the export guard correct and
the check passing green, exit 0, printing "OK: exactly one writer to
:buildcache, on refs/heads/master". It is the worse fault of the two: the
workflow never fires on `master`, so no image is built, the cache is never
written, and the guard itself never runs. **The check that would have caught it
is the thing the break disables.** A guard that passes because the workflow it
guards no longer runs is worse than the fault it was written for, because the
green is evidence of nothing and reads as evidence of everything.

It was found by breaking the check **two** ways rather than one. Break one, the
export guard changed to `refs/heads/main`, was caught: exit 1, four failures.
Stopping there would have left the guard looking sound. Anything asserting a
property of this workflow gets broken at every level that can carry the fault,
not at the level the last bug happened to be on.

`check_static` now asserts `on.push.branches == ["master"]`, and three
workflow-level breaks run in the self-test on every CI run. **PyYAML follows
YAML 1.1, so the bare key `on:` parses as the boolean `True`.** `document["on"]`
raises `KeyError` and `document.get("on", {})` returns an empty mapping and
passes — which is this same failure shape a third time, in the code that checks
for it.

## `.forgejo/workflows` shadows `.github/workflows`

This repository carries both. The `.github` ones are upstream's — a PyPI
publish and a GitHub Pages docs build — and **neither runs here, nor should
they be deleted.**

Forgejo reads `.forgejo/workflows` and ignores `.github/workflows` entirely
once the former exists. That is established by observation rather than
documentation: `caldav-mcp` carries both, its `.github/workflows/ci.yml`
declares jobs `quality` and `container`, and across every task record on that
repository the only job names that have ever run are `cargo` and `docker`.

So deleting `.github/workflows` buys nothing and costs fork divergence and a
merge conflict against upstream. A red `Deploy Docs` on a pull request whose
head predates `.forgejo/` is expected and resolves itself once `.forgejo/` is
in the tree.

## Deployment contract

Every MCP server in this fleet meets the same one, and this is the Python
instance of it:

- **Public listener**, default `0.0.0.0:3000`, carrying the MCP endpoint and a
  health endpoint. This is what the HTTPRoute targets.
- **The MCP endpoint is `/mcp/`, with the trailing slash.** `POST /mcp` answers
  `307 Temporary Redirect` to `/mcp/`; Starlette mounts it that way and the
  redirect is method-preserving, so a client that follows redirects works
  either way. An HTTPRoute matching the exact path `/mcp` and nothing else
  routes the redirect and not the session. Verified 2026-08-26 against the
  released `v0.2.0` image.
- **Metrics on a separate listener**, default `127.0.0.1:9090`, resolved as
  explicit env, then `{POD_IP}:9090`, then loopback. **It must never default to
  `0.0.0.0`.** The reason is that `/metrics` must not be publicly routable, and
  that reason does not depend on the language: serving both from one listener is
  less code and puts the metrics endpoint behind the public hostname, which is
  the thing the split prevents.
- **stdio stays working.** It is how the server is used from a local client, and
  replacing it with HTTP trades one deployment for another.

## One writer to `:buildcache`

The `docker` job exports the layer cache **only** on `refs/heads/master`. The
tag build imports and does not export.

Merging a pull request and pushing the release tag are two pushes seconds apart,
so two `docker` jobs run concurrently. Both exporting to one unqualified
`:buildcache` ref makes one lose the blob write and fail with
`error writing layer blob: unknown` — after its image has already been pushed.

**Guard on the branch ref by name.** `github.event_name != 'pull_request'` looks
equivalent and is not: it is also true on a tag push, which is the second writer.
`typst-mcp` had exactly one export site guarded that way and still had two
writers.

Three ways of checking this do not work, each learned the expensive way:

- A green `docker` job proves nothing. Two overlapping jobs both exporting to
  one ref both pass.
- An unchanged `:buildcache` manifest digest proves nothing. The manifest is
  content-addressed, so a full cache hit re-exports identical bytes to an
  identical digest.
- Grepping the job log proves nothing. Log retention on this Forgejo is
  sporadic, and `grep -c` returns `0` when the log is missing — the same answer
  that means "correct".

**Residual, known and deliberate.** The guard makes the *ref classes* disjoint:
merging a pull request and pushing the release tag no longer collide. Two
pushes to `master` in quick succession would still overlap and both export. A
`concurrency:` group is the obvious fix and is not applied, because Forgejo's
support for job-level concurrency is unverified on this instance and an
unsupported key is ignored **silently** — which would look fixed and not be.
Per-ref cache refs are the other option and give the tag build a cold cache
while accumulating refs on a registry whose disk is currently the bottleneck.
The five other servers in this fleet carry the same residual; `m365-mcp#7`
tracks it.

What works is `scripts/check_buildcache_guard.py`, which runs the workflow's own
branch logic under four refs and counts the arguments `buildctl` actually
received. It also breaks the guard three ways and requires each break to be
caught, so its failure path executes on every CI run rather than being assumed.

## Do not touch the live Listmonk

**Do not create a Listmonk API user or role.** The scope of that credential is
Julian's decision, not an implementation detail. `campaigns:send` is a distinct
permission from `campaigns:manage` in Listmonk v6.1.0, so a credential that can
draft but not send is a real option and the choice is his.

Whichever he picks, it arrives through an ExternalSecret from 1Password: never
an env value in a manifest, never a `.env`.

For local work, point `LISTMONK_MCP_URL` at a stub. A few endpoints returning
canned JSON is enough to exercise both transports end to end.

## Known pitfalls

Each of these shipped broken at least once, or would have.

**`master` requires `CI / python*` and deliberately not `CI / docker`.** A job
skipped because the job it `needs:` failed posts **`success`** to the commit
status. Measured here on `e005d4b`, a throwaway branch carrying one failing
test:

    CI / python (pull_request) = failure
    CI / docker (pull_request) = success     <- no docker task existed for that run

`docker` carries `needs: python`, so requiring it would build a gate that is
green precisely when the work did not happen — the same shape as a guard that
passes because the workflow it guards no longer runs.

**The glob is load-bearing.** The context carries an event suffix,
`(pull_request)` on a pull-request head and `(push)` on a branch push, so a
literal `CI / python (pull_request)` matches one and silently never matches the
other.

**What excluding `docker` costs, stated rather than discovered.** A genuine
docker failure no longer blocks a merge, and this repository has had exactly
one: `d74a768` merged with `python` green and `docker` red, the missing
`FORGE_PUSH_TOKEN` above. That failure is loud on `master` and reaches the
release tag, so it is caught by someone reading the run rather than by the
gate. Requiring the context that reports `success` when skipped would not have
caught it either — it would have hidden the next one.

**The declared Python floor is type-checked and never run.** `pyproject`
declares `requires-python = ">=3.11"`; `.python-version` pins 3.13, uv honours
it, and the Dockerfile pins `python:3.13-slim-bookworm` by digest, so CI and
the image agree with each other and neither exercises 3.11. mypy is configured
at `python_version = "3.11"`, which type-checks the floor and executes nothing.

Checked rather than assumed on 2026-08-26: the suite passes on 3.11.15, all 66
tests, in a throwaway venv. So the declaration is true today — it is simply not
a thing CI can keep true, and a change that needs 3.12+ would land green.

No 3.11 job was added: exercising a version nobody in this fleet runs buys less
than the noise costs. The alternative, raising the floor to 3.13, is not taken
because this is a fork of a package upstream publishes to PyPI at `>=3.11`, and
narrowing that is divergence to carry or send up rather than a local tidy. If
the floor ever stops being true, raise it — do not leave it declared.

**A new repository in this fleet has no `FORGE_PUSH_TOKEN`, and no pull request
will tell you.** The registry-login step is guarded
`if: github.event_name != 'pull_request'`, because a pull request has no
credentials to log in with — so every pull-request `docker` job is green while
the secret is absent. The first push to `master` is the first run that logs in
and the first that exports the layer cache, and it fails there.

That is what happened on `d74a768`, the merge of #3: `python` green, `docker`
red, on a repository whose five siblings all carried the secret and which had
never had one set. It would have failed the release tag too, which pushes the
image.

The token is the `jlxq0` CI bot (`repository:Read` + `package:Read/Write`),
already provisioned; the gap was this repository's secret, not the credential.
Set it before the first merge to `master`, and read
`GET /repos/{owner}/{repo}/actions/secrets` against a sibling repository rather
than trusting that a green pull request means the build path works.

**`uv sync` installs the project editable by default.** In a multi-stage image
that leaves a `.pth` pointing at the builder's source directory, which does not
exist in the runtime stage. The container starts and dies with
`ModuleNotFoundError`. Use `--no-editable`.

**uv bakes an absolute interpreter path into every console-script shebang.** A
venv built at `/build/.venv` and copied to `/app/.venv` exec's
`/build/.venv/bin/python3` and dies with `no such file or directory`. Build the
venv at its final path (`UV_PROJECT_ENVIRONMENT`), do not relocate it. Both of
these fail at container start, not at build, so a green build proves neither.

**FastMCP runs its `lifespan` once per MCP session over streamable-HTTP, not
once per process.** Passing the Listmonk-client lifespan to the `FastMCP`
constructor means the client connects and disconnects per client session, and
two concurrent sessions race on the module globals — the first to finish closes
the client the second is still using. Over HTTP, manage the client at the ASGI
lifespan instead. Over stdio the per-session lifespan is correct, because there
is exactly one session per process.

**A tool name arriving over the wire is caller-controlled.** Recording it as a
metric label without checking that it is registered lets any client grow the
metric registry without bound, one label set per request, for the life of the
process. Unregistered names collapse to a single label. The same rule bars
exception text, subscriber emails, campaign bodies and credentials from labels:
per-recipient detail is what Listmonk's own activity log is for, and mixing it
into metrics leaks recipient identity to anyone who can reach the endpoint.

**The build script uses empty-array expansion under `set -u`.** That is an
unbound-variable error before bash 4.4. The runner is fine (bash 5.1); macOS
ships bash 3.2, so anything running the script locally needs a newer one.

## Review is cross-engine

Codex reviews Claude's work here, and Claude reviews Codex's. Ask one specific
question rather than for a general review, bound it with `timeout 180`, and
check every finding against the code before acting on it.
