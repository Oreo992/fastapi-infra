from collections.abc import Iterable, Mapping

from infra.exceptions.base import AuthenticationError, AuthorizationError
from infra.plugins.auth.models import ApiKeyRecord, Principal


class AuthService:
    def __init__(self, api_keys: Mapping[str, ApiKeyRecord] | None = None) -> None:
        self._api_keys: dict[str, ApiKeyRecord] = dict(api_keys or {})

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
            claims=dict(record.claims),
        )

    def require_scopes(self, principal: Principal, required: Iterable[str]) -> None:
        missing = set(required) - principal.scopes
        if missing:
            raise AuthorizationError()
