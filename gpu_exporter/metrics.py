"""Unified Prometheus metrics schema for GPU and CPU monitoring.

Metric naming convention:
  gpu_<vendor>_<device_index>_<metric_name>
  cpu_core_<core_id>_<metric_name>
  cpu_package_<package_id>_<metric_name>

All vendors expose the same metric names so Grafana dashboards work across hardware.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import CollectorRegistry

logger = logging.getLogger("gpu_exporter.metrics")


@dataclass(frozen=True)
class GpuInfo:
    """Describes a single GPU device."""
    vendor: str  # 'nvidia', 'amd', 'intel'
    index: int
    name: str
    uuid: str = ""
    bus_id: str = ""


@dataclass(frozen=True)
class CpuInfo:
    """Describes a CPU package/core."""
    package_id: int
    core_count: int
    model: str = ""


# ── Metric definitions ───────────────────────────────────────────────

GPU_METRICS = [
    # Utilization (%)
    ("utilization", "percent"),
    # Memory (MiB)
    ("memory_used", "bytes"),
    ("memory_total", "bytes"),
    # Power (Watts)
    ("power_watts", "watts"),
    ("power_limit_watts", "watts"),
    # Temperature (Celsius)
    ("temperature_celsius", "celsius"),
    # Clock speeds (MHz)
    ("core_clock_mhz", "mhz"),
    ("memory_clock_mhz", "mhz"),
    # Fan speed (%)
    ("fan_speed_percent", "percent"),
]

CPU_METRICS = [
    # CPU utilization per core (%)
    ("cpu_utilization_core", "percent"),
    # Package power (Watts) via RAPL/hwmon
    ("package_power_watts", "watts"),
    # Temperature (Celsius) — package and per-core where available
    ("temperature_celsius_package", "celsius"),
    ("temperature_celsius_core", "celsius"),
]


class GpuCollector:
    """Prometheus Collector that exposes unified GPU metrics."""

    def __init__(self, labels: dict[str, str]):
        self.labels = labels or {}

    def collect(self) -> GaugeMetricFamily | list[GaugeMetricFamily]:
        yield from ()  # override in subclass


class UnifiedGpuCollector(CollectorRegistry):
    """Aggregates all vendor collectors into a single Prometheus scrape."""

    gauges: dict[str, GaugeMetricFamily] = field(default_factory=dict)

    def __init__(self, labels: dict[str, str]):
        self.labels = labels or {}
        super().__init__()

    def register_gauge(self, name: str, unit: str, vendor: str):
        """Register a gauge for a specific GPU device."""
        key = f"{vendor}_{name}"
        if key not in self.gauges:
            help_text = f"GPU {vendor} {name} ({unit})"
            g = GaugeMetricFamily(
                name,
                help_text,
                labels=["gpu_vendor", "gpu_index", "gpu_name"],
            )
            self.gauges[key] = g

    def set_value(self, gpu: GpuInfo, metric_name: str, unit: str, value: float):
        """Set a single gauge value for a GPU device."""
        key = f"{gpu.vendor}_{metric_name}"
        if key not in self.gauges:
            help_text = f"GPU {gpu.vendor} {metric_name} ({unit})"
            g = GaugeMetricFamily(
                key,
                help_text,
                labels=["gpu_vendor", "gpu_index", "gpu_name"],
            )
            self.gauges[key] = g

        self.gauges[key].add_metric(
            [gpu.vendor, str(gpu.index), gpu.name],
            value,
        )


class UnifiedCpuCollector:
    """Prometheus Collector for CPU metrics."""

    def __init__(self, labels: dict[str, str]):
        self.labels = labels or {}

    def collect(self) -> list[GaugeMetricFamily]:
        yield from ()  # override in subclass


def build_registry(
    gpu_collector: UnifiedGpuCollector | None = None,
    cpu_collector: UnifiedCpuCollector | None = None,
) -> CollectorRegistry:
    """Build a Prometheus registry with all collectors."""
    registry = CollectorRegistry()

    if gpu_collector is not None:
        for metric in gpu_collector.collect():
            metric.register(registry)

    if cpu_collector is not None:
        for metric in cpu_collector.collect():
            metric.register(registry)

    return registry
