"""Auth/scope foundation for standalone penguincode.

Exposes ``ScopeContext`` (tenant/org/team/user/scope, derived once per
request from a validated WaddleAI JWT) and the middleware that produces it
for both surfaces penguincode exposes: the gRPC server and the Quart REST
API. Self-contained -- validates via a public key/JWKS supplied through env,
never by importing management/shared.auth internals (see spec section 8).
"""
