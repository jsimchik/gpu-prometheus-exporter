# GPU Prometheus Exporter

Unified Prometheus exporter for **AMD, Intel, and NVIDIA GPUs** plus CPU metrics — all in one container. No separate exporters needed.

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────┐
│  NVIDIA GPU │     │                  │     │              │
│  (NVML)     │────▶│   gpu-exporter   │────▶│ Prometheus   │
├─────────────┤     │   container      │     │ Grafana      │
│  AMD GPU    │────▶│                  │     │ Dashboard    │
│  (ROCm SMI) │────▶│                  │     │              │
├─────────────┤     └──────────────────┘     └──────────────┘
│  Intel GPU  │────▶
│  (sysfs)    │
├─────────────┐
│  CPU        │────▶
│  (RAPL/hwmon)
└─────────────┘
```

Each collector is **independent** — if a vendor's driver isn't available, that collector silently skips. No config changes needed when you swap hardware.

## Metrics Exposed

All metrics use unified naming regardless of GPU vendor:

| Metric | Type | Description | Labels |
|--------|------|-------------|--------|
| `gpu_<vendor>_utilization` | gauge | GPU compute utilization (%) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_memory_used` | gauge | VRAM used (bytes) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_memory_total` | gauge | Total VRAM (bytes) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_power_watts` | gauge | Power draw (W) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_power_limit_watts` | gauge | Power limit (W) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_temperature_celsius` | gauge | GPU temperature (°C) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_core_clock_mhz` | gauge | Core clock speed (MHz) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_memory_clock_mhz` | gauge | Memory clock speed (MHz) | gpu_vendor, gpu_index, gpu_name |
| `gpu_<vendor>_fan_speed_percent` | gauge | Fan speed (%) | gpu_vendor, gpu_index, gpu_name |
| `cpu_utilization` | gauge | Overall CPU utilization (%) | - |
| `cpu_temperature_celsius` | gauge | CPU/Chip temperature (°C) | source, name |
| `cpu_package_power_watts` | gauge | RAPL package power (W) | package |

### Example output (`curl http://localhost:9102/metrics`)

```prometheus
# HELP gpu_nvidia_utilization GPU nvidia utilization (percent)
# TYPE gpu_nvidia_utilization gauge
gpu_nvidia_utilization{gpu_vendor="nvidia",gpu_index="0",gpu_name="GeForce RTX 4090"} 45.2
gpu_nvidia_utilization{gpu_vendor="nvidia",gpu_index="1",gpu_name="GeForce RTX 4090"} 12.8

# HELP gpu_amd_temperature_celsius GPU amd temperature (celsius)
# TYPE gpu_amd_temperature_celsius gauge
gpu_amd_temperature_celsius{gpu_vendor="amd",gpu_index="0",gpu_name="AMD Radeon RX 7900 XTX"} 67.5

# HELP cpu_utilization CPU overall utilization (%)
# TYPE cpu_utilization gauge
cpu_utilization 23.4

# HELP gpu_intel_temperature_celsius GPU intel temperature (celsius)
# TYPE gpu_intel_temperature_celsius gauge
gpu_intel_temperature_celsius{gpu_vendor="intel",gpu_index="0",gpu_name="Intel(R) Arc(TM) A770"} 52.1
```

## Quick Start — Docker

### Prerequisites

- Docker + Docker Compose installed
- GPU drivers already working on the host:
  - **NVIDIA**: `nvidia-container-toolkit` (`docker run --gpus all nvidia/cuda:latest nvidia-smi`)
  - **AMD**: ROCm driver loaded (`rocm-smi` works)
  - **Intel**: i915 or xe kernel module loaded

### One-command start (NVIDIA GPUs)

```bash
git clone https://github.com/YOUR-USER/gpu-prometheus-exporter.git
cd gpu-prometheus-exporter
docker compose up -d
```

### Multi-GPU setup (all vendors simultaneously)

Edit `config.yaml` to enable/disable collectors:

```yaml
collectors:
  nvidia: true    # Enable NVIDIA GPU collection
  amd: true       # Enable AMD GPU collection  
  intel: true     # Enable Intel GPU collection
  cpu: true       # Enable CPU metrics (always useful)
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EXPORTER_PORT` | `9102` | HTTP port for metrics |
| `EXPORTER_INTERVAL` | `5` | Collection interval in seconds |

## Installation Methods

### Method 1: Docker (recommended)

```bash
# Clone and start
git clone https://github.com/YOUR-USER/gpu-prometheus-exporter.git
cd gpu-prometheus-exporter
docker compose up -d

# Check metrics
curl http://localhost:9102/metrics | head -30
```

### Method 2: pip install (bare metal)

```bash
pip install pynvml pyrsmi prometheus-client PyYAML
git clone https://github.com/YOUR-USER/gpu-prometheus-exporter.git
cd gpu-prometheus-exporter
python -m gpu_exporter.main --port 9102
```

### Method 3: Direct from source

```bash
# Install dependencies
pip install .

# Run with custom config
gpu-exporter --config /path/to/config.yaml
```

## Prometheus Configuration

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'gpu-exporter'
    static_configs:
      - targets: ['host.docker.internal:9102']  # Docker host
        labels:
          env: 'production'
    
  # Or if running on bare metal:
  - job_name: 'gpu-exporter-local'
    static_configs:
      - targets: ['localhost:9102']
```

For Docker, use `host.docker.internal` or the container's IP. For bare metal, use `localhost`.

## Grafana Dashboard

Import the dashboard JSON from `grafana/dashboard.json`:

1. Open Grafana → Dashboards → Import
2. Upload `dashboard.json`
3. Select your Prometheus datasource
4. The dashboard auto-detects GPU vendors and shows unified views

### What's in the dashboard:

- **Overview**: All GPUs + CPU on one screen
- **GPU Detail**: Per-GPU utilization, memory, power, temperature over time
- **CPU Overview**: Utilization, temperatures, RAPL power per package
- **Alerts panel**: Temperature warnings (>85°C), power limit hits, fan failures

## Troubleshooting

### No GPUs detected?

**NVIDIA:**
```bash
# Check if NVML can see your GPU
python3 -c "import pynvml; pynvml.nvmlInit(); print(pynvml.nvmlDeviceGetCount())"
# If this fails, install nvidia-container-toolkit or check driver:
nvidia-smi
```

**AMD:**
```bash
# Check if ROCm SMI works
rocm-smi --showallinfo | head -20
# In Docker, ensure these device mounts are present:
# /dev/kfd and /dev/dri/renderD128
```

**Intel:**
```bash
# Check if i915/xe driver is loaded
lsmod | grep -E 'i915|xe'
# Check hwmon sensors exist
ls /sys/class/drm/card0/device/hwmon/
```

### Metrics show 0 or stale values?

- Ensure the container has access to `/dev/kfd`, `/dev/dri/*`, and `/sys/class/hwmon`
- For CPU power (RAPL), ensure `intel-rapl-*` is mounted from host
- Check logs: `docker logs gpu-exporter`

### Port already in use?

```bash
# Change port via environment variable
EXPORTER_PORT=9200 docker compose up -d
```

## Configuration Reference

Full config.yaml reference:

```yaml
exporter:
  port: 9102          # HTTP metrics port
  host: "0.0.0.0"     # Bind address (use 127.0.0.1 for localhost only)
  interval: 5         # Collection interval in seconds

collectors:
  nvidia: true        # Enable NVIDIA GPU collection (requires pynvml + driver)
  amd: true           # Enable AMD GPU collection (requires pyrsmi + ROCm)
  intel: true         # Enable Intel GPU collection (requires i915/xe kernel module)
  cpu: true           # Enable CPU metrics (temp, power via RAPL/hwmon)

labels:
  env: "production"   # Custom labels applied to all metrics
```

## License

MIT
