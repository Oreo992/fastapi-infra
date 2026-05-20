from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ServiceKey(Generic[T]):
    name: str
    service_type: type[T] | None = None

    def __post_init__(self) -> None:
        normalized = self.name.strip()
        if not normalized:
            raise ValueError("service name must not be empty")
        object.__setattr__(self, "name", normalized)

    def validate(self, service: object) -> T:
        if self.service_type is not None and not isinstance(service, self.service_type):
            raise RuntimeError(
                f"infra service has unexpected type: {self.name} "
                f"(expected {self.service_type.__name__})"
            )
        return service  # type: ignore[return-value]
