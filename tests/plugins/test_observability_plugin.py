import pytest

from infra.config.models import InfraSettings
from infra.core.health import HealthState
from infra.plugins.manager import PluginManager
from infra.plugins.observability import ObservabilityPlugin, ObservabilityService


@pytest.mark.asyncio
async def test_observability_plugin_registers_service_and_reads_health_snapshot():
    settings = InfraSettings(infra={"plugins": {"observability": {"enabled": True}}})
    manager = PluginManager(settings=settings, plugins=[ObservabilityPlugin()])

    await manager.startup()

    service = manager.get("observability")
    assert isinstance(service, ObservabilityService)

    service.increment("requests_total")
    service.increment("requests_total", 2)
    service.timing("request_seconds", 0.125)
    service.event("startup", {"source": "test"})

    assert service.counters["requests_total"] == 3
    assert service.timers["request_seconds"] == [0.125]
    assert service.events[0].name == "startup"
    assert service.events[0].payload == {"source": "test"}

    snapshot = service.health_snapshot()
    assert snapshot["observability"].status is HealthState.HEALTHY

    await manager.shutdown()


@pytest.mark.asyncio
async def test_observability_plugin_can_register_prometheus_metrics_backend():
    pytest.importorskip("prometheus_client")
    settings = InfraSettings(
        infra={
            "plugins": {
                "observability": {
                    "enabled": True,
                    "config": {"metrics_backend": "prometheus"},
                }
            }
        }
    )
    manager = PluginManager(settings=settings, plugins=[ObservabilityPlugin()])

    await manager.startup()

    service = manager.get("observability")
    assert isinstance(service, ObservabilityService)
    service.increment("requests_total")
    assert "# TYPE requests_total counter" in (service.render_metrics() or "")

    await manager.shutdown()
