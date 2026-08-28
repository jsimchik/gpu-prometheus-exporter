"""Configuration loader with defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ExporterConfig:
    port: int = 9102
    host: str = "0.0.0.0"
    interval: int = 5


@dataclass
class CollectorConfig:
    nvidia: bool = True
    amd: bool = True
    intel: bool = True
    cpu: bool = True


@dataclass
class AppConfig:
    exporter: ExporterConfig = field(default_factory=ExporterConfig)
    collectors: CollectorConfig = field(default_factory=CollectorConfig)
    labels: dict[str, str] = field(default_factory=lambda: {"env": "production"})


def load_config(path: Path | None = None) -> AppConfig:
    """Load config from YAML file or environment variables."""
    cfg_path = path or Path(__file__).parent.parent / "config.yaml"

    # Defaults
    app_cfg = AppConfig()

    if cfg_path.exists():
        with open(cfg_path, "r") as f:
            data = yaml.safe_load(f) or {}

        exporter_data = data.get("exporter", {})
        collector_data = data.get("collectors", {})
        labels_data = data.get("labels", {})

        app_cfg.exporter.port = int(exporter_data.get("port", 9102))
        app_cfg.exporter.host = str(exporter_data.get("host", "0.0.0.0"))
        app_cfg.exporter.interval = int(exporter_data.get("interval", 5))

        if collector_data:
            app_cfg.collectors.nvidia = bool(collector_data.get("nvidia", True))
            app_cfg.collectors.amd = bool(collector_data.get("amd", True))
            app_cfg.collectors.intel = bool(collector_data.get("intel", True))
            app_cfg.collectors.cpu = bool(collector_data.get("cpu", True))

        if labels_data:
            app_cfg.labels = {str(k): str(v) for k, v in labels_data.items()}

    # Environment variable overrides (highest priority)
    env_port = os.environ.get("EXPORTER_PORT")
    if env_port is not None:
        app_cfg.exporter.port = int(env_port)

    env_interval = os.environ.get("EXPORTER_INTERVAL")
    if env_interval is not None:
        app_cfg.exporter.interval = int(env_interval)

    return app_cfg
