# Workload Automation Toolkit

A Python toolkit for setting up, running, and collecting results from CPU, memory, and I/O benchmarks. Workloads are pinned to specific cores via `taskset` and `numactl`, and the built-in scaling study engine sweeps across configurations automatically.

---

## Requirements

- Python 3.9+
- Linux (reads CPU topology from `/sys/devices/system/cpu/`)
- `taskset` (util-linux) and `numactl` — present on most distros

Optional Python packages (richer table output, no functional impact if missing):

```
pip install tabulate
```

No other Python dependencies are required. The `python_bench` workload works out of the box. All other workloads require their respective system binaries (see [Workloads](#workloads)).

---

## Directory Structure

```
tools/
├── main.py                     # CLI entry point
├── requirements.txt
├── benchmark_toolkit/          # Core library
│   ├── system.py               # CPU/NUMA topology detection
│   ├── config.py               # BenchmarkConfig + ConfigPreset factory
│   ├── base.py                 # BaseWorkload abstract class
│   ├── registry.py             # Auto-discovery of workloads/
│   ├── runner.py               # Execution engine (taskset, numactl, timing)
│   ├── collector.py            # Results → JSON + CSV + summary stats
│   └── scaling.py              # Scaling study orchestrator
├── workloads/                  # Drop new .py files here to add workloads
│   ├── python_bench.py         # Pure Python (always works, no deps)
│   ├── sysbench.py             # sysbench CPU
│   ├── stream.py               # STREAM memory bandwidth
│   └── fio_bench.py            # FIO I/O benchmark
└── results/                    # Auto-created; one subdirectory per run
```

---

## Quick Start

```bash
# See what's available
python3 main.py list-workloads
python3 main.py list-configs

# Check which workloads are ready to run
python3 main.py validate

# Run the built-in Python benchmark on all physical cores
python3 main.py run --workload python_bench --config full_socket --iterations 3

# Run a scaling study
python3 main.py scaling --workload python_bench --mode powers_of_2

# Compare SMT vs physical cores
python3 main.py scaling --workload python_bench --configs 1c1t,1c2t,2c2t,2c4t

# List and inspect saved results
python3 main.py report --list
python3 main.py report --run-id <run_id>
```

---

## CLI Reference

All commands are invoked as:

```
python3 main.py [--work-dir DIR] COMMAND [options]
```

`--work-dir` (default: `/tmp/benchmark_toolkit`) is where temporary files such as worker scripts are written.

---

### `list-workloads`

List all auto-discovered workloads.

```bash
python3 main.py list-workloads
```

---

### `list-configs`

List all configuration presets for the current machine. Topology is detected automatically; presets reflect the actual core and thread counts of your CPU.

```bash
python3 main.py list-configs
```

---

### `validate`

Check whether each workload's required binaries are installed.

```bash
# Validate all workloads
python3 main.py validate

# Validate a specific workload
python3 main.py validate --workload sysbench_cpu
```

---

### `run`

Run a single workload benchmark.

```bash
python3 main.py run --workload NAME [--config PRESET | --threads N]
                    [--iterations N] [--dry-run] [--verbose]
                    [--arg key=value ...]
```

| Flag | Description |
|---|---|
| `--workload NAME` | Workload to run (required) |
| `--config PRESET` | Named config preset (see [Configurations](#configurations)) |
| `--threads N` | Custom thread count (alternative to `--config`) |
| `--iterations N` | Repeat count; statistics are computed across iterations (default: 1) |
| `--dry-run` | Print the exact command that would be run, without executing it |
| `--verbose` | Print per-iteration metrics as they complete |
| `--arg key=value` | Pass workload-specific arguments (see each workload's defaults) |

**Examples:**

```bash
# Run on all physical cores, 3 iterations
python3 main.py run --workload python_bench --config full_socket --iterations 3

# Run on 8 threads (auto-selected CPUs)
python3 main.py run --workload python_bench --threads 8

# Run sysbench with custom prime limit and duration
python3 main.py run --workload sysbench_cpu --config 4c4t --arg prime=50000 time=30

# Run FIO sequential read on a specific directory
python3 main.py run --workload fio --config 8c8t \
    --arg rw=read bs=128k size=4G directory=/mnt/nvme runtime=60

# Preview the command without running
python3 main.py run --workload sysbench_cpu --config 2c4t --dry-run
```

After each run, a summary table is printed and results are saved to `results/`.

---

### `scaling`

Run a workload across multiple configurations and print a speedup/efficiency table.

```bash
python3 main.py scaling --workload NAME
                        [--configs 1c1t,1c2t,...]  # named preset list
                        [--threads 1,2,4,...]       # explicit thread counts
                        [--mode powers_of_2|linear] # auto thread sweep
                        [--max-threads N] [--smt]
                        [--iterations N] [--dry-run] [--verbose]
                        [--arg key=value ...]
```

Three ways to define the sweep (in priority order):

| Mode | Flag | Description |
|---|---|---|
| Named configs | `--configs 1c1t,1c2t,2c2t,2c4t` | Specific presets in order; distinguishes `1c2t` (SMT pair) from `2c2t` (two physical cores) |
| Explicit threads | `--threads 1,2,4,8,16` | Fixed thread counts; CPUs auto-selected (physical cores first) |
| Auto sweep | `--mode powers_of_2` or `--mode linear` | Generates thread counts up to `--max-threads` (default: all physical cores) |

`--smt` applies only to `--threads` / `--mode` sweeps; it causes SMT siblings to be included when filling thread slots.

**Examples:**

```bash
# Compare SMT topology: 1 core no-SMT vs 1 core SMT vs 2 cores no-SMT vs 2 cores SMT
python3 main.py scaling --workload python_bench --configs 1c1t,1c2t,2c2t,2c4t

# Powers-of-2 sweep up to all physical cores
python3 main.py scaling --workload sysbench_cpu --mode powers_of_2

# Linear sweep, 1 to 8 threads
python3 main.py scaling --workload sysbench_cpu --mode linear --max-threads 8

# Explicit thread list, 3 iterations each
python3 main.py scaling --workload python_bench --threads 1,2,4,8,16 --iterations 3

# Full socket scaling including SMT, with custom workload args
python3 main.py scaling --workload sysbench_cpu --mode powers_of_2 --smt \
    --arg prime=20000 time=15

# SMT vs physical comparison for memory bandwidth
python3 main.py scaling --workload stream --configs 8c8t,8c16t,16c16t,16c32t
```

Output for a named-config study:

```
Scaling study: python_bench (ops_per_sec)
Config | Threads | ops_per_sec | Speedup | Efficiency
-------+---------+-------------+---------+-----------
  1c1t |       1 |       299.9 |   1.00x |       100%
  1c2t |       2 |       282.1 |   0.94x |        47%
  2c2t |       2 |       602.2 |   2.01x |       100%
  2c4t |       4 |       560.2 |   1.87x |        47%
```

Speedup and efficiency are always relative to the first config in the list (or thread count = 1 for thread sweeps).

---

### `report`

View or export saved results.

```bash
# List all saved runs
python3 main.py report --list

# Show summary for a specific run
python3 main.py report --run-id 20260508_185406_python_bench_full_socket

# Show most recent run (no flags)
python3 main.py report

# Export one run to CSV
python3 main.py report --run-id <run_id> --csv output.csv

# Export all saved runs to CSV
python3 main.py report --csv all_results.csv
```

CSV columns: `run_id`, `workload`, `config_name`, `num_threads`, `metric_name`, `value`, `timestamp`.

---

## Configurations

### NcMT Naming Convention

Configurations follow the `NcMT` convention: **N** physical cores, **M** total threads.

| Preset | Threads | CPU pinning | Description |
|---|---|---|---|
| `1c1t` | 1 | `[0]` | 1 core, physical thread only |
| `1c2t` | 2 | `[0, 16]` | 1 core, both SMT siblings |
| `2c2t` | 2 | `[0, 1]` | 2 cores, 1 thread each |
| `2c4t` | 4 | `[0, 1, 16, 17]` | 2 cores, both threads each |
| `4c4t` | 4 | `[0, 1, 2, 3]` | 4 cores, no SMT |
| `4c8t` | 8 | `[0–3, 16–19]` | 4 cores, both threads |
| `8c8t` | 8 | `[0–7]` | 8 cores, no SMT |
| `8c16t` | 16 | `[0–7, 16–23]` | 8 cores, both threads |
| `16c16t` | 16 | `[0–15]` | Full socket, no SMT |
| `16c32t` | 32 | `[0–31]` | Full socket, all threads |

The exact presets generated depend on the machine's topology (cores per socket, SMT width). Run `list-configs` to see what's available on the current host.

### Legacy Aliases

These named presets are retained for convenience and map directly to NcMT equivalents:

| Alias | Equivalent |
|---|---|
| `single_core` | `1c1t` |
| `dual_core` | `2c2t` |
| `quad_core` | `4c4t` |
| `octa_core` | `8c8t` |
| `half_socket` | `8c8t` (half of total physical cores) |
| `full_socket` | `16c16t` |
| `full_socket_smt` | `16c32t` |

### How CPU Pinning Works

- All runs use `taskset -c <cpu_list>` to pin the process to the listed logical CPUs.
- For NcMT presets, physical cores are always filled first; SMT siblings are added only when `M > N`.
- `numactl --cpunodebind --membind` is used when `use_numactl=True` (off by default; set via `BenchmarkConfig`).
- Environment variables `OMP_NUM_THREADS` and `GOMP_NUM_THREADS` are set to `num_threads` for every run.

---

## Workloads

### `python_bench` — Pure Python CPU

**Dependencies:** None (always available)

Runs a Sieve of Eratosthenes in N parallel worker processes for a fixed duration. Reports total operations per second across all workers.

```bash
python3 main.py run --workload python_bench --config full_socket
```

| Argument | Default | Description |
|---|---|---|
| `duration` | `10` | Run time in seconds per worker |
| `limit` | `1000000` | Sieve upper bound (higher = harder) |

```bash
# Longer run, larger sieve
python3 main.py run --workload python_bench --config 8c8t \
    --arg duration=30 limit=5000000
```

**Metrics:** `ops_per_sec`, `total_ops`, `duration_sec`

---

### `sysbench_cpu` — sysbench CPU

**Dependencies:** `apt install sysbench` / `yum install sysbench`

Runs sysbench's CPU workload: find all prime numbers up to `prime` using multiple threads. Reports events (prime checks) per second.

```bash
python3 main.py run --workload sysbench_cpu --config full_socket --iterations 3
```

| Argument | Default | Description |
|---|---|---|
| `prime` | `20000` | Upper bound for prime search |
| `time` | `10` | Duration in seconds |

```bash
# Heavier workload for 30 seconds
python3 main.py run --workload sysbench_cpu --config 4c4t --arg prime=50000 time=30
```

**Metrics:** `events_per_sec`, `total_events`, `total_time_sec`, `latency_min_ms`, `latency_avg_ms`, `latency_max_ms`, `latency_p95_ms`

---

### `stream` — STREAM Memory Bandwidth

**Dependencies:** A compiled STREAM binary named `stream_omp`, `stream_c`, or `stream` in `PATH` or `--work-dir`.

Build from source:

```bash
# Download
wget https://www.cs.virginia.edu/stream/FTP/Code/stream.c

# Compile with OpenMP (recommended for multi-threaded runs)
gcc -O3 -march=native -fopenmp -DSTREAM_ARRAY_SIZE=80000000 \
    -o stream_omp stream.c
```

Place the binary in your `PATH` or pass `--work-dir /path/to/binary/dir`.

```bash
python3 main.py run --workload stream --config full_socket
```

Thread count is controlled via `OMP_NUM_THREADS` (set automatically from the config). The STREAM array size can be overridden:

```bash
python3 main.py run --workload stream --config 16c16t --arg array_size=200000000
```

**Metrics:** `copy_mb_s`, `scale_mb_s`, `add_mb_s`, `triad_mb_s`

---

### `fio` — FIO I/O Benchmark

**Dependencies:** `apt install fio` / `yum install fio`

Flexible I/O benchmark for storage. Runs in JSON output mode; results are aggregated across all parallel jobs.

```bash
python3 main.py run --workload fio --config 4c4t
```

| Argument | Default | Description |
|---|---|---|
| `rw` | `randread` | I/O pattern: `read`, `write`, `randread`, `randwrite`, `randrw`, `rw` |
| `bs` | `4k` | Block size |
| `size` | `1G` | File size per job |
| `runtime` | `30` | Duration in seconds |
| `directory` | `/tmp` | Directory for test files |
| `name` | `benchmark` | FIO job name |

```bash
# Sequential write to NVMe
python3 main.py run --workload fio --config 8c8t \
    --arg rw=write bs=1m size=8G directory=/mnt/nvme runtime=60

# Random read IOPS scaling study
python3 main.py scaling --workload fio --configs 1c1t,2c2t,4c4t,8c8t \
    --arg rw=randread bs=4k size=2G directory=/tmp
```

**Metrics:** `read_iops`, `read_bw_kb_s`, `write_iops`, `write_bw_kb_s`, `read_lat_us`, `write_lat_us`

---

## Results

Every run is saved to `results/<run_id>/results.json`. The run ID encodes the timestamp, workload name, and config name:

```
results/
  20260508_185406_python_bench_full_socket/
    results.json
  scaling_20260508_182437_python_bench/
    results.json
  scaling_20260508_182437_python_bench_t1/
    results.json
  ...
```

A scaling study creates one top-level directory for the combined results plus one sub-directory per thread count or config. The top-level file stores all iterations across all configs and is what `report` and `print_scaling_table` operate on.

The `results.json` schema:

```json
{
  "run_id": "...",
  "meta": { "study_type": "named_configs", "config_order": ["1c1t", "1c2t"] },
  "results": [
    {
      "workload": "python_bench",
      "config_name": "1c1t",
      "num_threads": 1,
      "cpu_list": [0],
      "metrics": { "ops_per_sec": 299.9, "total_ops": 2999.0 },
      "wall_time": 10.03,
      "iteration": 0,
      "success": true,
      "command": ["taskset", "-c", "0", "..."]
    }
  ],
  "summary": {
    "ops_per_sec": { "mean": 300.1, "median": 299.9, "stdev": 1.2, "min": 298.8, "max": 302.1, "samples": 3 }
  },
  "num_results": 3,
  "num_successful": 3
}
```

---

## Adding a Workload

1. Create a new file in `workloads/`, e.g. `workloads/my_bench.py`.
2. Define a class that inherits from `BaseWorkload` and sets a unique `name`.
3. Implement the four required methods.

The workload is auto-discovered at startup — no registration or imports needed anywhere else.

### Minimal example

```python
# workloads/my_bench.py
import shutil
import re
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from benchmark_toolkit.base import BaseWorkload
from benchmark_toolkit.config import BenchmarkConfig


class MyBench(BaseWorkload):
    name = "my_bench"
    description = "My custom benchmark"
    version = "1.0"

    def validate(self):
        ok = shutil.which("mybinary") is not None
        return ok, "mybinary found" if ok else "mybinary not found — install it"

    def build_command(self, config):
        args = {**self.default_workload_args(), **config.workload_args}
        return [
            "mybinary",
            f"--threads={config.num_threads}",
            f"--duration={args['duration']}",
        ]

    def parse_output(self, stdout, stderr, returncode):
        metrics = {}
        for line in stdout.splitlines():
            m = re.search(r"throughput:\s+([\d.]+)", line)
            if m:
                metrics["throughput"] = float(m.group(1))
        return metrics

    def default_workload_args(self):
        return {"duration": 10}
```

After saving the file, it appears immediately in `list-workloads`:

```bash
python3 main.py list-workloads
python3 main.py validate --workload my_bench
python3 main.py run --workload my_bench --config full_socket --iterations 3
python3 main.py scaling --workload my_bench --configs 1c1t,2c2t,4c4t,8c8t
```

### BaseWorkload interface

All methods in `BaseWorkload` (`benchmark_toolkit/base.py`):

| Method | Required | Description |
|---|---|---|
| `validate()` | Yes | Return `(bool, str)`. Check that required binaries exist. |
| `build_command(config)` | Yes | Return `argv` list. Do not include `taskset`/`numactl`; the runner prepends those. |
| `parse_output(stdout, stderr, returncode)` | Yes | Return `{metric: float}`. Empty dict marks the run as failed. |
| `setup(config, work_dir)` | No | Runs once before the first iteration. Compile, download data, write helper scripts, etc. |
| `teardown(config, work_dir)` | No | Runs once after the last iteration. Clean up temp files. |
| `get_env(config)` | No | Return env var overrides merged on top of `os.environ`. Default sets `OMP_NUM_THREADS`. |
| `default_workload_args()` | No | Return default `{key: value}` dict. Merged with (and overridden by) `config.workload_args`. |

### Passing workload arguments

The `--arg` flag on `run` and `scaling` populates `config.workload_args`. Inside `build_command`, merge defaults first so CLI args always win:

```python
def build_command(self, config):
    args = {**self.default_workload_args(), **config.workload_args}
    return ["mybinary", f"--size={args['size']}"]

def default_workload_args(self):
    return {"size": "1G", "rw": "randread"}
```

```bash
# Uses default size=1G
python3 main.py run --workload my_bench --config 4c4t

# Overrides size at the CLI
python3 main.py run --workload my_bench --config 4c4t --arg size=8G rw=write
```

Values are auto-cast to `int` or `float` when possible; otherwise kept as strings.

---

## Scaling Studies in Depth

### Thread-count sweep

Auto-generates thread counts and assigns CPUs using physical-first ordering (no SMT unless `--smt`):

```bash
# 1, 2, 4, 8, 16 threads (powers of 2 up to all physical cores)
python3 main.py scaling --workload sysbench_cpu --mode powers_of_2

# 1 through 16 threads (linear)
python3 main.py scaling --workload sysbench_cpu --mode linear --max-threads 16

# Include SMT — fills 1, 2, 4, ..., 32 logical CPUs
python3 main.py scaling --workload sysbench_cpu --mode powers_of_2 --smt
```

### Named-config sweep

Explicitly specifies which presets to run in order. This is required when you want to compare configurations that have the same thread count but different topologies (e.g. `1c2t` vs `2c2t`):

```bash
# SMT vs physical core comparison
python3 main.py scaling --workload sysbench_cpu \
    --configs 1c1t,1c2t,2c2t,2c4t,4c4t,4c8t,8c8t,8c16t,16c16t,16c32t

# Investigate specific configs only
python3 main.py scaling --workload stream \
    --configs 8c8t,8c16t,16c16t,16c32t --iterations 5
```

### Interpreting the results

- **Speedup** = `value(N) / value(baseline)` where baseline is the first config or T=1.
- **Efficiency** = `speedup / num_threads × 100%`. 100% means perfect linear scaling; values below indicate overhead or resource contention.
- For SMT configs like `1c2t`, efficiency is computed against the total thread count (2), so values around 50% indicate that SMT adds half the throughput of a real core — which is typical for compute-bound workloads.

---

## Environment Variables Set Per Run

| Variable | Value | Notes |
|---|---|---|
| `OMP_NUM_THREADS` | `config.num_threads` | Controls OpenMP thread count |
| `GOMP_NUM_THREADS` | `config.num_threads` | GCC OpenMP alias |
| Any in `config.env_vars` | As specified | Set via `BenchmarkConfig.env_vars` |

Workloads can override `get_env()` to add or suppress variables (e.g. `python_bench` omits `OMP_NUM_THREADS` since it controls parallelism via `multiprocessing`).
