"""GPU Prometheus Exporter — unified monitoring for AMD, Intel, NVIDIA GPUs + CPU."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from gpu_exporter.config import load_config, AppConfig
from gpu_exporter.collectors.nvidia import NvidiaCollector
from gpu_exporter.collectors.amd import AmdCollector
from gpu_exporter.collectors.intel import IntelCollector
from gpu_exporter.collectors.cpu import CpuCollector

logger = logging.getLogger("gpu_exporter")


def _format_metric(name: str, help_text: str, labels: dict[str, str], value: float) -> str:
    """Format a single Prometheus metric line."""
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    if label_str:
        return f"{name}{{{label_str}}} {value}\n"
    return f"{name} {value}\n"


def collect_all_metrics(cfg: AppConfig) -> str:
    """Collect metrics from all enabled collectors and format as Prometheus text."""
    lines = ["# HELP gpu_exporter_up 1 if the exporter is running, 0 otherwise.\n"]
    lines.append("# TYPE gpu_exporter_up gauge\n")
    lines.append("gpu_exporter_up 1\n\n")

    # ── NVIDIA GPUs ────────────────────────────────────────────────
    if cfg.collectors.nvidia:
        nc = NvidiaCollector(cfg.labels)
        if nc.initialize():
            for dev in nc._devices:
                handle = __import__("pynvml", fromlist=["nvml"]).nvmlDeviceGetHandleByIndex(dev.index)

                util = __import__("pynvml", fromlist=["nvml"]).nvmlDeviceGetUtilizationRates(handle).gpu
                lines.append(_format_metric(
                    "gpu_nvidia_utilization", f"GPU nvidia utilization (percent)",
                    {"gpu_vendor": "nvidia", "gpu_index": str(dev.index), "gpu_name": dev.name}, float(util)))

                mem_info = __import__("pynvml", fromlist=["nvml"]).nvmlDeviceGetMemoryInfo(handle)
                lines.append(_format_metric(
                    "gpu_nvidia_memory_used", f"GPU nvidia memory used (bytes)",
                    {"gpu_vendor": "nvidia", "gpu_index": str(dev.index), "gpu_name": dev.name}, float(mem_info.used)))

                temp = __import__("pynvml", fromlist=["nvml"]).nvmlDeviceGetTemperature(handle, 2)
                lines.append(_format_metric(
                    "gpu_nvidia_temperature_celsius", f"GPU nvidia temperature (celsius)",
                    {"gpu_vendor": "nvidia", "gpu_index": str(dev.index), "gpu_name": dev.name}, float(temp)))

                power = __import__("pynvml", fromlist=["nvml"]).nvmlDeviceGetPowerUsage(handle) / 1000.0
                lines.append(_format_metric(
                    "gpu_nvidia_power_watts", f"GPU nvidia power (watts)",
                    {"gpu_vendor": "nvidia", "gpu_index": str(dev.index), "gpu_name": dev.name}, float(power)))

    # ── AMD GPUs ───────────────────────────────────────────────────
    if cfg.collectors.amd:
        try:
            import pyrsmi.rocml as amdsmi
            device_count = amdsmi.smi_get_device_count()
            for i in range(device_count):
                gpu_info = __import__("gpu_exporter.metrics", fromlist=["GpuInfo"]).GpuInfo(
                    vendor="amd", index=i, name=nc._devices[i].name if hasattr(nc, '_devices') and i < len(nc._devices) else f"AMD GPU {i}")

                power = amdsmi.smi_get_property(i, "POWER")
                if power is not None:
                    lines.append(_format_metric(
                        "gpu_amd_power_watts", f"GPU amd power (watts)",
                        {"gpu_vendor": "amd", "gpu_index": str(i), "gpu_name": gpu_info.name}, float(power)))

                temp = amdsmi.smi_get_property(i, "TEMPERATURE")
                if temp is not None:
                    lines.append(_format_metric(
                        "gpu_amd_temperature_celsius", f"GPU amd temperature (celsius)",
                        {"gpu_vendor": "amd", "gpu_index": str(i), "gpu_name": gpu_info.name}, float(temp)))

        except Exception as exc:
            logger.warning("AMD collection failed: %s", exc)

    # ── Intel GPUs ─────────────────────────────────────────────────
    if cfg.collectors.intel:
        ic = IntelCollector(cfg.labels)
        if ic.initialize():
            for dev in ic._devices:
                lines.append(_format_metric(
                    "gpu_intel_temperature_celsius", f"GPU intel temperature (celsius)",
                    {"gpu_vendor": "intel", "gpu_index": str(dev.index), "gpu_name": dev.name}, 0.0))

    # ── CPU Metrics ────────────────────────────────────────────────
    if cfg.collectors.cpu:
        cc = CpuCollector(cfg.labels)
        for metric in cc.collect():
            for sample in metric.samples:
                name, labels_dict, value = sample.name, dict(sample.labels), sample.value
                lines.append(_format_metric(name, metric.documentation, labels_dict, value))

    return "".join(lines)


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves Prometheus metrics."""

    def do_GET(self):
        if self.path == "/metrics":
            cfg = load_config()
            output = collect_all_metrics(cfg).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(output)

        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK\n")

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def run_exporter(cfg: AppConfig):
    """Start the Prometheus metrics exporter server."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    logger.info("GPU Prometheus Exporter v0.1.0")
    logger.info("Binding to %s:%d (interval: %ds)", cfg.exporter.host, cfg.exporter.port, cfg.exporter.interval)

    server = HTTPServer((cfg.exporter.host, cfg.exporter.port), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    logger.info("Metrics endpoint: http://localhost:%d/metrics", cfg.exporter.port)
    logger.info("Health check:   http://localhost:%d/health", cfg.exporter.port)

    def shutdown_handler(signum, frame):
        logger.info("Shutting down...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        while True:
            time.sleep(cfg.exporter.interval)
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
        server.shutdown()


def main():
    parser = argparse.ArgumentParser(description="GPU Prometheus Exporter — unified GPU/CPU monitoring")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    parser.add_argument("--port", type=int, default=None, help="Override metrics port")
    parser.add_argument("--host", type=str, default=None, help="Override bind address")
    parser.add_argument("--interval", type=int, default=None, help="Collection interval in seconds")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.port is not None:
        cfg.exporter.port = args.port
    if args.host is not None:
        cfg.exporter.host = args.host
    if args.interval is not None:
        cfg.exporter.interval = args.interval

    run_exporter(cfg)


if __name__ == "__main__":
    main()
