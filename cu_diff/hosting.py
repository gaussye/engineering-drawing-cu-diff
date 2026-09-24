"""Explicit App Service boundary; Easy Auth must validate and inject identity headers."""

import base64
import binascii
from dataclasses import dataclass
import json
from urllib.parse import urlsplit
from uuid import UUID


def principal_id(value):
    if not isinstance(value, str):
        raise ValueError("Principal and tenant identifiers must be UUID strings")
    return str(UUID(value))


@dataclass(frozen=True)
class CloudBoundary:
    origin: str
    tenant: str
    allowed_principals: frozenset[str]

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (url.scheme != "https" or not url.hostname or url.path or url.query or url.fragment
                or url.username or url.password or url.port not in (None, 443)
                or url.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ValueError("Cloud origin must be a public HTTPS origin without path or credentials")
        if not isinstance(self.allowed_principals, (set, frozenset)) or not self.allowed_principals:
            raise ValueError("Cloud access needs a nonempty principal allowlist")
        object.__setattr__(self, "tenant", principal_id(self.tenant))
        object.__setattr__(self, "allowed_principals",
                           frozenset(principal_id(p) for p in self.allowed_principals))

    @property
    def host(self):
        return urlsplit(self.origin).netloc

    def authenticate(self, header):
        if not isinstance(header, str) or not header or len(header) > 16384:
            raise ValueError("Missing or invalid Easy Auth principal")
        try:
            value = json.loads(base64.b64decode(header, validate=True))
        except (ValueError, UnicodeError, binascii.Error) as error:
            raise ValueError("Invalid Easy Auth principal encoding") from error
        if not isinstance(value, dict) or value.get("auth_typ") != "aad":
            raise ValueError("Microsoft Entra authentication is required")
        claims = value.get("claims")
        if not isinstance(claims, list) or any(not isinstance(c, dict) for c in claims):
            raise ValueError("Invalid Easy Auth claims")
        tenants = {principal_id(c.get("val")) for c in claims if c.get("typ") in (
            "tid", "http://schemas.microsoft.com/identity/claims/tenantid")}
        identities = {principal_id(c.get("val")) for c in claims if c.get("typ") in (
            "oid", "http://schemas.microsoft.com/identity/claims/objectidentifier")}
        if tenants != {self.tenant} or len(identities) != 1:
            raise ValueError("Unexpected tenant or ambiguous principal")
        identity = next(iter(identities))
        if identity not in self.allowed_principals:
            raise ValueError("Principal is not authorized for this application")
        return identity
