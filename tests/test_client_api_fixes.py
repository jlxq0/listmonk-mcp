"""The two Listmonk API fixes, observed on the wire against a stub.

`dc07b44` and `4fd84c2` are behaviour changes to the request body the client
sends, verified when they were written by hand against a live instance and by
nothing since. Both were invisible to the suite: every other test imports
``transport``, ``metrics`` or ``server``, and none of them import ``client``.

What makes these worth having is that neither fix changes a return value. The
old code and the new code both succeed against a permissive server; the
difference is only in the bytes on the wire, so the stub records requests and
the assertions read the recorded body rather than the client's return.

Each test names the mutation that turns it red, next to the assertion it turns.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from listmonk_mcp.client import ListmonkClient
from listmonk_mcp.config import Config

CAMPAIGN: dict[str, Any] = {
    "id": 7,
    "name": "August newsletter",
    "subject": "What we shipped",
    "lists": [{"id": 3, "name": "subscribers"}, {"id": 5, "name": "beta"}],
    "from_email": "news@example.test",
    "body": "<p>hello</p>",
    "altbody": None,
    "content_type": "richtext",
    "messenger": "email",
    "type": "regular",
    "tags": ["monthly"],
    "template_id": 2,
    "headers": [],
}


class RecordedRequest:
    def __init__(self, method: str, path: str, body: bytes) -> None:
        self.method = method
        self.path = path
        self.raw_body = body

    @property
    def json(self) -> Any:
        return json.loads(self.raw_body) if self.raw_body else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.method} {self.path} {self.raw_body!r}>"


class StubListmonk:
    """A few endpoints returning canned JSON, recording what it was sent.

    Deliberately permissive: it accepts any body and answers 200. A stub that
    validated the payload the way listmonk does would make these tests pass for
    the wrong reason — they would be asserting that the stub rejects the old
    shape, which is a fact about the stub.
    """

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _record_and_reply(self, payload: Any) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                recorder.requests.append(
                    RecordedRequest(self.command, self.path, body)
                )
                encoded = json.dumps({"data": payload}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                if self.path == "/api/health":
                    self._record_and_reply(True)
                elif self.path.startswith("/api/campaigns/"):
                    self._record_and_reply(CAMPAIGN)
                else:
                    self._record_and_reply({})

            def do_POST(self) -> None:
                self._record_and_reply(True)

            def do_PUT(self) -> None:
                self._record_and_reply(True)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Keep-alive handler threads block reading the next request, and a
        # non-daemon handler makes server_close() join them. Both defaults cost
        # about half a second per test; neither is load-bearing here.
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02),
            daemon=True,
        )

    def __enter__(self) -> StubListmonk:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def last(self, method: str, path_contains: str) -> RecordedRequest:
        for recorded in reversed(self.requests):
            if recorded.method == method and path_contains in recorded.path:
                return recorded
        raise AssertionError(
            f"no {method} to a path containing {path_contains!r}; "
            f"saw {self.requests!r}"
        )


@pytest.fixture
def stub() -> Iterator[StubListmonk]:
    with StubListmonk() as running:
        yield running


@pytest.fixture
def client(stub: StubListmonk) -> Iterator[ListmonkClient]:
    """A real ListmonkClient pointed at the stub, via LISTMONK_MCP_URL's field."""
    config = Config(url=stub.url, username="stub", password="token")
    yield ListmonkClient(config)


class TestTestCampaignSendsFullPayload:
    """dc07b44: listmonk validates the test request as a full campReq.

    Sending only ``{"subscribers": [...]}`` fails with "Invalid length for name"
    because the empty name fails ``strHasLen``.
    """

    async def test_body_carries_the_saved_campaign_fields(
        self, client: ListmonkClient, stub: StubListmonk
    ) -> None:
        async with client:
            await client.test_campaign(7, ["someone@example.test"])

        body = stub.last("POST", "/api/campaigns/7/test").json

        # Mutation: restore `data = {"subscribers": subscribers}` in
        # ListmonkClient.test_campaign. Every assertion below fails on the
        # missing keys; the four listed first are the ones listmonk's
        # strHasLen validation rejects.
        assert body["name"] == "August newsletter"
        assert body["subject"] == "What we shipped"
        assert body["body"] == "<p>hello</p>"
        assert body["messenger"] == "email"
        assert body["content_type"] == "richtext"
        assert body["from_email"] == "news@example.test"
        assert body["type"] == "regular"
        assert body["template_id"] == 2

    async def test_lists_are_flattened_to_ids(
        self, client: ListmonkClient, stub: StubListmonk
    ) -> None:
        async with client:
            await client.test_campaign(7, ["someone@example.test"])

        # Mutation: drop the comprehension and pass `camp.get("lists", [])`
        # straight through. listmonk's campReq binds `lists` as []int, so the
        # objects the GET returns are not what the POST may carry back.
        assert stub.last("POST", "/api/campaigns/7/test").json["lists"] == [3, 5]

    async def test_recipients_survive_the_merge(
        self, client: ListmonkClient, stub: StubListmonk
    ) -> None:
        async with client:
            await client.test_campaign(7, ["a@example.test", "b@example.test"])

        # Mutation: remove the `"subscribers": subscribers` key. Merging the
        # saved campaign in is only useful if it does not displace the thing
        # the caller asked for.
        assert stub.last("POST", "/api/campaigns/7/test").json["subscribers"] == [
            "a@example.test",
            "b@example.test",
        ]

    async def test_it_reads_the_campaign_before_testing_it(
        self, client: ListmonkClient, stub: StubListmonk
    ) -> None:
        async with client:
            await client.test_campaign(7, ["someone@example.test"])

        # Mutation: delete the `await self.get_campaign(campaign_id)` line.
        # Without the GET there is nothing to merge, and the three assertions
        # above would be satisfied by hardcoded defaults.
        methods = [(r.method, r.path) for r in stub.requests]
        assert ("GET", "/api/campaigns/7") in methods
        assert methods.index(("GET", "/api/campaigns/7")) < methods.index(
            ("POST", "/api/campaigns/7/test")
        )


class TestUpdateSettingSendsRawValue:
    """4fd84c2: UpdateSettingsByKey binds the body as json.RawMessage.

    Wrapping the value in ``{"value": ...}`` stores the whole object under the
    key, which broke ``custom_js`` and ``custom_css`` until they were restored
    through the full-settings PUT.
    """

    async def test_string_value_is_the_whole_body(
        self, client: ListmonkClient, stub: StubListmonk
    ) -> None:
        async with client:
            await client.update_setting(
                "appearance.public.custom_js", "console.log(1)"
            )

        recorded = stub.last("PUT", "/api/settings/appearance.public.custom_js")

        # Mutation: restore `json_data={"value": value}`. The body becomes
        # {"value": "console.log(1)"} and both assertions fail — the second is
        # the one that names the actual breakage, since a wrapper is exactly
        # what listmonk writes into the settings document verbatim.
        assert recorded.json == "console.log(1)"
        assert recorded.raw_body == b'"console.log(1)"'

    @pytest.mark.parametrize(
        "value",
        [42, True, False, None, ["a", "b"], {"nested": "object"}],
        ids=["int", "true", "false", "null", "array", "object"],
    )
    async def test_non_string_values_are_not_wrapped_either(
        self, client: ListmonkClient, stub: StubListmonk, value: Any
    ) -> None:
        async with client:
            await client.update_setting("some.key", value)

        # Mutation: as above. The `{"nested": "object"}` case is the one that
        # makes the wrapper invisible to a naive assertion — a wrapped body is
        # also a JSON object, so asserting only "the body is a dict" would stay
        # green against the bug.
        assert stub.last("PUT", "/api/settings/some.key").json == value

    async def test_full_settings_put_still_sends_the_mapping(
        self, client: ListmonkClient, stub: StubListmonk
    ) -> None:
        async with client:
            await client.update_settings({"app.site_name": "Example"})

        # Mutation: change `update_settings` to send `{"value": settings}`.
        # The per-key fix loosened `_request`'s json_data type to Any; this
        # pins that the bulk path was not loosened along with it.
        assert stub.last("PUT", "/api/settings").json == {"app.site_name": "Example"}
