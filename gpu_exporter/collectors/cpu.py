"""CPU metrics collector — utilization, temperature, power via sysfs/RAPL/hwmon.

Reads from:
  - /proc/stat for CPU utilization (delta-based)
  - /sys/class/hwmon for temperatures and fan speeds
  - /sys/class/powercap/intel-rapl-* for RAPL package/core power
"""

from __future__ import annotations

import glob
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from prometheus_client.core import GaugeMetricFamily

logger = logging.getLogger("gpu_exporter.cpu")


@dataclass(frozen=True)
class CpuPackage:
    package_id: int
    model: str = ""
    core_count: int = 0


def _get_cpu_model() -> str:
    """Get CPU model name from /proc/cpuinfo."""
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except (FileNotFoundError, PermissionError):
        pass
    return "Unknown CPU"


def _get_core_count() -> int:
    """Get number of logical cores."""
    try:
        with open("/proc/cpuinfo", "r") as f:
            count = sum(1 for line in f if line.startswith("processor"))
        return max(count, 1)
    except (FileNotFoundError, PermissionError):
        return os.cpu_count() or 1


def _get_package_count() -> int:
    """Get number of CPU packages from sysfs."""
    pkgs = glob.glob("/sys/devices/system/cpu/cpu*/topology/physical_package_id")
    if not pkgs:
        return 1

    package_ids = set()
    for pkg_path in pkgs:
        try:
            with open(pkg_path, "r") as f:
                package_ids.add(f.read().strip())
        except (FileNotFoundError, PermissionError):
            pass

    return max(len(package_ids), 1)


def _get_hwmon_sensors() -> dict[str, list[dict]]:
    """Read all hwmon sensors. Returns {sensor_type: [{name, value}]}.

    sensor_types: temp, power, fan
    Values are raw (Celsius for temp, Watts for power, RPM for fan).
    """
    result = {"temp": [], "power": [], "fan": []}
    hwmon_dirs = glob.glob("/sys/class/hwmon/hwmon*")

    for hwmon_dir in sorted(hwmon_dirs):
        try:
            name_file = os.path.join(hwmon_dir, "name")
            if os.path.exists(name_file):
                with open(name_file, "r") as f:
                    chip_name = f.read().strip()
            else:
                chip_name = os.path.basename(hwmon_dir)

            # Read temperature inputs
            for i in range(1, 20):
                temp_input = os.path.join(hwmon_dir, f"temp{i}_input")
                if not os.path.exists(temp_input):
                    break
                try:
                    with open(temp_input, "r") as f:
                        val_milli = int(f.read().strip())
                    result["temp"].append({
                        "chip": chip_name,
                        "index": i,
                        "name": f"temp{i}",
                        "value_celsius": val_milli / 1000.0,
                    })
                except (ValueError, PermissionError):
                    pass

            # Read power inputs
            for i in range(1, 20):
                power_input = os.path.join(hwmon_dir, f"power{i}_average")
                if not os.path.exists(power_input):
                    break
                try:
                    with open(power_input, "r") as f:
                        val_micro = int(f.read().strip())
                    result["power"].append({
                        "chip": chip_name,
                        "index": i,
                        "name": f"power{i}",
                        "value_watts": val_micro / 1_000_000.0,
                    })
                except (ValueError, PermissionError):
                    pass

            # Read fan inputs
            for i in range(1, 20):
                fan_input = os.path.join(hwmon_dir, f"fan{i}_input")
                if not os.path.exists(fan_input):
                    break
                try:
                    with open(fan_input, "r") as f:
                        val_rpm = int(f.read().strip())
                    result["fan"].append({
                        "chip": chip_name,
                        "index": i,
                        "name": f"fan{i}",
                        "value_rpm": float(val_rpm),
                    })
                except (ValueError, PermissionError):
                    pass

        except Exception:
            continue

    return result


def _get_rapl_power() -> dict[int, float]:
    """Read RAPL package power from /sys/class/powercap/intel-rapl-*/.

    Returns {package_id: watts}.
    """
    rapl_dirs = glob.glob("/sys/class/powercap/intel-rapl:*")
    result = {}

    for rapl_dir in sorted(rapl_dirs):
        # Extract package ID from directory name
        match = None
        import re
        m = re.search(r"intel-rapl:(\d+)", os.path.basename(rapl_dir))
        if m:
            pkg_id = int(m.group(1))

        energy_uj_file = os.path.join(rapl_dir, "energy_uJ")
        if not os.path.exists(energy_uj_file):
            continue

        try:
            with open(energy_uj_file, "r") as f:
                current_energy_uj = int(f.read().strip())

            # Store for delta calculation in next call
            result[pkg_id] = current_energy_uj
        except (ValueError, PermissionError):
            continue

    return result


class CpuCollector:
    """Collects CPU utilization, temperature, and power metrics."""

    def __init__(self, labels: dict[str, str] | None = None):
        self.labels = labels or {}
        self._prev_stat = None
        self._prev_time = time.monotonic()
        self._rapl_prev = {}  # package_id -> energy_uJ

    def collect(self) -> list[GaugeMetricFamily]:
        """Yield all CPU metrics."""
        gauges: dict[str, GaugeMetricFamily] = {}

        def get_gauge(name: str, help_text: str, labels_list: list[str]):
            if name not in gauges:
                gauge_obj = GaugeMetricFamily(name, help_text, labels=labels_list)
                gauges[name] = gauge_obj
            return gauges[name]

        # ── CPU Utilization (delta-based from /proc/stat) ───────────────
        try:
            with open("/proc/stat", "r") as f:
                cpu_line = f.readline()
            parts = cpu_line.split()
            if len(parts) >= 8 and parts[0] == "cpu":
                # user, nice, system, idle, iowait, irq, softirq, steal
                fields = [int(x) for x in parts[1:]]
                while len(fields) < 8:
                    fields.append(0)

                user, nice, system, idle, iowait, irq, softirq, steal = fields[:8]
                total = user + nice + system + idle + iowait + irq + softirq + steal
                idle_delta = idle - self._prev_stat[3] if self._prev_stat else 0
                total_delta = total - self._prev_stat[7] if self._prev_stat else total

                if total_delta > 0 and self._prev_stat:
                    utilization = (1.0 - idle_delta / total_delta) * 100.0
                elif not self._prev_stat:
                    # First call — report 0 as baseline
                    utilization = 0.0
                else:
                    utilization = 0.0

                g = get_gauge("cpu_utilization", "CPU overall utilization (%)", ["label"])
                g.add_metric(["overall"], min(utilization, 100.0))

                self._prev_stat = (user, nice, system, idle, iowait, irq, softirq, steal)
                self._prev_time = time.monotonic()
            else:
                logger.warning("Unexpected /proc/stat format")
        except Exception as exc:
            logger.error("Failed to read CPU utilization from /proc/stat: %s", exc)

        # ── Temperature (hwmon + RAPL thermal zones) ────────────────────
        hwmon = _get_hwmon_sensors()

        for sensor in hwmon["temp"]:
            g = get_gauge(
                "cpu_temperature_celsius",
                f"CPU/Chip temperature from {sensor['chip']} (C)",
                ["source", "name"],
            )
            g.add_metric([sensor["chip"], sensor["name"]], sensor["value_celsius"])

        # Also check thermal zones for CPU-specific temps
        tz_dirs = glob.glob("/sys/class/thermal/thermal_zone*")
        for tz_dir in sorted(tz_dirs):
            try:
                type_file = os.path.join(tz_dir, "type")
                temp_file = os.path.join(tz_dir, "temp")
                if not os.path.exists(type_file) or not os.path.exists(temp_file):
                    continue

                with open(type_file, "r") as f:
                    tz_type = f.read().strip()

                # Only care about CPU-related thermal zones
                if any(kw in tz_type.lower() for kw in ["cpu", "package", "core", "thermal"]):
                    with open(temp_file, "r") as f:
                        temp_milli = int(f.read().strip())
                    g = get_gauge(
                        "cpu_temperature_celsius",
                        f"CPU thermal zone {tz_type} (C)",
                        ["source", "name"],
                    )
                    g.add_metric([f"thermal_zone_{os.path.basename(tz_dir)}", tz_type], temp_milli / 1000.0)
            except Exception:
                continue

        # ── Power (RAPL package power in Watts) ─────────────────────────
        try:
            rapl_current = _get_rapl_power()
            for pkg_id, energy_uj in rapl_current.items():
                if pkg_id in self._rapl_prev and self._prev_time > 0:
                    elapsed = time.monotonic() - self._prev_time
                    if elapsed > 0:
                        delta_watts = (energy_uj - self._rapl_prev[pkg_id]) / (elapsed * 1_000_000)
                        g = get_gauge(
                            "cpu_package_power_watts",
                            f"RAPL package {pkg_id} power (W)",
                            ["package"],
                        )
                        g.add_metric([str(pkg_id)], max(delta_watts, 0.0))

            self._rapl_prev.update(rapl_current)
        except Exception as exc:
            logger.debug("RAPL power read failed: %s", exc)

        # ── Fan speeds (hwmon) ──────────────────────────────────────────
        for sensor in hwmon["fan"]:
            g = get_gauge(
                "cpu_fan_rpm",
                f"CPU fan speed from {sensor['chip']} (RPM)",
                ["source", "name"],
            )
            g.add_metric([sensor["chip"], sensor["name"]], sensor["value_rpm"])

        return list(gauges.values())
