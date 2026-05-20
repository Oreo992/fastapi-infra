import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable, Mapping
from typing import Any

from infra.exceptions.base import AuthenticationError, AuthorizationError
from infra.plugins.auth.models import HashedApiKeyRecord, JwtSigningKeyRecord, Principal

API_KEY_HASH_ALGORITHM = "pbkdf2_sha256"
API_KEY_HASH_ITERATIONS = 260_000
API_KEY_HASH_SALT_BYTES = 16


def hash_api_key(api_key: str, *, salt: bytes | str | None = None) -> str:
    salt_bytes = _salt_bytes(salt)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        salt_bytes,
        API_KEY_HASH_ITERATIONS,
    )
    return "$".join(
        [
            API_KEY_HASH_ALGORITHM,
            str(API_KEY_HASH_ITERATIONS),
            _b64_encode(salt_bytes),
            _b64_encode(digest),
        ]
    )


def verify_api_key_hash(api_key: str, encoded_hash: str) -> bool:
    try:
        algorithm, encoded_iterations, encoded_salt, encoded_digest = encoded_hash.split("$")
        if algorithm != API_KEY_HASH_ALGORITHM:
            return False
        iterations = int(encoded_iterations)
        if iterations <= 0:
            return False
        salt = _b64_decode(encoded_salt)
        expected_digest = _b64_decode(encoded_digest)
    except (binascii.Error, ValueError, TypeError):
        return False

    actual_digest = hashlib.pbkdf2_hmac(
        "sha256",
        api_key.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual_digest, expected_digest)


class AuthService:
    def __init__(
        self,
        hashed_api_keys: Iterable[HashedApiKeyRecord] | None = None,
        jwt_secret: str | None = None,
        jwt_signing_keys: Iterable[JwtSigningKeyRecord] | None = None,
        jwt_key_id: str | None = None,
        jwt_issuer: str | None = None,
        jwt_audience: str | None = None,
        access_token_ttl_seconds: int = 3600,
    ) -> None:
        self._hashed_api_keys: list[HashedApiKeyRecord] = list(hashed_api_keys or [])
        self._jwt_signing_keys = self._build_jwt_keyring(jwt_secret, jwt_signing_keys)
        self._jwt_key_id = self._resolve_active_jwt_key_id(jwt_key_id)
        self._jwt_issuer = jwt_issuer
        self._jwt_audience = jwt_audience
        self._access_token_ttl_seconds = access_token_ttl_seconds

    def add_hashed_api_key(self, record: HashedApiKeyRecord) -> None:
        self._hashed_api_keys.append(record)

    def authenticate_api_key(self, api_key: str | None) -> Principal:
        if not api_key:
            raise AuthenticationError()

        for hashed_record in self._hashed_api_keys:
            if verify_api_key_hash(api_key, hashed_record.key_hash):
                return self._principal_from_api_key_record(hashed_record)

        raise AuthenticationError()

    def _principal_from_api_key_record(self, record: HashedApiKeyRecord) -> Principal:
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
        key_id: str | None = None,
    ) -> str:
        signing_key_id = key_id or self._jwt_key_id
        signing_secret = self._jwt_signing_secret(signing_key_id)
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

        header = {"alg": "HS256", "kid": signing_key_id, "typ": "JWT"}
        signing_input = ".".join(
            [
                self._b64url_json(header),
                self._b64url_json(payload),
            ]
        )
        signature = self._sign(signing_input, signing_secret)
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
        self._require_jwt_signing_keys()
        if not token:
            raise AuthenticationError()

        try:
            encoded_header, encoded_payload, signature = token.split(".")
        except ValueError as exc:
            raise AuthenticationError() from exc

        header = self._decode_json(encoded_header)
        if header.get("alg") != "HS256":
            raise AuthenticationError()
        signing_secret = self._jwt_secret_for_header(header)

        signing_input = f"{encoded_header}.{encoded_payload}"
        expected_signature = self._sign(signing_input, signing_secret)
        if not hmac.compare_digest(signature, expected_signature):
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

    def _require_jwt_signing_keys(self) -> None:
        if not self._jwt_signing_keys:
            raise AuthenticationError()

    def _sign(self, signing_input: str, secret: str) -> str:
        digest = hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return self._b64url_encode(digest)

    @staticmethod
    def _build_jwt_keyring(
        jwt_secret: str | None,
        jwt_signing_keys: Iterable[JwtSigningKeyRecord] | None,
    ) -> dict[str, str]:
        keyring: dict[str, str] = {}
        if jwt_secret:
            keyring["default"] = jwt_secret
        for record in jwt_signing_keys or []:
            key_id = record.key_id or "default"
            if not record.secret:
                raise AuthenticationError()
            if key_id in keyring:
                raise ValueError(f"duplicate JWT signing key id: {key_id}")
            keyring[key_id] = record.secret
        return keyring

    def _resolve_active_jwt_key_id(self, jwt_key_id: str | None) -> str | None:
        if not self._jwt_signing_keys:
            return None
        if jwt_key_id is None:
            return next(iter(self._jwt_signing_keys))
        if jwt_key_id not in self._jwt_signing_keys:
            raise ValueError(f"unknown JWT signing key id: {jwt_key_id}")
        return jwt_key_id

    def _jwt_signing_secret(self, key_id: str | None) -> str:
        self._require_jwt_signing_keys()
        if key_id is None or key_id not in self._jwt_signing_keys:
            raise AuthenticationError()
        return self._jwt_signing_keys[key_id]

    def _jwt_secret_for_header(self, header: Mapping[str, Any]) -> str:
        key_id = header.get("kid")
        if isinstance(key_id, str) and key_id:
            return self._jwt_signing_secret(key_id)
        if len(self._jwt_signing_keys) == 1:
            return next(iter(self._jwt_signing_keys.values()))
        raise AuthenticationError()

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


def _salt_bytes(salt: bytes | str | None) -> bytes:
    if salt is None:
        return secrets.token_bytes(API_KEY_HASH_SALT_BYTES)
    if isinstance(salt, bytes):
        return salt
    return salt.encode("utf-8")


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64_decode(encoded: str) -> bytes:
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))
