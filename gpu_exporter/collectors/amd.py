"""AMD GPU collector using pyrsmi (amdsmi).

Requires: ROCm driver + amdsmi library. Works in Docker with --device=/dev/kfd --device=/dev/dri.
Metrics exposed per device: utilization, memory_used, memory_total, power_watts,
power_limit_watts, temperature_celsius, core_clock_mhz, memory_clock_mhz, fan_speed_percent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

try:
    from pyrsmi import rocml as amdsmi
except ImportError:
    amdsmi = None  # type: ignore[assignment]

from gpu_exporter.metrics import GpuInfo, UnifiedGpuCollector

logger = logging.getLogger("gpu_exporter.amd")


@dataclass(frozen=True)
class AmdgpuDevice:
    index: int
    name: str
    uuid: str = ""
    memory_total: int = 0  # bytes
    bus_id: str = ""


def _init_amdsmi() -> bool:
    """Initialize AMD SMI. Returns True if successful."""
    if amdsmi is None:
        logger.warning("pyrsmi not installed — AMD collector disabled")
        return False
    try:
        amdsmi.smi_initialize()
        return True
    except Exception as exc:
        logger.warning("AMD SMI init failed (%s) — AMD collector disabled", exc)
        return False


def _shutdown_amdsmi():
    if amdsmi is not None:
        try:
            amdsmi.smi_shutdown()
        except Exception:
            pass


def enumerate_devices() -> list[AmdgpuDevice]:
    """List all AMD GPUs visible to pyrsmi."""
    if not amdsmi or not _init_amdsmi():
        return []

    devices = []
    try:
        device_count = amdsmi.smi_get_device_count()
        for i in range(device_count):
            # Get GPU name from sysfs as fallback
            try:
                with open(f"/sys/class/drm/card{i}/device/name", "r") as f:
                    name = f.read().strip()
            except (FileNotFoundError, PermissionError):
                name = f"AMD GPU {i}"

            # Get UUID from sysfs if available
            uuid = ""
            try:
                with open(f"/sys/class/drm/card{i}/device/uuid", "r") as f:
                    uuid = f.read().strip()
            except (FileNotFoundError, PermissionError):
                pass

            devices.append(AmdgpuDevice(index=i, name=name, uuid=uuid))
    except Exception as exc:
        logger.warning("Failed to enumerate AMD GPUs: %s", exc)
    finally:
        _shutdown_amdsmi()

    return devices


class AmdCollector(UnifiedGpuCollector):
    """Collects metrics from all AMD GPUs via pyrsmi."""

    def __init__(self, labels: dict[str, str] | None = None):
        super().__init__(labels or {})
        self._initialized = False
        self._devices: list[AmdgpuDevice] = []

    def initialize(self) -> bool:
        """Try to init AMD SMI and enumerate devices. Returns True if any GPU found."""
        if not amdsmi:
            logger.info("pyrsmi unavailable — skipping AMD collector")
            return False

        if not _init_amdsmi():
            return False

        self._devices = enumerate_devices()
        if not self._devices:
            logger.info("No AMD GPUs found via pyrsmi")
            _shutdown_amdsmi()
            return False

        logger.info("Found %d AMD GPU(s)", len(self._devices))
        for dev in self._devices:
            logger.info("  [%d] %s (UUID: %s)", dev.index, dev.name, dev.uuid)

        self._initialized = True
        return True

    def collect(self):
        """Yield all GPU metrics for all AMD devices."""
        if not self._initialized or not amdsmi:
            return

        try:
            device_count = amdsmi.smi_get_device_count()
            for i in range(device_count):
                gpu_info = GpuInfo(vendor="amd", index=i, name=self._devices[i].name)

                # Utilization (%) — GPU compute
                try:
                    util = amdsmi.smi_get_property(i, "GPU_UTILIZATION")
                    if util is not None and isinstance(util, (int, float)):
                        yield from self._gauge_sample("utilization", "percent", gpu_info, float(util))
                except Exception as exc:
                    logger.debug("AMD GPU %d utilization read failed: %s", i, exc)

                # Memory
                try:
                    mem_used = amdsmi.smi_get_property(i, "MEMORY_USED")
                    if mem_used is not None and isinstance(mem_used, (int, float)):
                        yield from self._gauge_sample("memory_used", "bytes", gpu_info, float(mem_used))

                    mem_total = amdsmi.smi_get_property(i, "MEMORY_TOTAL")
                    if mem_total is not None and isinstance(mem_total, (int, float)):
                        yield from self._gauge_sample("memory_total", "bytes", gpu_info, float(mem_total))
                except Exception as exc:
                    logger.debug("AMD GPU %d memory read failed: %s", i, exc)

                # Power (Watts)
                try:
                    power = amdsmi.smi_get_property(i, "POWER")
                    if power is not None and isinstance(power, (int, float)):
                        yield from self._gauge_sample("power_watts", "watts", gpu_info, float(power))

                    power_limit = amdsmi.smi_get_property(i, "POWER_LIMIT")
                    if power_limit is not None and isinstance(power_limit, (int, float)):
                        yield from self._gauge_sample("power_limit_watts", "watts", gpu_info, float(power_limit))
                except Exception as exc:
                    logger.debug("AMD GPU %d power read failed: %s", i, exc)

                # Temperature (Celsius)
                try:
                    temp = amdsmi.smi_get_property(i, "TEMPERATURE")
                    if temp is not None and isinstance(temp, (int, float)):
                        yield from self._gauge_sample("temperature_celsius", "celsius", gpu_info, float(temp))
                except Exception as exc:
                    logger.debug("AMD GPU %d temperature read failed: %s", i, exc)

                # Clock speeds (MHz)
                try:
                    core_clock = amdsmi.smi_get_property(i, "CURRENT_CLK")
                    if core_clock is not None and isinstance(core_clock, (int, float)):
                        yield from self._gauge_sample("core_clock_mhz", "mhz", gpu_info, float(core_clock))

                    mem_clock = amdsmi.smi_get_property(i, "MEM_CURRENT_CLK")
                    if mem_clock is not None and isinstance(mem_clock, (int, float)):
                        yield from self._gauge_sample("memory_clock_mhz", "mhz", gpu_info, float(mem_clock))
                except Exception as exc:
                    logger.debug("AMD GPU %d clock read failed: %s", i, exc)

                # Fan speed (%)
                try:
                    fan = amdsmi.smi_get_property(i, "FAN_SPEED")
                    if fan is not None and isinstance(fan, (int, float)):
                        yield from self._gauge_sample("fan_speed_percent", "percent", gpu_info, float(fan))
                except Exception as exc:
                    logger.debug("AMD GPU %d fan read failed: %s", i, exc)

        except Exception as exc:
            logger.error("Error collecting AMD metrics: %s", exc)

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
    _shutdown_amdsmi()
