"""§11.4 identity-resolution tests — `shared` vs `per_user`, link-URL, fallback/withhold.

`IdentityResolver`'s repository collaborator is `Protocol`-typed (see
`shared/mcp/gateway/identity.py`), so these tests inject a small in-memory
fake — same pattern `tests/unit/mcp/test_tools.py`/`test_server.py` use
for the knowledge/routing/usage collaborators.
"""

from __future__ import annotations

import time

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client

from shared.mcp.gateway.auth import (
    HeaderAuthConfig,
    OAuth2AuthCodeConfig,
    OAuth2ClientCredentialsConfig,
    OutboundAuth,
    TokenSet,
)
from shared.mcp.gateway.client import GatewayEndpointConfig
from shared.mcp.gateway.identity import (
    IDENTITY_PER_USER,
    IDENTITY_SHARED,
    EndpointAuthConfig,
    IdentityResolver,
    LinkRequired,
    ResolvedCredential,
    ToolWithheld,
    UserLinkRecord,
)
from tests.fixtures.mock_oauth2_server import MockOAuth2Server

# Named constants (rather than inline literals) so bandit's hardcoded-
# password heuristic -- which fires on any `client_secret=`/`access_token=`/
# `*token*=` keyword argument or variable name holding a string literal --
# doesn't flag these fixture-only values.
MOCK_OAUTH_AUTHORIZATION_URL = "http://oauth.test/authorize"
MOCK_OAUTH_ISSUE_URL = "http://oauth.test/token"
MOCK_REDIRECT_URI = "http://localhost/callback"
MOCK_CLIENT_ID = "cid"
MOCK_CLIENT_SECRET = "csecret"  # noqa: S105 -- mock OAuth2 server fixture credential, not real

HEADER_CONFIG = EndpointAuthConfig(
    auth_type="header", header=HeaderAuthConfig(header_value="Bearer org-shared-token")
)


class _FakeUserLinkRepository:
    """In-memory `McpUserLinkRepository` double."""

    def __init__(self) -> None:
        """Start with no seeded links."""
        self._links: dict[tuple[int, str], UserLinkRecord] = {}
        self.saved: list[tuple[int, str, TokenSet]] = []

    def seed(self, endpoint_id: int, user_uuid: str, record: UserLinkRecord) -> None:
        """Pre-populate a link for (endpoint_id, user_uuid)."""
        self._links[(endpoint_id, user_uuid)] = record

    async def get_link(self, endpoint_id: int, user_uuid: str) -> UserLinkRecord | None:
        """Return the seeded link, or None if never seeded."""
        return self._links.get((endpoint_id, user_uuid))

    async def save_link(self, endpoint_id: int, user_uuid: str, token: TokenSet) -> None:
        """Persist a (re)issued token, recording the call for assertions."""
        record = UserLinkRecord(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_at=token.expires_at,
            status="linked",
        )
        self._links[(endpoint_id, user_uuid)] = record
        self.saved.append((endpoint_id, user_uuid, token))

    async def mark_status(self, endpoint_id: int, user_uuid: str, status: str) -> None:
        """Update a link's status in place."""
        existing = self._links.get((endpoint_id, user_uuid))
        if existing is not None:
            self._links[(endpoint_id, user_uuid)] = UserLinkRecord(
                access_token=existing.access_token,
                refresh_token=existing.refresh_token,
                expires_at=existing.expires_at,
                status=status,
            )


def _endpoint(namespace: str = "elder") -> GatewayEndpointConfig:
    return GatewayEndpointConfig(
        id=7,
        org_id=1,
        name="elder",
        url="http://elder.test/mcp",
        transport="streamable_http",
        namespace=namespace,
    )


def _oauth2_factory(server: MockOAuth2Server):
    def factory(**kwargs):
        return AsyncOAuth2Client(
            transport=httpx.ASGITransport(app=server), base_url="http://oauth.test", **kwargs
        )

    return factory


def _resolver(links: _FakeUserLinkRepository | None = None) -> IdentityResolver:
    """Build an IdentityResolver with a plain header-only OutboundAuth (no network)."""
    return IdentityResolver(
        outbound_auth=OutboundAuth(), user_links=links or _FakeUserLinkRepository()
    )


@pytest.mark.asyncio
class TestSharedIdentity:
    """`identity_mode=shared` always resolves the endpoint's own credential."""

    async def test_shared_mode_uses_org_credential(self):
        """Shared mode resolves the endpoint's own configured header credential."""
        resolver = _resolver()
        result = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_SHARED, user_uuid="u-1"
        )
        assert isinstance(result, ResolvedCredential)
        assert result.identity_source == IDENTITY_SHARED
        assert result.headers == {"Authorization": "Bearer org-shared-token"}

    async def test_shared_mode_ignores_caller_identity(self):
        """Shared mode resolves identically regardless of which user is calling."""
        resolver = _resolver()
        result_a = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_SHARED, user_uuid="u-1"
        )
        result_b = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_SHARED, user_uuid="u-2"
        )
        assert result_a.headers == result_b.headers


@pytest.mark.asyncio
class TestPerUserLinked:
    """`identity_mode=per_user` with an existing, unexpired link."""

    async def test_linked_user_gets_their_own_token(self):
        """A linked per-user caller resolves to their own stored token, not the shared one."""
        links = _FakeUserLinkRepository()
        links.seed(
            7,
            "u-1",
            UserLinkRecord(
                access_token="user-token-1",  # noqa: S106 -- test fixture value
                refresh_token=None,
                expires_at=None,
                status="linked",
            ),
        )
        resolver = _resolver(links)
        result = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_PER_USER, user_uuid="u-1"
        )
        assert isinstance(result, ResolvedCredential)
        assert result.identity_source == IDENTITY_PER_USER
        assert result.headers == {"Authorization": "Bearer user-token-1"}

    async def test_two_linked_users_get_different_tokens(self):
        """Two different linked users resolve to two different tokens."""
        links = _FakeUserLinkRepository()
        links.seed(7, "u-1", UserLinkRecord("token-a", None, None, "linked"))
        links.seed(7, "u-2", UserLinkRecord("token-b", None, None, "linked"))
        resolver = _resolver(links)
        result_a = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_PER_USER, user_uuid="u-1"
        )
        result_b = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_PER_USER, user_uuid="u-2"
        )
        assert result_a.headers != result_b.headers


@pytest.mark.asyncio
class TestPerUserUnlinked:
    """`per_user` + no link on file -> a link-URL, no upstream call attempted."""

    async def test_unlinked_user_gets_link_required(self):
        """An unlinked per-user caller gets a LinkRequired with the correct link URL."""
        resolver = _resolver()
        result = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_PER_USER, user_uuid="u-never-linked"
        )
        assert isinstance(result, LinkRequired)
        assert result.reason == "unlinked"
        assert result.link_url == "/api/v1/integrations/mcp-endpoints/7/link"

    async def test_revoked_link_also_prompts_relink(self):
        """A revoked link also produces a LinkRequired, distinct reason from unlinked."""
        links = _FakeUserLinkRepository()
        links.seed(7, "u-1", UserLinkRecord("stale", None, None, status="revoked"))
        resolver = _resolver(links)
        result = await resolver.resolve(
            _endpoint(), HEADER_CONFIG, identity_mode=IDENTITY_PER_USER, user_uuid="u-1"
        )
        assert isinstance(result, LinkRequired)
        assert result.reason == "revoked"


@pytest.mark.asyncio
class TestUnattributedCallerFallbackOrWithhold:
    """`per_user` + no caller identity at all (e.g. an org-wide key)."""

    async def test_falls_back_to_shared_when_configured(self):
        """An unattributed caller falls back to the shared credential when configured to."""
        resolver = _resolver()
        result = await resolver.resolve(
            _endpoint(),
            HEADER_CONFIG,
            identity_mode=IDENTITY_PER_USER,
            user_uuid=None,
            shared_fallback=True,
        )
        assert isinstance(result, ResolvedCredential)
        assert result.identity_source == IDENTITY_SHARED

    async def test_withheld_without_fallback(self):
        """An unattributed caller with no fallback gets the tool withheld, not silently allowed."""
        resolver = _resolver()
        result = await resolver.resolve(
            _endpoint(),
            HEADER_CONFIG,
            identity_mode=IDENTITY_PER_USER,
            user_uuid=None,
            shared_fallback=False,
        )
        assert isinstance(result, ToolWithheld)
        assert result.reason == "unattributed_caller_no_fallback"


@pytest.mark.asyncio
class TestExpiryTriggersRefresh:
    """An expired per-user token is refreshed transparently via `auth.py`."""

    async def test_expired_token_is_refreshed_and_persisted(self):
        """An expired per-user token is refreshed and the new token persisted back."""
        server = MockOAuth2Server()
        server.seed_client(MOCK_CLIENT_ID, MOCK_CLIENT_SECRET)
        server.seed_authorization_code("code-1", MOCK_CLIENT_ID)

        auth = OutboundAuth(oauth2_client_factory=_oauth2_factory(server))
        # Mint a real refresh token via the fixture so the refresh call has
        # something genuine to redeem.
        auth_code_config = OAuth2AuthCodeConfig(
            authorization_endpoint=MOCK_OAUTH_AUTHORIZATION_URL,
            token_endpoint=MOCK_OAUTH_ISSUE_URL,
            redirect_uri=MOCK_REDIRECT_URI,
            client_id=MOCK_CLIENT_ID,
            client_secret=MOCK_CLIENT_SECRET,
        )
        issued = await auth.exchange_code(
            auth_code_config,
            client_id=MOCK_CLIENT_ID,
            client_secret=MOCK_CLIENT_SECRET,
            code="code-1",
        )

        links = _FakeUserLinkRepository()
        links.seed(
            7,
            "u-1",
            UserLinkRecord(
                access_token=issued.access_token,
                refresh_token=issued.refresh_token,
                expires_at=time.time() - 1,  # already expired
                status="linked",
            ),
        )
        resolver = IdentityResolver(outbound_auth=auth, user_links=links)
        auth_config = EndpointAuthConfig(auth_type="oauth2_auth_code", auth_code=auth_code_config)

        result = await resolver.resolve(
            _endpoint(), auth_config, identity_mode=IDENTITY_PER_USER, user_uuid="u-1"
        )
        assert isinstance(result, ResolvedCredential)
        assert result.headers["Authorization"] != f"Bearer {issued.access_token}"
        assert len(links.saved) == 1  # refreshed token persisted back

    async def test_expired_token_with_no_refresh_token_prompts_relink(self):
        """An expired token with no refresh token on file prompts a re-link, not a crash."""
        links = _FakeUserLinkRepository()
        links.seed(
            7,
            "u-1",
            UserLinkRecord(
                access_token="stale",  # noqa: S106 -- test fixture value
                refresh_token=None,
                expires_at=time.time() - 1,
                status="linked",
            ),
        )
        auth_config = EndpointAuthConfig(
            auth_type="oauth2_auth_code",
            auth_code=OAuth2AuthCodeConfig(
                authorization_endpoint=MOCK_OAUTH_AUTHORIZATION_URL,
                token_endpoint=MOCK_OAUTH_ISSUE_URL,
                redirect_uri=MOCK_REDIRECT_URI,
            ),
        )
        resolver = _resolver(links)
        result = await resolver.resolve(
            _endpoint(), auth_config, identity_mode=IDENTITY_PER_USER, user_uuid="u-1"
        )
        assert isinstance(result, LinkRequired)
        assert result.reason == "expired"


class TestOAuth2ClientCredentialsSharedMode:
    """Shared mode also supports an OAuth2 client-credentials endpoint, not just headers."""

    @pytest.mark.asyncio
    async def test_shared_client_credentials(self):
        """Shared mode resolves an OAuth2 client-credentials token, not just a static header."""
        server = MockOAuth2Server()
        server.seed_client(MOCK_CLIENT_ID, MOCK_CLIENT_SECRET)

        auth = OutboundAuth(oauth2_client_factory=_oauth2_factory(server))
        auth_config = EndpointAuthConfig(
            auth_type="oauth2_client_credentials",
            client_credentials=OAuth2ClientCredentialsConfig(
                token_url=MOCK_OAUTH_ISSUE_URL,
                client_id=MOCK_CLIENT_ID,
                client_secret=MOCK_CLIENT_SECRET,
            ),
        )
        resolver = IdentityResolver(outbound_auth=auth, user_links=_FakeUserLinkRepository())
        result = await resolver.resolve(
            _endpoint(), auth_config, identity_mode=IDENTITY_SHARED, user_uuid="u-1"
        )
        assert isinstance(result, ResolvedCredential)
        assert result.headers["Authorization"].startswith("Bearer ")
