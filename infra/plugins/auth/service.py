import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from infra.exceptions.base import AuthenticationError, AuthorizationError
from infra.plugins.auth.models import ApiKeyRecord, Principal


class AuthService:
    def __init__(
        self,
        api_keys: Mapping[str, ApiKeyRecord] | None = None,
        jwt_secret: str | None = None,
        jwt_issuer: str | None = None,
        jwt_audience: str | None = None,
        access_token_ttl_seconds: int = 3600,
    ) -> None:
        self._api_keys: dict[str, ApiKeyRecord] = dict(api_keys or {})
        self._jwt_secret = jwt_secret
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience
        self._access_token_ttl_seconds = access_token_ttl_seconds

    def add_api_key(self, key: str, record: ApiKeyRecord) -> None:
        self._api_keys[key] = record

    def authenticate_api_key(self, api_key: str | None) -> Principal:
        if not api_key:
            raise AuthenticationError()

        record = self._api_keys.get(api_key)
        if record is None:
            raise AuthenticationError()

        return Principal(
            subject=record.subject,
            scopes=set(record.scopes),
            roles=set(record.roles),
            claims=dict(record.claims),
        )

    def issue_jwt(
        self,
        subject: str,
        scopes: Iterable[str],
        roles: Iterable[str],
        claims: Mapping[str, Any] | None = None,
        expires_in_seconds: int | None = None,
    ) -> str:
        self._require_jwt_secret()
        now = int(time.time())
        ttl = self._access_token_ttl_seconds if expires_in_seconds is None else expires_in_seconds
        payload: dict[str, Any] = dict(claims or {})
        payload.update(
            {
                "sub": subject,
                "scopes": sorted(set(scopes)),
                "roles": sorted(set(roles)),
                "iat": now,
                "exp": now + ttl,
            }
        )
        if self._jwt_issuer is not None:
            payload["iss"] = self._jwt_issuer
        if self._jwt_audience is not None:
            payload["aud"] = self._jwt_audience

        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = ".".join(
            [
                self._b64url_json(header),
                self._b64url_json(payload),
            ]
        )
        signature = self._sign(signing_input)
        return f"{signing_input}.{signature}"

    def authenticate_bearer(self, authorization_header: str | None) -> Principal:
        if not authorization_header:
            raise AuthenticationError()

        token = authorization_header.strip()
        scheme, separator, credentials = token.partition(" ")
        if separator and scheme.lower() == "bearer":
            token = credentials.strip()
        elif separator:
            raise AuthenticationError()

        return self.authenticate_jwt(token)

    def authenticate_jwt(self, token: str | None) -> Principal:
        self._require_jwt_secret()
        if not token:
            raise AuthenticationError()

        try:
            encoded_header, encoded_payload, signature = token.split(".")
        except ValueError as exc:
            raise AuthenticationError() from exc

        signing_input = f"{encoded_header}.{encoded_payload}"
        expected_signature = self._sign(signing_input)
        if not hmac.compare_digest(signature, expected_signature):
            raise AuthenticationError()

        header = self._decode_json(encoded_header)
        if header.get("alg") != "HS256":
            raise AuthenticationError()

        payload = self._decode_json(encoded_payload)
        subject = payload.get("sub")
        expires_at = payload.get("exp")
        if not isinstance(subject, str) or not subject:
            raise AuthenticationError()
        if not isinstance(expires_at, int | float) or int(time.time()) >= expires_at:
            raise AuthenticationError()
        if self._jwt_issuer is not None and payload.get("iss") != self._jwt_issuer:
            raise AuthenticationError()
        if self._jwt_audience is not None and not self._audience_matches(payload.get("aud")):
            raise AuthenticationError()

        return Principal(
            subject=subject,
            scopes=self._string_set(payload.get("scopes")),
            roles=self._string_set(payload.get("roles")),
            claims=dict(payload),
        )

    def require_scopes(self, principal: Principal, required: Iterable[str]) -> None:
        missing = set(required) - principal.scopes
        if missing:
            raise AuthorizationError()

    def require_roles(self, principal: Principal, required: Iterable[str]) -> None:
        missing = set(required) - principal.roles
        if missing:
            raise AuthorizationError()

    def _require_jwt_secret(self) -> None:
        if not self._jwt_secret:
            raise AuthenticationError()

    def _sign(self, signing_input: str) -> str:
        self._require_jwt_secret()
        digest = hmac.new(
            self._jwt_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return self._b64url_encode(digest)

    @staticmethod
    def _b64url_json(value: Mapping[str, Any]) -> str:
        data = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return AuthService._b64url_encode(data)

    @staticmethod
    def _b64url_encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_json(encoded: str) -> dict[str, Any]:
        try:
            padded = encoded + "=" * (-len(encoded) % 4)
            data = base64.urlsafe_b64decode(padded.encode("ascii"))
            value = json.loads(data.decode("utf-8"))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AuthenticationError() from exc
        if not isinstance(value, dict):
            raise AuthenticationError()
        return value

    def _audience_matches(self, audience: Any) -> bool:
        if isinstance(audience, str):
            return audience == self._jwt_audience
        if isinstance(audience, list):
            return self._jwt_audience in audience
        return False

    @staticmethod
    def _string_set(value: Any) -> set[str]:
        if value is None:
            return set()
        if isinstance(value, str):
            return {value}
        if isinstance(value, list):
            return {item for item in value if isinstance(item, str)}
        return set()
