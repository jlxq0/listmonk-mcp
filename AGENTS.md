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
