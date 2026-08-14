"""WaddleAI as an MCP *client* -- the external-MCP gateway (§11.4).

Submodules:

* ``client`` -- ``GatewayClient``, connect/discover/invoke against one
  external MCP endpoint over streamable-HTTP or stdio.
* ``auth`` -- ``OutboundAuth``, outbound credential handling (static
  header, OAuth2 client-credentials, OAuth2 authorization-code + dynamic
  client registration).
* ``identity`` -- ``IdentityResolver``, per-endpoint ``shared``/``per_user``
  caller-identity resolution, link-URL issuance, fallback/withhold.
* ``aggregator`` -- ``GatewayAggregator``, namespacing, collision
  handling, and the per-tool security policy chokepoint for external
  tool calls.
"""
