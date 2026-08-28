"""Intel GPU collector using sysfs (i915/Xe kernel driver).

Requires: i915 or xe kernel module loaded. Works in Docker with --device=/dev/dri.
Reads from /sys/class/drm/card*/device/ for hwmon and performance counters.
Metrics exposed per device: utilization, memory_used, memory_total, power_watts,
power_limit_watts, temperature_celsius, core_clock_mhz, memory_clock_mhz, fan_speed_percent
"""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from gpu_exporter.metrics import GpuInfo, UnifiedGpuCollector

logger = logging.getLogger("gpu_exporter.intel")


@dataclass(frozen=True)
class IntelGpuDevice:
    index: int
    name: str
    uuid: str = ""
    memory_total: int = 0  # bytes (from I915_GEM_INFO if available)
    bus_id: str = ""
    card_path: str = ""


def enumerate_devices() -> list[IntelGpuDevice]:
    """List all Intel GPUs visible via /sys/class/drm/card*."""
    devices = []

    # Find all DRM cards (Intel GPUs appear as card0, card1, etc.)
    card_paths = sorted(glob.glob("/sys/class/drm/card[0-9]*/device"))

    for idx, dev_path in enumerate(card_paths):
        if not os.path.exists(dev_path):
            continue

        # Check if it's an Intel device by vendor ID (0x8086)
        vendor_file = os.path.join(dev_path, "vendor")
        try:
            with open(vendor_file, "r") as f:
                vendor_id = f.read().strip()
            if vendor_id != "0x8086":
                continue  # Not Intel
        except (FileNotFoundError, PermissionError):
            continue

        # Get device name from sysfs
        name = ""
        try:
            with open(os.path.join(dev_path, "device_name"), "r") as f:
                name = f.read().strip()
        except (FileNotFoundError, PermissionError):
            pass

        if not name:
            # Try cardX/name
            try:
                card_num = os.path.basename(dev_path).replace("device", "")
                with open(f"/sys/class/drm/{card_num}/name", "r") as f:
                    name = f.read().strip()
            except (FileNotFoundError, PermissionError):
                name = f"Intel GPU {idx}"

        # Get UUID if available
        uuid = ""
        try:
            with open(os.path.join(dev_path, "uuid"), "r") as f:
                uuid = f.read().strip()
        except (FileNotFoundError, PermissionError):
            pass

        # Try to get memory info from I915_GEM_INFO
        mem_total = 0
        try:
            total_mem_file = os.path.join(dev_path, "gt", "total_memory")
            if os.path.exists(total_mem_file):
                with open(total_mem_file, "r") as f:
                    mem_total = int(f.read().strip()) * 1024  # KB -> bytes
        except (FileNotFoundError, PermissionError, ValueError):
            pass

        devices.append(IntelGpuDevice(
            index=idx, name=name, uuid=uuid, memory_total=mem_total, bus_id="", card_path=dev_path,
        ))

    return devices


class IntelCollector(UnifiedGpuCollector):
    """Collects metrics from all Intel GPUs via sysfs."""

    def __init__(self, labels: dict[str, str] | None = None):
        super().__init__(labels or {})
        self._initialized = False
        self._devices: list[IntelGpuDevice] = []

    def initialize(self) -> bool:
        """Try to enumerate Intel GPUs. Returns True if any found."""
        self._devices = enumerate_devices()
        if not self._devices:
            logger.info("No Intel GPUs found via sysfs")
            return False

        logger.info("Found %d Intel GPU(s)", len(self._devices))
        for dev in self._devices:
            logger.info("  [%d] %s (path: %s, VRAM: %.0f MiB)",
                        dev.index, dev.name, dev.card_path, dev.memory_total / 1024**2)

        self._initialized = True
        return True

    def _read_hwmon(self, card_path: str, sensor_type: str) -> float | None:
        """Read a hwmon value (temp, power, fan)."""
        # Find the hwmon directory for this GPU
        hwmon_dirs = glob.glob(os.path.join(card_path, "hwmon/hwmon*"))
        if not hwmon_dirs:
            return None

        hwmon_dir = hwmon_dirs[0]

        # Map sensor type to input file pattern
        patterns = {
            "temp": ["temp1_input", "temp2_input"],
            "power": ["power1_average", "power1_input", "energy1_accumulate"],
            "fan": ["fan1_input", "fan1_target"],
        }

        for pattern in patterns.get(sensor_type, []):
            filepath = os.path.join(hwmon_dir, pattern)
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "r") as f:
                    val = int(f.read().strip())
                return float(val)
            except (ValueError, PermissionError):
                continue

        return None

    def _read_gt_frequency(self, card_path: str, freq_type: str) -> float | None:
        """Read GPU frequency from sysfs gt directory."""
        gt_dir = os.path.join(card_path, "gt")
        if not os.path.exists(gt_dir):
            return None

        # Frequency is in kHz for some kernels, MHz for others — try both
        freq_files = {
            "core": ["cur_freq", "min_cur_freq"],
            "mem": ["mem_min_cur_freq"],
        }

        for fname in freq_files.get(freq_type, []):
            filepath = os.path.join(gt_dir, fname)
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "r") as f:
                    val = int(f.read().strip())
                # Some kernels report in kHz, some in MHz — heuristics
                if val > 10000:  # Likely kHz
                    return float(val) / 1000.0
                return float(val)
            except (ValueError, PermissionError):
                continue

        return None

    def _read_utilization(self, card_path: str) -> float | None:
        """Read GPU utilization from sysfs or perf."""
        # Try I915_GEM_INFO / gt directory first
        gt_dir = os.path.join(card_path, "gt")
        if not os.path.exists(gt_dir):
            return None

        # For newer kernels (Xe/i915), try the 0/ directory for per-engine stats
        engine_dir = os.path.join(gt_dir, "0")
        if os.path.exists(engine_dir):
            busy_file = os.path.join(engine_dir, "busy_time")
            if os.path.exists(busy_file):
                try:
                    with open(busy_file, "r") as f:
                        busy_ns = int(f.read().strip())
                    # We need a reference timestamp — fall back to sysfs for now
                except (ValueError, PermissionError):
                    pass

        # Fallback: read from /proc/driver/i915/gt_status if available
        try:
            with open("/proc/driver/i915/gt_status", "r") as f:
                content = f.read()
                match = re.search(r"Busy:\s+(\d+)%", content)
                if match:
                    return float(match.group(1))
        except (FileNotFoundError, PermissionError):
            pass

        # Last resort: try perf PMU via intel_gpu_top JSON output
        try:
            import subprocess
            result = subprocess.run(
                ["intel-gpu-top", "-J"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Parse first JSON object from the stream
                import json
                data = json.loads(result.stdout.split("{")[0] + "{")
                busy = data.get("busy", {}).get("gt0", {})
                if isinstance(busy, dict):
                    return float(busy.get("percentage", 0))
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            pass

        return None

    def collect(self):
        """Yield all GPU metrics for all Intel devices."""
        if not self._initialized:
            return

        try:
            for dev in self._devices:
                gpu_info = GpuInfo(vendor="intel", index=dev.index, name=dev.name)

                # Utilization (%)
                util = self._read_utilization(dev.card_path)
                if util is not None:
                    yield from self._gauge_sample("utilization", "percent", gpu_info, min(util, 100.0))

                # Memory (bytes) — only available on some platforms with I915_GEM_INFO
                if dev.memory_total > 0:
                    mem_used = self._read_hwmon(dev.card_path, "memory_used")
                    if mem_used is not None:
                        yield from self._gauge_sample("memory_used", "bytes", gpu_info, float(mem_used))
                    yield from self._gauge_sample("memory_total", "bytes", gpu_info, float(dev.memory_total))

                # Temperature (Celsius) — hwmon temp1_input is usually GPU die temp
                temp = self._read_hwmon(dev.card_path, "temp")
                if temp is not None:
                    yield from self._gauge_sample("temperature_celsius", "celsius", gpu_info, float(temp))

                # Power (Watts) — hwmon power1_average or energy1_accumulate derivative
                power = self._read_hwmon(dev.card_path, "power")
                if power is not None:
                    yield from self._gauge_sample("power_watts", "watts", gpu_info, float(power))

                # Clock speeds (MHz) — gt/cur_freq in kHz or MHz
                core_clock = self._read_gt_frequency(dev.card_path, "core")
                if core_clock is not None:
                    yield from self._gauge_sample("core_clock_mhz", "mhz", gpu_info, float(core_clock))

                mem_clock = self._read_gt_frequency(dev.card_path, "mem")
                if mem_clock is not None:
                    yield from self._gauge_sample("memory_clock_mhz", "mhz", gpu_info, float(mem_clock))

                # Fan speed (%) — hwmon fan1_input (RPM) -> convert to % if possible
                fan_rpm = self._read_hwmon(dev.card_path, "fan")
                if fan_rpm is not None and fan_rpm > 0:
                    # Some Intel GPUs report RPM directly; try to get max RPM from sysfs
                    fan_max = self._read_hwmon(dev.card_path, "fan_max")
                    if fan_max and fan_max > 0:
                        yield from self._gauge_sample("fan_speed_percent", "percent", gpu_info, (fan_rpm / fan_max) * 100.0)
                    else:
                        # Just report RPM as a separate metric — skip % for now
                        pass

        except Exception as exc:
            logger.error("Error collecting Intel metrics: %s", exc)

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
