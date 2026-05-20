import pytest

from infra.plugins.provider_extensions import (
    external_provider_names_to_load,
    load_entry_point_provider,
)


class FakeEntryPoint:
    def __init__(self, name: str, loaded: object | None = None) -> None:
        self.name = name
        self.loaded = loaded

    def load(self) -> object:
        return self.loaded


class ValidProvider:
    name = "custom"

    def chat(self) -> None:
        return None

    def embed(self) -> None:
        return None


class MissingMethodProvider:
    name = "custom"

    def chat(self) -> None:
        return None


def test_external_provider_names_to_load_returns_sorted_external_names(monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeEntryPoint("custom_b"), FakeEntryPoint("custom_a")],
    )

    assert external_provider_names_to_load(
        provider_kind="ai",
        requested_names={"mock", "custom_b", "custom_a"},
        registered_names={"mock"},
        entry_point_group="fastapi_infra.ai_providers",
    ) == ["custom_a", "custom_b"]


def test_external_provider_names_to_load_rejects_unknown_names(monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeEntryPoint("custom")],
    )

    with pytest.raises(ValueError, match="unknown payment provider: missing"):
        external_provider_names_to_load(
            provider_kind="payment",
            requested_names={"mock", "missing"},
            registered_names={"mock"},
            entry_point_group="fastapi_infra.payment_providers",
        )


def test_load_entry_point_provider_returns_valid_provider(monkeypatch):
    def provider_factory(config):
        provider = ValidProvider()
        provider.config = dict(config)
        return provider

    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeEntryPoint("custom", provider_factory)],
    )

    provider = load_entry_point_provider(
        "fastapi_infra.ai_providers",
        "custom",
        {"api_key": "sk-custom"},
        required_methods=("chat", "embed"),
    )

    assert isinstance(provider, ValidProvider)
    assert provider.config == {"api_key": "sk-custom"}


def test_load_entry_point_provider_rejects_non_callable_factory(monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeEntryPoint("custom", object())],
    )

    with pytest.raises(
        ValueError,
        match="fastapi_infra.ai_providers:custom must load a provider factory",
    ):
        load_entry_point_provider("fastapi_infra.ai_providers", "custom", {})


def test_load_entry_point_provider_rejects_mismatched_provider_name(monkeypatch):
    class WrongNameProvider:
        name = "wrong"

    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeEntryPoint("custom", lambda config: WrongNameProvider())],
    )

    with pytest.raises(
        ValueError,
        match="fastapi_infra.ai_providers:custom returned provider named 'wrong'",
    ):
        load_entry_point_provider("fastapi_infra.ai_providers", "custom", {})


def test_load_entry_point_provider_rejects_missing_required_methods(monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeEntryPoint("custom", lambda config: MissingMethodProvider())],
    )

    with pytest.raises(
        ValueError,
        match=(
            "fastapi_infra.ai_providers:custom provider is missing required "
            r"method\(s\): embed, stream_chat"
        ),
    ):
        load_entry_point_provider(
            "fastapi_infra.ai_providers",
            "custom",
            {},
            required_methods=("chat", "embed", "stream_chat"),
        )


def test_load_entry_point_provider_rejects_unknown_entry_point(monkeypatch):
    monkeypatch.setattr(
        "infra.plugins.provider_extensions.entry_points",
        lambda group: [FakeEntryPoint("other", lambda config: ValidProvider())],
    )

    with pytest.raises(
        LookupError,
        match="unknown provider entry point: fastapi_infra.ai_providers:custom",
    ):
        load_entry_point_provider("fastapi_infra.ai_providers", "custom", {})
