"""NVIDIA GPU collector using pynvml (NVML).

Requires: NVIDIA driver + NVML library. Works with Docker via --gpus=all.
Metrics exposed per device: utilization, memory_used, memory_total, power_watts,
power_limit_watts, temperature_celsius, core_clock_mhz, memory_clock_mhz, fan_speed_percent
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

try:
    import pynvml
except ImportError:
    pynvml = None  # type: ignore[assignment]

from gpu_exporter.metrics import GpuInfo, UnifiedGpuCollector

logger = logging.getLogger("gpu_exporter.nvidia")


@dataclass(frozen=True)
class NvmlDevice:
    index: int
    name: str
    uuid: str
    memory_total: int  # bytes
    bus_id: str = ""


def _init_nvml() -> bool:
    """Initialize NVML. Returns True if successful."""
    if pynvml is None:
        logger.warning("pynvml not installed — NVIDIA collector disabled")
        return False
    try:
        pynvml.nvmlInit()
        return True
    except Exception as exc:
        logger.warning("NVML init failed (%s) — NVIDIA collector disabled", exc)
        return False


def _shutdown_nvml():
    if pynvml is not None:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def enumerate_devices() -> list[NvmlDevice]:
    """List all NVIDIA GPUs visible to NVML."""
    if not pynvml or not _init_nvml():
        return []

    devices = []
    try:
        device_count = pynvml.nvmlDeviceGetCount()
        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle).decode("utf-8")
            uuid = pynvml.nvmlDeviceGetUUID(handle)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            devices.append(NvmlDevice(index=i, name=name, uuid=uuid, memory_total=mem_info.total))
    except Exception as exc:
        logger.warning("Failed to enumerate NVIDIA GPUs: %s", exc)
    finally:
        _shutdown_nvml()

    return devices


class NvidiaCollector(UnifiedGpuCollector):
    """Collects metrics from all NVIDIA GPUs via NVML."""

    def __init__(self, labels: dict[str, str] | None = None):
        super().__init__(labels or {})
        self._initialized = False
        self._devices: list[NvmlDevice] = []

    def initialize(self) -> bool:
        """Try to init NVML and enumerate devices. Returns True if any GPU found."""
        if not pynvml:
            logger.info("pynvml unavailable — skipping NVIDIA collector")
            return False

        if not _init_nvml():
            return False

        self._devices = enumerate_devices()
        if not self._devices:
            logger.info("No NVIDIA GPUs found via NVML")
            _shutdown_nvml()
            return False

        logger.info("Found %d NVIDIA GPU(s)", len(self._devices))
        for dev in self._devices:
            logger.info("  [%d] %s (UUID: %s, VRAM: %.0f MiB)",
                        dev.index, dev.name, dev.uuid, dev.memory_total / 1024**2)

        self._initialized = True
        return True

    def collect(self):
        """Yield all GPU metrics for all NVIDIA devices."""
        if not self._initialized or not pynvml:
            return

        try:
            for dev in self._devices:
                handle = pynvml.nvmlDeviceGetHandleByIndex(dev.index)

                # Utilization (%)
                util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                yield from self._gauge_sample(
                    "utilization", "percent", dev, float(util.gpu),
                )

                # Memory (bytes)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                yield from self._gauge_sample("memory_used", "bytes", dev, float(mem_info.used))
                yield from self._gauge_sample("memory_total", "bytes", dev, float(dev.memory_total))

                # Power (Watts)
                try:
                    power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # mW -> W
                    yield from self._gauge_sample("power_watts", "watts", dev, power)

                    power_limit = pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000.0
                    yield from self._gauge_sample("power_limit_watts", "watts", dev, power_limit)
                except Exception:
                    pass  # Some GPUs don't support power metrics

                # Temperature (Celsius)
                try:
                    temp = pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU)
                    yield from self._gauge_sample("temperature_celsius", "celsius", dev, float(temp))
                except Exception:
                    pass

                # Clock speeds (MHz)
                try:
                    clocks = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_GRAPHICS)
                    yield from self._gauge_sample("core_clock_mhz", "mhz", dev, float(clocks))
                except Exception:
                    pass

                try:
                    mem_clock = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
                    yield from self._gauge_sample("memory_clock_mhz", "mhz", dev, float(mem_clock))
                except Exception:
                    pass

                # Fan speed (%)
                try:
                    fan = pynvml.nvmlDeviceGetFanSpeed(handle)
                    yield from self._gauge_sample("fan_speed_percent", "percent", dev, float(fan))
                except Exception:
                    pass  # Passive cooling GPUs may not have fans

        except Exception as exc:
            logger.error("Error collecting NVIDIA metrics: %s", exc)

    def _gauge_sample(self, metric_name: str, unit: str, gpu: GpuInfo, value: float):
        """Yield a single gauge sample."""
        key = f"{gpu.vendor}_{metric_name}"
        if key not in self.gauges:
            from prometheus_client.core import GaugeMetricFamily
            help_text = f"GPU {gpu.vendor} {metric_name} ({unit})"
            g = GaugeMetricFamily(
                key, help_text,
                labels=["gpu_vendor", "gpu_index", "gpu_name"],
            )
            self.gauges[key] = g

        self.gauges[key].add_metric(
            [gpu.vendor, str(gpu.index), gpu.name], value,
        )
        yield self.gauges[key]


def shutdown():
    _shutdown_nvml()
