from typing import Any


class AppException(Exception):
    def __init__(
        self,
        message: str,
        error_code: str = "INTERNAL_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(message)


class ConfigurationError(AppException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, "CONFIGURATION_ERROR", details)


class PluginError(AppException):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, "PLUGIN_ERROR", details)


class AuthenticationError(AppException):
    def __init__(self, message: str = "authentication failed") -> None:
        super().__init__(message, "UNAUTHORIZED")


class AuthorizationError(AppException):
    def __init__(self, message: str = "permission denied") -> None:
        super().__init__(message, "FORBIDDEN")


class ExternalServiceError(AppException):
    def __init__(self, service_name: str, message: str) -> None:
        super().__init__(
            message=f"{service_name}: {message}",
            error_code="EXTERNAL_SERVICE_ERROR",
            details={"service_name": service_name},
        )
