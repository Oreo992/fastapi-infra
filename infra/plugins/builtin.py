from infra.plugins.contract import InfraPlugin
from infra.plugins.ai import AIPlugin
from infra.plugins.auth import AuthPlugin
from infra.plugins.cache import CachePlugin
from infra.plugins.database import DatabasePlugin
from infra.plugins.http import HTTPPlugin
from infra.plugins.notifications import NotificationsPlugin
from infra.plugins.observability import ObservabilityPlugin
from infra.plugins.payment import PaymentPlugin
from infra.plugins.ratelimit import RateLimitPlugin
from infra.plugins.storage import StoragePlugin
from infra.plugins.tasks import TasksPlugin
from infra.plugins.webhooks import WebhooksPlugin


def get_builtin_plugins() -> list[InfraPlugin]:
    return [
        AIPlugin(),
        AuthPlugin(),
        DatabasePlugin(),
        CachePlugin(),
        HTTPPlugin(),
        ObservabilityPlugin(),
        TasksPlugin(),
        StoragePlugin(),
        WebhooksPlugin(),
        PaymentPlugin(),
        RateLimitPlugin(),
        NotificationsPlugin(),
    ]
