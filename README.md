# Garuda — Workload Automation Toolkit

A Python toolkit for measuring Linux kernel and system performance across CPU, memory, networking, and I/O. Workloads are pinned to specific cores via `taskset` and `numactl`, and the built-in scaling study engine sweeps across configurations automatically.

Includes dedicated benchmarks for every major Linux kernel subsystem: scheduler latency, memory allocation, memory hierarchy, address translation, TCP/IP stack, socket latency, and the virtual filesystem.

A cloud orchestrator layer sits on top: it can provision a VM on **Google Cloud (GCP)**, **Microsoft Azure**, or **Amazon AWS**, deploy the toolkit over SSH, run the benchmarks remotely, stream results back, and optionally tear down the VM — all from a single command.

A **kernel analysis layer** sits on top of everything: `kernel-analyze` installs multiple kernel versions, reboots into each one automatically using a systemd service, runs the full workload suite, and pushes results to the [Garuda Kernel Ledger](portal/README.md). `cloud-kernel-analyze` does the same on a cloud VM, polling via SSH across reboots until all kernels are benchmarked, then fetches results and prints a composite score report.

---

## Requirements

### Local machine

- Python 3.9+
- Linux (reads CPU topology from `/sys/devices/system/cpu/`)
- `taskset` (util-linux) and `numactl` — present on most distros
- `rsync` — for syncing the toolkit to cloud VMs

Optional Python packages (richer table output, no functional impact if missing):

```
pip install tabulate
```

### Cloud orchestration (only needed for `cloud-*` commands)

| Provider | CLI tool | Auth |
|---|---|---|
| GCP | `gcloud` (Google Cloud SDK) | `gcloud auth login` |
| Azure | `az` (Azure CLI) | `az login` |
| AWS | `aws` (AWS CLI v2) | `aws configure` |

An SSH key pair is required (`~/.ssh/id_rsa` + `~/.ssh/id_rsa.pub` by default). For AWS, you also need an existing EC2 key pair name in your account.

No other Python dependencies are required. The `python_bench` workload works out of the box. All other workloads require their respective system binaries (see [Workloads](#workloads)).

---

## Directory Structure

```
tools/
├── main.py                     # CLI entry point (local + cloud commands)
├── requirements.txt
├── benchmark_toolkit/          # Core library
│   ├── system.py               # CPU/NUMA topology detection
│   ├── config.py               # BenchmarkConfig + ConfigPreset factory
│   ├── base.py                 # BaseWorkload abstract class
│   ├── registry.py             # Auto-discovery of workloads/
│   ├── runner.py               # Execution engine (taskset, numactl, timing)
│   ├── collector.py            # Results → JSON + CSV + summary stats
│   └── scaling.py              # Scaling study orchestrator
├── orchestrator/               # Cloud orchestration layer
│   ├── base.py                 # VMConfig, VMInstance dataclasses + CloudProvider ABC
│   ├── gcp.py                  # GCPProvider  (gcloud CLI)
│   ├── azure.py                # AzureProvider (az CLI)
│   ├── aws.py                  # AWSProvider  (aws CLI)
│   ├── remote.py               # RemoteExecutor — SSH + rsync
│   ├── runner.py               # CloudBenchmarkRunner — end-to-end lifecycle
│   └── state.py                # VMStateStore — persists VM info locally
├── kernel_analysis/            # Multi-kernel benchmarking layer
│   ├── state.py                # AnalysisSession + KernelEntry — persisted JSON state machine
│   ├── installer.py            # apt/GitHub/tarball kernel install, grub.cfg parser, grub-reboot
│   ├── scorer.py               # Composite score computation + formatted report
│   └── service.py              # Systemd oneshot service install/remove
├── workloads/                  # Drop new .py files here to add workloads
│   ├── python_bench.py         # Pure Python (always works, no deps)
│   ├── sysbench.py             # sysbench CPU
│   ├── stream.py               # STREAM memory bandwidth
│   ├── multichase.py           # Memory latency (pointer chasing)
│   ├── fio_bench.py            # FIO I/O benchmark
│   ├── sysbench_mysql.py       # sysbench OLTP against MySQL in Docker
│   │
│   │   ── Linux kernel subsystem benchmarks ──
│   ├── schbench.py             # Scheduler: wakeup-latency percentiles
│   ├── hackbench.py            # Scheduler: task-communication throughput
│   ├── cyclictest.py           # Scheduler: RT timer latency
│   ├── mem_lat.py              # Memory: hierarchy latency (L1→DRAM) + TLB
│   ├── mem_alloc.py            # Memory: allocation throughput (malloc/mmap/brk)
│   ├── iperf3.py               # Networking: loopback TCP throughput
│   ├── sockperf.py             # Networking: socket round-trip latency
│   └── fs_mark.py              # VFS: file metadata throughput
└── results/                    # Auto-created; one subdirectory per run
    └── cloud_vms.json          # Tracked cloud VMs (written by cloud-provision)
```

---

## Quick Start

### Local benchmarks

```bash
# 1. Check what's installed and what needs setup
python3 main.py setup --list

# 2. Install all missing workloads (or just one)
python3 main.py setup
python3 main.py setup --workload multichase

# 3. Confirm everything is ready
python3 main.py validate

# 4. Run the built-in Python benchmark on all physical cores
python3 main.py run --workload python_bench --config full_socket --iterations 3

# 5. Run a scaling study
python3 main.py scaling --workload python_bench --mode powers_of_2

# 6. Compare SMT vs physical cores
python3 main.py scaling --workload python_bench --configs 1c1t,1c2t,2c2t,2c4t

# 7. Run multiple workloads across multiple configs in one shot
python3 main.py multi-run \
  --workloads stream,sysbench_cpu,python_bench \
  --configs single_core,full_socket --iterations 3

# 8. List and inspect saved results
python3 main.py report --list
python3 main.py report --run-id <run_id>
```

### Kernel analysis (local)

Automatically install and benchmark across multiple kernel versions. Requires root — uses `grub-reboot` and a systemd service to survive across reboots. Kernels can come from apt, a GitHub branch, or a tar.gz URL.

```bash
# Benchmark three apt kernels on fio + stream, push to Kernel Ledger
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,6.11.0-25-generic,6.12.0-10-generic \
  --workloads fio,stream,hackbench \
  --iterations 5 \
  --push-url http://perf.example.com \
  --api-key my-secret-key

# Compare an apt kernel against mainline built from GitHub
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,github:torvalds/linux:master@v6.15-rc1 \
  --workloads schbench,hackbench,mem_lat --iterations 3

# Build from a tar.gz URL
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,tarball:https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.tar.gz@6.13.0 \
  --workloads fio,stream --iterations 3

# Dry run — see the plan without touching anything
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,github:torvalds/linux:master@v6.15-rc1 \
  --workloads fio --dry-run

# Check status of an in-progress analysis
sudo python3 main.py kernel-analyze --status

# Abort a running analysis
sudo python3 main.py kernel-analyze --abort
```

After all kernels complete, a score report is printed. Baseline kernel = 100; higher is better.

### Kernel analysis (cloud)

Same pipeline on a cloud VM — the orchestrator polls SSH every 30 s and reconnects transparently across reboots.

```bash
# GCP — three kernels, auto-push to Kernel Ledger
python3 main.py cloud-kernel-analyze \
  --provider gcp --region us-central1 --instance-type n2-standard-4 \
  --kernels 6.8.0-55-generic,6.11.0-25-generic,6.12.0-10-generic \
  --workloads fio,stream,hackbench \
  --iterations 5 \
  --push-url http://perf.example.com --api-key my-key

# AWS — keep VM running after analysis (for manual inspection)
python3 main.py cloud-kernel-analyze \
  --provider aws --region us-east-1 \
  --instance-type m5.xlarge --aws-key-name my-keypair \
  --kernels 6.8.0-55-generic,6.12.0-10-generic \
  --workloads hackbench,schbench,stream \
  --config 4c4t --iterations 3 --no-teardown
```

### Cloud benchmarks

```bash
# GCP — spin up a VM, run a benchmark, fetch results, destroy the VM
python3 main.py cloud-run \
  --provider gcp --region us-central1 --instance-type n2-standard-4 \
  --workload python_bench --config full_socket --iterations 3

# AWS — scaling study on a persistent VM (kept running for further use)
python3 main.py cloud-run \
  --provider aws --region us-east-1 \
  --instance-type m5.xlarge --aws-key-name my-keypair \
  --workload sysbench_cpu --scaling --max-threads 16 --no-teardown

# List VMs you've provisioned
python3 main.py cloud-list

# Run another benchmark on that same VM (toolkit already deployed)
python3 main.py cloud-exec --vm-name benchmark-abc12345 \
  --workload stream --config full_socket --skip-setup

# Destroy when done
python3 main.py cloud-destroy --vm-name benchmark-abc12345
```

---

## CLI Reference

All commands are invoked as:

```
python3 main.py [--work-dir DIR] COMMAND [options]
```

`--work-dir` (default: `/tmp/benchmark_toolkit`) is where temporary files such as worker scripts are written.

---

### `setup`

Download, build, and install benchmark binaries into the local `bin/` directory. Once installed, binaries are found automatically by `validate` and `run` without any PATH changes needed.

```bash
python3 main.py setup [--workload NAME] [--install-dir DIR] [--force] [--list]
```

| Flag | Description |
|---|---|
| `--list` | Show install status and method for each workload — no changes made |
| `--workload NAME` | Install a specific workload only (default: all missing workloads) |
| `--install-dir DIR` | Where to write compiled binaries (default: `bin/` inside the toolkit root) |
| `--force` | Reinstall even if the binary is already present |

**Examples:**

```bash
# Show current status without installing anything
python3 main.py setup --list

# Install all workloads that are missing
python3 main.py setup

# Install only multichase
python3 main.py setup --workload multichase

# Recompile stream with a fresh download
python3 main.py setup --workload stream --force

# Install to a custom directory
python3 main.py setup --workload stream --install-dir /opt/benchmarks/bin
```

**What each workload does:**

| Workload | Install method | Package / source |
|---|---|---|
| `python_bench` | No install needed | Python only |
| `sysbench_cpu` | Package manager | `sysbench` |
| `stream` | Download + compile | `stream.c` + `gcc -fopenmp` |
| `multichase` | `git clone` + `make` | github.com/google/multichase |
| `fio` | Package manager | `fio` |
| `sysbench_mysql` | Package manager + Docker | `sysbench`, `docker` |
| `schbench` | Package manager | `schbench` |
| `hackbench` | Package manager | `rt-tests` |
| `cyclictest` | Package manager | `rt-tests` |
| `mem_lat` | Package manager | `lmbench` |
| `mem_alloc` | Package manager | `stress-ng` |
| `iperf3` | Package manager | `iperf3` |
| `sockperf` | Package manager | `sockperf` |
| `fs_mark` | Package manager | `fs-mark` |

The `STREAM_ARRAY_SIZE` is auto-detected from the system's L3 cache size (targeting 4× L3) so the arrays always fit in DRAM during the benchmark.

Binaries land in `bin/` (relative to `main.py`). This directory is prepended to `PATH` at startup, so `validate` and `run` find them without any shell configuration.

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

### `multi-run`

Run multiple workloads across multiple configurations as a full matrix — every (workload, config) pair — and print a result table when done.

```bash
python3 main.py multi-run --workloads NAME,NAME,... \
                          [--configs PRESET,... | --threads N,N,...] \
                          [--iterations N] [--dry-run] [--verbose] [--stop-on-error] \
                          [--arg key=value ...] \
                          [--push-url URL] [--api-key KEY] [--kernel VERSION] [--kernel-config LABEL]
```

| Flag | Default | Description |
|---|---|---|
| `--workloads NAME,...` | (required) | Comma-separated workload names |
| `--configs PRESET,...` | `single_core` | Named config presets to sweep (see `list-configs`) |
| `--threads N,N,...` | — | Explicit thread counts instead of named presets |
| `--iterations N` | `1` | Iterations per (workload, config) combination |
| `--dry-run` | off | Print commands without executing |
| `--verbose` | off | Print per-iteration metrics as they complete |
| `--stop-on-error` | off | Abort the matrix after the first failed combination |
| `--arg key=value` | — | Workload-specific arguments applied to every run |
| `--push-url URL` | — | Push all successful runs to this Kernel Ledger URL after the matrix completes |
| `--api-key KEY` | `$GARUDA_API_KEY` | Ledger push API key |
| `--kernel VERSION` | `uname -r` | Kernel version string to tag pushed results with |
| `--kernel-config LABEL` | `unknown` | Kernel config label stored in the ledger |

**Examples:**

```bash
# 3 workloads × 2 configs = 6 runs, 3 iterations each
python3 main.py multi-run \
  --workloads stream,sysbench_cpu,python_bench \
  --configs single_core,full_socket \
  --iterations 3

# Custom thread counts instead of named presets
python3 main.py multi-run \
  --workloads hackbench,schbench \
  --threads 1,4,8,16 --iterations 5

# Single workload against several configs with a shared argument
python3 main.py multi-run --workloads fio \
  --configs 1c1t,2c2t,4c4t,8c8t --arg rw=randread bs=4k

# Dry run — preview every command that would be executed
python3 main.py multi-run \
  --workloads stream,fio --configs single_core,full_socket --dry-run

# Run matrix then push all successful results to Kernel Ledger
python3 main.py multi-run \
  --workloads stream,sysbench_cpu,hackbench \
  --configs single_core,full_socket --iterations 3 \
  --push-url http://perf.example.com --api-key my-key
```

**Output format:**

```
Multi-run: 3 workload(s) × 2 config(s) = 6 run(s)
  Workloads : stream, sysbench_cpu, python_bench
  Configs   : single_core, full_socket
  Iterations: 3

[1/6] stream / single_core  (1 thread(s), 3 iter)
  iter=0 [OK] wall=5.12s  triad_mb_s=28432
  ...

────────────────────────────────────────────────────────────────
Multi-run complete: 6/6 combination(s) succeeded
────────────────────────────────────────────────────────────────

Primary metric (mean) per cell:
Workload              single_core    full_socket
──────────────────────────────────────────────
stream                      28432          91847
sysbench_cpu                 2341           7203
python_bench                  299            968

Run IDs:
  stream/single_core          3/3       20260511_101523_stream_single_core
  stream/full_socket          3/3       20260511_101528_stream_full_socket
  ...
```

`--configs` and `--threads` are mutually exclusive. If neither is given, `single_core` is used. `--threads` automatically selects physical cores first (no SMT).

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

## Cloud Orchestration

The `orchestrator/` package adds a provider-agnostic layer that:

1. **Provisions** a VM on GCP, Azure, or AWS using the respective CLI tool
2. **Waits** for the VM to reach running state and for SSH to become available
3. **Deploys** the benchmark toolkit via `rsync`
4. **Installs** workload dependencies on the remote VM (`python3 main.py setup`)
5. **Runs** any `run` or `scaling` command remotely and streams output
6. **Fetches** the `results/` directory back to the local machine
7. **Destroys** the VM (or keeps it with `--no-teardown`)

VM state is persisted to `results/cloud_vms.json` so provisioned VMs can be referenced by name in subsequent commands.

### Prerequisites

| Provider | CLI | Auth command |
|---|---|---|
| GCP | `gcloud` | `gcloud auth login && gcloud config set project PROJECT_ID` |
| Azure | `az` | `az login` |
| AWS | `aws` | `aws configure` |

SSH key:
- Default: `~/.ssh/id_rsa` (private) + `~/.ssh/id_rsa.pub` (public)
- Override with `--ssh-key-path /path/to/key`
- AWS additionally requires `--aws-key-name` — the name of an EC2 key pair already registered in your account

### Common VM arguments

These flags are shared by `cloud-run` and `cloud-provision`:

| Flag | Default | Description |
|---|---|---|
| `--provider` | _(required)_ | `gcp`, `azure`, or `aws` |
| `--region` | _(required)_ | Provider region (e.g. `us-central1`, `eastus`, `us-east-1`) |
| `--instance-type` | _(required)_ | Machine type (e.g. `n2-standard-4`, `Standard_D4s_v3`, `m5.xlarge`) |
| `--ssh-key-path PATH` | `~/.ssh/id_rsa` | Local private key for SSH |
| `--ssh-user USER` | `ubuntu` | SSH username on the VM |
| `--vm-name NAME` | auto-generated | Custom name (e.g. `benchmark-perftest`) |
| `--disk-size GB` | `50` | Boot disk size in GB |
| `--image IMAGE` | Ubuntu 22.04 LTS | OS image override |
| `--gcp-zone ZONE` | `{region}-a` | GCP zone (e.g. `us-central1-b`) |
| `--gcp-project ID` | gcloud default | GCP project ID |
| `--aws-key-name NAME` | _(required for AWS)_ | EC2 key pair name |
| `--aws-profile NAME` | AWS CLI default | Named AWS CLI profile |
| `--azure-resource-group RG` | `benchmark-rg-{region}` | Azure resource group (created if missing) |
| `--azure-subscription ID` | az default | Azure subscription ID |

### Common benchmark arguments

These flags are shared by `cloud-run` and `cloud-exec`:

| Flag | Default | Description |
|---|---|---|
| `--workload NAME` | _(required)_ | Workload to run |
| `--scaling` | off | Run a scaling study instead of a single benchmark run |
| `--config PRESET` | `single_core` | Config preset for single run |
| `--threads N` | — | Thread count for single run |
| `--iterations N` | `1` | Iterations per config |
| `--scaling-configs 1c1t,...` | — | Named presets for scaling study |
| `--scaling-mode` | `powers_of_2` | Thread sweep mode (`powers_of_2` or `linear`) |
| `--max-threads N` | all cores | Max threads for scaling sweep |
| `--smt` | off | Include SMT siblings in scaling |
| `--arg key=value` | — | Workload-specific arguments |

---

### `cloud-run`

Provision a VM, run benchmarks, fetch results, and destroy the VM — all in one shot.

```bash
python3 main.py cloud-run \
  --provider PROVIDER --region REGION --instance-type TYPE \
  --workload NAME [benchmark options] \
  [--no-teardown] [--verbose]
```

`--no-teardown` keeps the VM running and saves it to `results/cloud_vms.json` so you can reference it with `cloud-exec` and `cloud-destroy` later.

**Examples:**

```bash
# GCP — single run, full socket, 3 iterations
python3 main.py cloud-run \
  --provider gcp --region us-central1 --instance-type n2-standard-4 \
  --workload python_bench --config full_socket --iterations 3

# GCP — scaling study with custom workload args
python3 main.py cloud-run \
  --provider gcp --region us-central1 --instance-type n2-standard-8 \
  --workload sysbench_cpu --scaling --mode powers_of_2 --iterations 3 \
  --arg prime=50000 time=30

# Azure — memory bandwidth benchmark
python3 main.py cloud-run \
  --provider azure --region eastus --instance-type Standard_D8s_v3 \
  --workload stream --config full_socket --iterations 5

# AWS — scaling study, keep VM for further use
python3 main.py cloud-run \
  --provider aws --region us-east-1 \
  --instance-type m5.2xlarge --aws-key-name my-keypair \
  --workload sysbench_cpu --scaling --max-threads 8 \
  --no-teardown

# AWS — specific SSH key and custom VM name
python3 main.py cloud-run \
  --provider aws --region us-west-2 \
  --instance-type c5.4xlarge --aws-key-name perf-key \
  --ssh-key-path ~/.ssh/perf_key \
  --vm-name my-perf-vm \
  --workload python_bench --config full_socket
```

**End-to-end flow:**
```
[cloud] Creating n2-standard-4 on gcp (us-central1)...
[cloud] VM 'benchmark-a3f2c1b0' created, waiting for running state...
[cloud] VM running at 34.56.78.90. Waiting for SSH...
[cloud] SSH ready.
[cloud] Installing system dependencies (python3, pip3, rsync)...
[cloud] Syncing toolkit to remote:~/benchmark_toolkit ...
[cloud] Toolkit deployed.
[cloud] Setting up workload 'python_bench' on remote VM...
[cloud] Workload 'python_bench' ready.
[cloud] Running: python3 main.py run --workload python_bench --config full_socket --iterations 3
  ... benchmark output ...
[cloud] Fetching results → ./results ...
[cloud] Results downloaded.
[cloud] VM 'benchmark-a3f2c1b0' destroyed.
```

---

### `cloud-provision`

Provision a VM and save it to the state store without running any benchmarks. Useful when you want to run multiple benchmark rounds against the same VM.

```bash
python3 main.py cloud-provision \
  --provider PROVIDER --region REGION --instance-type TYPE \
  [VM options]
```

**Examples:**

```bash
# GCP
python3 main.py cloud-provision \
  --provider gcp --region us-central1 --instance-type n2-standard-4

# Azure with explicit resource group
python3 main.py cloud-provision \
  --provider azure --region westeurope --instance-type Standard_D4s_v3 \
  --azure-resource-group my-benchmarks-rg

# AWS
python3 main.py cloud-provision \
  --provider aws --region ap-southeast-1 \
  --instance-type m5.xlarge --aws-key-name singapore-key
```

**Output:**
```
VM 'benchmark-a3f2c1b0' is ready.
  Provider:  gcp
  Type:      n2-standard-4
  IP:        34.56.78.90
  SSH:       ssh -i ~/.ssh/id_rsa ubuntu@34.56.78.90

  Run bench: python3 main.py cloud-exec --vm-name benchmark-a3f2c1b0 --workload <name>
  Destroy:   python3 main.py cloud-destroy --vm-name benchmark-a3f2c1b0
```

---

### `cloud-exec`

Run benchmarks on a VM that was previously provisioned with `cloud-provision` or kept with `cloud-run --no-teardown`.

```bash
python3 main.py cloud-exec \
  --vm-name NAME --workload NAME [benchmark options] \
  [--skip-setup] [--verbose]
```

`--skip-setup` skips toolkit sync and workload binary installation — use this when the VM is already set up and you just want to run another benchmark.

**Examples:**

```bash
# Full setup + benchmark (first time running on this VM)
python3 main.py cloud-exec \
  --vm-name benchmark-a3f2c1b0 \
  --workload python_bench --config full_socket --iterations 3

# Second run — skip setup since the toolkit is already deployed
python3 main.py cloud-exec \
  --vm-name benchmark-a3f2c1b0 \
  --workload sysbench_cpu --scaling --mode powers_of_2 \
  --skip-setup

# FIO benchmark on the same VM
python3 main.py cloud-exec \
  --vm-name benchmark-a3f2c1b0 \
  --workload fio --config 4c4t --arg rw=randread bs=4k size=2G \
  --skip-setup
```

---

### `cloud-destroy`

Terminate and delete a tracked cloud VM, and remove it from `results/cloud_vms.json`.

```bash
python3 main.py cloud-destroy --vm-name NAME
```

**Example:**

```bash
python3 main.py cloud-destroy --vm-name benchmark-a3f2c1b0
```

For GCP this deletes the instance. For Azure this deletes the entire resource group (VM + disk + NIC + public IP). For AWS this terminates the instance.

---

### `cloud-list`

List all VMs currently tracked in `results/cloud_vms.json`. State shown is from provisioning time; use `cloud-exec` or SSH directly to verify current state.

```bash
python3 main.py cloud-list
```

**Example output:**
```
Name                  Provider  Type             Region       IP            State
benchmark-a3f2c1b0    gcp       n2-standard-4    us-central1  34.56.78.90   RUNNING
benchmark-b7d9e2f1    aws       m5.xlarge         us-east-1    54.23.11.88   running

2 tracked VM(s).  State shown is from provisioning time.
```

---

### Confidential Computing

Pass `--confidential` to any `cloud-run` or `cloud-provision` command to launch a **Confidential VM** backed by hardware-level memory encryption. The benchmarks then run inside a hardware-attested, isolated environment.

```bash
# GCP — AMD SEV-SNP on an N2D instance
python3 main.py cloud-run \
  --provider gcp --region us-central1 --instance-type n2d-standard-4 \
  --confidential --confidential-type SEV_SNP \
  --workload python_bench --config full_socket

# Azure — Confidential VM with full disk encryption
python3 main.py cloud-run \
  --provider azure --region eastus --instance-type Standard_DC4as_v5 \
  --confidential --confidential-type DiskWithVMGuestState \
  --workload sysbench_cpu --config full_socket --iterations 3

# AWS — AMD SEV-SNP on an M6a instance
python3 main.py cloud-run \
  --provider aws --region us-east-1 \
  --instance-type m6a.xlarge --aws-key-name my-keypair \
  --confidential --confidential-type SevSnp \
  --workload python_bench --config full_socket
```

#### `--confidential-type` values per provider

| Provider | Value | Technology | Notes |
|---|---|---|---|
| GCP | `SEV` _(default)_ | AMD SEV | N2D or C2D machine types |
| GCP | `SEV_SNP` | AMD SEV-SNP | N2D machine types |
| GCP | `TDX` | Intel TDX | C3 machine types |
| Azure | `VMGuestStateOnly` _(default)_ | AMD SEV-SNP | Encrypts VM guest state |
| Azure | `DiskWithVMGuestState` | AMD SEV-SNP | Encrypts disk + VM guest state |
| AWS | `SevSnp` _(default)_ | AMD SEV-SNP | `--cpu-options AmdSevSnp=enabled` |
| AWS | `NitroEnclave` | AWS Nitro | Isolated enclave within the instance |

#### Required instance types

| Provider | Type | Required machine family |
|---|---|---|
| GCP SEV / SEV_SNP | `n2d-standard-*`, `c2d-standard-*` | `n2d-` or `c2d-` prefix |
| GCP TDX | `c3-standard-*` | `c3-` prefix |
| Azure CVM | `Standard_DC*as_v5`, `Standard_EC*as_v5` | DCasv5 / ECasv5 family |
| AWS SEV-SNP | `m6a.*`, `c6a.*`, `r6a.*`, `m7a.*`, `c7a.*`, `r7a.*` | AMD EPYC families |
| AWS Nitro Enclave | Any Nitro-based instance | Most modern instance types |

#### Provider-specific notes

**GCP**
- Live migration is automatically disabled (`--maintenance-policy=TERMINATE`) — instances will stop on host maintenance events. Set appropriate restart policies if needed.
- The default Ubuntu 22.04 LTS image works for SEV and SEV_SNP. TDX also uses the same image on GCP.
- Validate launch with: `gcloud compute instances get-shielded-instance-identity INSTANCE_NAME --zone ZONE`

**Azure**
- A CVM-compatible image (`Canonical:ubuntu-24_04-lts:cvm:latest`) is selected automatically when `--confidential` is used and no `--image` override is given.
- `VMGuestStateOnly` encrypts only the VM state; `DiskWithVMGuestState` also encrypts the OS disk — use the latter for stronger isolation at a small performance cost.
- Verify attestation with: `az vm show --name NAME --resource-group RG --query securityProfile`

**AWS**
- AMD SEV-SNP requires an instance from the m6a, c6a, r6a, m7a, c7a, or r7a families. The flag `AmdSevSnp=enabled` is passed via `--cpu-options`.
- Nitro Enclaves are an isolated compute partition *within* the instance (not full-instance memory encryption). They require a Nitro-based instance and the enclave application to run inside the enclave process separately.
- Verify SNP attestation: use the AWS `GetAttestationDocument` API from inside the instance.

---

### Instance type recommendations

| Use case | GCP | Azure | AWS |
|---|---|---|---|
| CPU benchmark (4 cores) | `n2-standard-4` | `Standard_D4s_v3` | `m5.xlarge` |
| CPU benchmark (8 cores) | `n2-standard-8` | `Standard_D8s_v3` | `m5.2xlarge` |
| Memory bandwidth | `n2-standard-8` | `Standard_E8s_v3` | `r5.2xlarge` |
| High core count | `n2-standard-32` | `Standard_D32s_v3` | `m5.8xlarge` |
| Storage / FIO | `n2-standard-4` + persistent SSD | `Standard_D4s_v3` + Premium SSD | `i3.xlarge` (NVMe) |

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

### `multichase` — Memory Latency (Pointer Chasing)

**Dependencies:** `multichase` binary in PATH

Build from source:

```bash
git clone https://github.com/google/multichase
cd multichase && make
sudo cp multichase /usr/local/bin/
```

Runs pointer-chasing loops across a configurable list of arena sizes. Each arena is measured independently; the results form a latency-vs-memory-level profile showing L1, L2, L3, and DRAM latency in a single run.

```bash
# Full cache hierarchy sweep on one physical core
python3 main.py run --workload multichase --config 1c1t

# DRAM latency scaling study — how latency changes as threads compete for memory
python3 main.py scaling --workload multichase --configs 1c1t,2c2t,4c4t,8c8t,16c16t \
    --arg arena=256m
```

| Argument | Default | Description |
|---|---|---|
| `arena` | `64k,512k,4m,32m,128m,256m` | Comma-separated arena sizes (k/m/g suffix). Covers L1→DRAM on typical x86. |
| `samples` | `6` | Samples per arena size (each = 0.5 s; default = 3 s per arena) |
| `stride` | `256` | Stride between pointers in bytes |
| `mode` | `simple` | Chase mode: `simple`, `parallel2`–`parallel10`, `work:N`, `incr`, `branch` |
| `average` | `false` | `true` → report average latency; `false` → report best (minimum) latency |
| `hugepages` | `false` | Use transparent hugepages (`-H` flag) |

```bash
# Single DRAM measurement only (faster; useful for scaling studies)
python3 main.py run --workload multichase --config 4c4t --arg arena=256m samples=10

# Parallel chase mode — multiple independent pointer chains per thread
python3 main.py run --workload multichase --config 1c1t --arg mode=parallel4

# Custom arena sweep targeting this machine's cache hierarchy
python3 main.py run --workload multichase --config 1c1t \
    --arg arena=32k,512k,1m,8m,32m,64m,256m
```

**Metrics:** `latency_<size>_ns` for each arena (e.g. `latency_64k_ns`, `latency_256m_ns`), plus `latency_min_ns` (≈ L1 latency) and `latency_max_ns` (≈ DRAM latency).

For scaling studies, specify a single arena size with `--arg arena=256m` so the primary metric `latency_256m_ns` is comparable across all configs.

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

### `sysbench_mysql` — OLTP Benchmark against MySQL

**Dependencies:** `apt install sysbench` + Docker Engine running

Spawns a MySQL 8 Docker container, prepares sysbench OLTP tables inside it, runs the benchmark, then tears down the container. The MySQL server and the sysbench client can be pinned to disjoint CPU sets for clean per-core measurements.

```bash
python3 main.py run --workload sysbench_mysql --config 4c4t
```

| Argument | Default | Description |
|---|---|---|
| `test` | `oltp_read_write` | sysbench OLTP test: `oltp_read_write`, `oltp_read_only`, `oltp_write_only`, `oltp_point_select`, `oltp_insert`, `oltp_delete`, etc. |
| `tables` | `10` | Number of test tables |
| `table_size` | `100000` | Rows per table |
| `time` | `60` | Benchmark duration in seconds |
| `report_interval` | `10` | Per-interval progress report (seconds) |
| `mysql_port` | `13306` | Host port mapped to the container's 3306 |
| `mysql_image` | `mysql:8.0` | Docker image to pull |
| `mysql_cpus` | _(none)_ | Pin MySQL container to these cores: `"0-3"`, `"0,1,2,3"` |
| `mysql_mems` | _(none)_ | Pin MySQL container to these NUMA nodes: `"0"`, `"0,1"` |

```bash
# Read-only workload, larger dataset
python3 main.py run --workload sysbench_mysql --config 8c8t \
    --arg test=oltp_read_only tables=20 table_size=500000 time=120

# Pin MySQL server to cores 0-3, sysbench client to cores 4-7
python3 main.py run --workload sysbench_mysql --config 4c4t \
    --arg mysql_cpus=0-3 mysql_mems=0

# Write-heavy, short run
python3 main.py run --workload sysbench_mysql --config 4c4t \
    --arg test=oltp_write_only time=30
```

**Metrics:** `transactions_per_sec`, `queries_per_sec`, `errors_per_sec`, `total_time_sec`, `total_events`, `latency_min_ms`, `latency_avg_ms`, `latency_max_ms`, `latency_p95_ms`

> **CPU pinning:** `mysql_cpus` / `mysql_mems` pin the Docker container via `--cpuset-cpus` / `--cpuset-mems`. The sysbench client is separately pinned by the runner via `taskset`. Using disjoint sets (e.g. server on `0-3`, client on `4-7`) isolates server and client noise for cleaner results.

---

## Linux Kernel Subsystem Benchmarks

These workloads measure specific kernel subsystems directly. All follow the same `run` / `scaling` / `cloud-run` interface as other workloads.

**Install all dependencies at once:**

```bash
sudo apt install rt-tests schbench lmbench stress-ng iperf3 sockperf fs-mark
```

---

### `schbench` — Scheduler Wakeup Latency

**Dependencies:** `apt install schbench`  
**Subsystem:** CPU Scheduling

Measures how quickly the scheduler wakes a sleeping thread after being signalled. Two layers of threads: message-passers wake workers and record the resulting latency percentiles. A direct measure of scheduler responsiveness under contention.

```bash
python3 main.py run --workload schbench --config 4c4t
```

| Argument | Default | Description |
|---|---|---|
| `message_threads` | `2` | Number of message-passer threads (`-m`) |
| `worker_threads` | `16` | Worker threads per message-passer (`-t`) |
| `runtime` | `30` | Duration in seconds (`-r`) |

```bash
# High-contention: many workers, longer run
python3 main.py run --workload schbench --config full_socket \
    --arg message_threads=4 worker_threads=32 runtime=60
```

**Metrics:** `wakeup_p50_us`, `wakeup_p75_us`, `wakeup_p90_us`, `wakeup_p95_us`, `wakeup_p99_us`, `wakeup_p99_5_us`, `wakeup_p99_9_us`, `wakeup_min_us`, `wakeup_max_us`

---

### `hackbench` — Scheduler Communication Throughput

**Dependencies:** `apt install rt-tests`  
**Subsystem:** CPU Scheduling

Creates groups of tasks that pass messages to each other through sockets or pipes. Measures scheduling throughput: how fast the kernel can context-switch and deliver messages between many competing tasks. Lower `time_sec` = faster scheduler.

```bash
python3 main.py run --workload hackbench --config 4c4t
```

| Argument | Default | Description |
|---|---|---|
| `groups` | `10` | Number of task groups (`-g`) |
| `loops` | `1000` | Messages per sender per run (`-l`) |
| `data_size` | `100` | Message size in bytes (`-s`) |
| `use_threads` | `false` | Use threads instead of processes (`-T`) |
| `use_pipes` | `false` | Use pipes instead of sockets (`-p`) |

```bash
# Thread-based, with pipes
python3 main.py run --workload hackbench --config 8c8t \
    --arg groups=20 use_threads=true use_pipes=true

# Scaling study: how scheduling throughput changes with core count
python3 main.py scaling --workload hackbench --configs 1c1t,2c2t,4c4t,8c8t,16c16t
```

**Metrics:** `time_sec`

---

### `cyclictest` — Real-Time Timer Latency

**Dependencies:** `apt install rt-tests`  
**Subsystem:** CPU Scheduling / Real-Time

Measures the latency from when a POSIX timer fires to when the sleeping thread is actually scheduled. Captures interrupt handling overhead, scheduler jitter, and real-time responsiveness. Run as root for accurate RT-priority results.

```bash
# Run as root for proper RT-FIFO priority
sudo python3 main.py run --workload cyclictest --config 4c4t

# Non-root run (reduced accuracy, still useful for relative comparisons)
python3 main.py run --workload cyclictest --config 4c4t \
    --arg priority=0 mlockall=false
```

| Argument | Default | Description |
|---|---|---|
| `loops` | `100000` | Measurement loops per thread (`-l`) |
| `interval_us` | `1000` | Timer interval in microseconds (`-i`) |
| `priority` | `99` | RT SCHED_FIFO priority; 0 = no RT (`-p`) |
| `mlockall` | `true` | Lock all memory pages to prevent page-fault jitter (`-m`) |

```bash
# Short high-frequency run
python3 main.py run --workload cyclictest --config 8c8t \
    --arg loops=500000 interval_us=200
```

**Metrics:** `latency_min_us`, `latency_avg_us`, `latency_max_us` (worst-case across all threads)

---

### `mem_lat` — Memory Hierarchy Latency

**Dependencies:** `apt install lmbench`  
**Subsystem:** Memory Access, Address Translation

Sweeps working-set size from L1 cache through main DRAM using pointer chasing (`lat_mem_rd`). The latency inflection points reveal L1/L2/L3 cache capacities. TLB pressure becomes visible at sizes beyond the L3 cache where page-table walks dominate.

```bash
python3 main.py run --workload mem_lat --config 1c1t
```

| Argument | Default | Description |
|---|---|---|
| `max_size_mb` | `512` | Sweep upper bound in MB |
| `stride` | `128` | Stride between pointer loads in bytes (128 = 2 cache lines) |
| `trials` | `3` | Timing trials per size point (`-t`) |

```bash
# Page-stride run (4096 bytes) — maximises TLB pressure
python3 main.py run --workload mem_lat --config 1c1t --arg stride=4096

# Scaling study: DRAM latency under increasing thread count (NUMA pressure)
python3 main.py scaling --workload mem_lat --configs 1c1t,2c2t,4c4t,8c8t,16c16t
```

**Metrics:** `lat_l1_ns`, `lat_l2_ns`, `lat_l3_ns`, `lat_l3b_ns`, `lat_dram_ns`, `lat_min_ns`, `lat_max_ns`

> **TLB tip:** Run twice — once with `stride=128` (cache-line) and once with `stride=4096` (page). The latency difference at large sizes (e.g. 64 MB+) is the TLB miss overhead.

---

### `mem_alloc` — Memory Allocation Throughput

**Dependencies:** `apt install stress-ng`  
**Subsystem:** Memory Allocation (buddy allocator, slab, brk, mmap, page faults)

Measures allocation throughput in bogo-ops/sec using `stress-ng`. Three stressors exercise different kernel paths:

| Stressor | Kernel path |
|---|---|
| `malloc` | glibc → `brk` / `mmap` → page-fault handler |
| `mmap` | `mmap(2)` directly → anonymous mapping → page-fault handler |
| `brk` | `brk(2)` → heap expansion |

```bash
python3 main.py run --workload mem_alloc --config 4c4t
python3 main.py run --workload mem_alloc --config 4c4t --arg stressor=mmap
```

| Argument | Default | Description |
|---|---|---|
| `stressor` | `malloc` | `malloc`, `mmap`, or `brk` |
| `duration` | `60` | Duration in seconds |

```bash
# Compare all three allocation paths
for s in malloc mmap brk; do
    python3 main.py run --workload mem_alloc --config 8c8t --arg stressor=$s duration=30
done

# Scaling study for malloc throughput
python3 main.py scaling --workload mem_alloc --configs 1c1t,2c2t,4c4t,8c8t,16c16t
```

**Metrics:** `malloc_bogo_ops_per_sec` (or `mmap_` / `brk_` prefix depending on stressor)

---

### `iperf3` — Network Stack Throughput

**Dependencies:** `apt install iperf3`  
**Subsystem:** Networking (TCP/IP stack, socket layer, sk_buff, softirq)

Runs an iperf3 server and client on the loopback interface. Loopback bypasses the NIC driver but exercises the full kernel TCP/IP stack: socket layer, TCP congestion control, `sk_buff` allocation, and softirq handling. A setup/teardown server process is managed automatically.

```bash
python3 main.py run --workload iperf3 --config 4c4t
```

| Argument | Default | Description |
|---|---|---|
| `port` | `5201` | iperf3 port |
| `duration` | `30` | Test duration in seconds (`-t`) |
| `streams` | `1` | Parallel TCP streams (`-P`) |
| `udp` | `false` | Use UDP instead of TCP (`-u`) |
| `reverse` | `false` | Reverse direction: server→client (`-R`) |

```bash
# Multi-stream for higher throughput
python3 main.py run --workload iperf3 --config 4c4t --arg streams=4 duration=60

# UDP test
python3 main.py run --workload iperf3 --config 2c2t --arg udp=true

# Scaling study: how TCP throughput scales with thread count
python3 main.py scaling --workload iperf3 --configs 1c1t,2c2t,4c4t,8c8t \
    --arg streams=4
```

**Metrics:** `throughput_gbps`, `retransmits`

---

### `sockperf` — Network Socket Latency

**Dependencies:** `apt install sockperf`  
**Subsystem:** Networking (socket layer, send/recv path, scheduling)

Runs a sockperf ping-pong test over the loopback interface, measuring round-trip socket latency. Exercises the kernel socket layer, the TCP/UDP send and receive paths, and thread scheduling for network operations. A setup/teardown server process is managed automatically.

```bash
python3 main.py run --workload sockperf --config 1c1t
```

| Argument | Default | Description |
|---|---|---|
| `port` | `11111` | Server port |
| `duration` | `30` | Test duration in seconds |
| `msg_size` | `14` | Payload size in bytes (14 = minimum) |
| `udp` | `false` | Use UDP instead of TCP |

```bash
# Larger message size
python3 main.py run --workload sockperf --config 1c1t --arg msg_size=1024 duration=60

# UDP latency
python3 main.py run --workload sockperf --config 1c1t --arg udp=true
```

**Metrics:** `latency_avg_us`, `latency_p50_us`, `latency_p99_us`, `latency_p999_us`

---

### `fs_mark` — VFS Metadata Throughput

**Dependencies:** `apt install fs-mark`  
**Subsystem:** Virtual Filesystem (VFS, dentry cache, inode allocation)

Creates, syncs, and deletes large numbers of small files in repeated cycles, measuring files-per-second throughput. Stresses the VFS layer: dentry and inode cache, dcache locking, directory entry management, and the underlying filesystem's metadata path.

```bash
python3 main.py run --workload fs_mark --config 4c4t
```

| Argument | Default | Description |
|---|---|---|
| `num_files` | `4096` | Files created per iteration (`-n`) |
| `file_size` | `4096` | File size in bytes; `0` = metadata-only (`-s`) |
| `iterations` | `5` | Create/sync/delete cycles (`-L`) |
| `dir` | _(work_dir)_ | Working directory; defaults to a temp subdir |
| `subdirs` | `0` | Number of subdirectories; `0` = flat layout (`-D`) |
| `sync_writes` | `true` | `fsync` each file after write (`-S`) |

```bash
# Metadata-only (no file content written)
python3 main.py run --workload fs_mark --config 4c4t --arg file_size=0

# Large-file variant on a fast NVMe
python3 main.py run --workload fs_mark --config 8c8t \
    --arg num_files=1000 file_size=65536 dir=/mnt/nvme/fsmark

# Scaling study: VFS throughput vs thread count
python3 main.py scaling --workload fs_mark --configs 1c1t,2c2t,4c4t,8c8t,16c16t \
    --arg num_files=2048 file_size=0 iterations=10
```

**Metrics:** `files_per_sec`

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
  cloud_vms.json              ← tracked cloud VMs
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

## Kernel Analysis

The kernel analysis layer automates cross-kernel performance comparisons. It installs each kernel (from apt, a GitHub branch, or a tar.gz URL), configures GRUB for a one-time boot (`grub-reboot`), runs the full workload suite after each reboot, and pushes results to the Kernel Ledger. A systemd service handles auto-resume so the workflow survives reboots without manual intervention.

Scores are computed after all kernels complete using a geometric mean of normalised per-metric values. The baseline kernel (first in the list) scores 100; other kernels are shown relative to it.

---

### `kernel-analyze`

Run benchmarks across multiple kernel versions on the local machine. Requires `root` for `apt`, `grub-reboot`, and `systemctl reboot`.

```bash
sudo python3 main.py kernel-analyze \
  --kernels VER,VER,... --workloads NAME,NAME,... \
  [--config PRESET] [--iterations N] \
  [--push-url URL] [--api-key KEY] [--kernel-config LABEL] \
  [--system-name NAME] [--state-file PATH] [--dry-run]
```

| Flag | Default | Description |
|---|---|---|
| `--kernels SPEC,...` | (required) | Comma-separated kernel specs (see formats below) |
| `--workloads NAME,...` | (required) | Comma-separated workload names to run on each kernel |
| `--config PRESET` | `single_core` | Config preset for each workload run (see `list-configs`) |
| `--iterations N` | `3` | Iterations per workload per kernel |
| `--push-url URL` | — | Kernel Ledger base URL; results are pushed after each kernel |
| `--api-key KEY` | `$GARUDA_API_KEY` | Ledger push API key |
| `--kernel-config LABEL` | `analyzed` | Label stored in the ledger (e.g. `distro-ubuntu`, `defconfig`) |
| `--system-name NAME` | hostname | Override the system name in the ledger |
| `--state-file PATH` | `/var/lib/garuda/kernel_analysis.json` | State file path (survives reboots) |
| `--dry-run` | off | Print the plan without installing or rebooting |
| `--status` | — | Show current session state and exit |
| `--abort` | — | Disable the systemd service and remove the state file |
| `--resume` | — | Resume an in-progress session (called automatically by systemd) |
| `--list-kernels` | — | Show kernel packages available via apt and exit |

**Kernel spec formats**

Three install methods can be mixed freely in a single `--kernels` list:

| Format | Install method | Example |
|---|---|---|
| `VERSION` | apt (plain version string) | `6.8.0-55-generic` |
| `apt:VERSION` | apt (explicit) | `apt:6.12.0-10-generic` |
| `github:REPO:BRANCH[@LABEL]` | git clone + `make bindeb-pkg` | `github:torvalds/linux:master@v6.15-rc1` |
| `github:URL:BRANCH[@LABEL]` | git clone + `make bindeb-pkg` | `github:https://github.com/owner/linux:stable` |
| `tarball:URL[@LABEL]` | download + extract + `make bindeb-pkg` | `tarball:https://cdn.kernel.org/.../linux-6.13.tar.gz@6.13.0` |

For GitHub and tarball sources:
- The optional `@LABEL` suffix sets the display name and run ID label. Without it, a label is derived from the repo/filename.
- The running kernel's `.config` is copied into the source tree and `make olddefconfig` is run before the build.
- `make bindeb-pkg` produces `.deb` packages which are installed with `dpkg -i`. The build directory is cached at `/var/cache/garuda/kernel-builds/` — re-running after an interrupted build resumes from the existing clone/source.
- Build time is typically 30–90 minutes depending on machine speed. Plan accordingly.
- Required build dependencies: `build-essential bc bison flex libssl-dev libelf-dev`

**How it works:**

```
sudo python3 main.py kernel-analyze --kernels 6.8,6.11,github:torvalds/linux:master@v6.15 --workloads fio,stream
  │
  ├─ Creates state file at /var/lib/garuda/kernel_analysis.json
  ├─ Installs garuda-kernel-analysis.service (systemd oneshot)
  │
  ├─ [kernel 6.8.0-55-generic — already running]
  │   ├─ Benchmarks fio, stream
  │   ├─ Pushes results to Kernel Ledger
  │   └─ grub-reboot → reboot into 6.11
  │
  ├─ [boot] systemd fires kernel-analyze --resume
  │   ├─ Benchmarks fio, stream on 6.11
  │   ├─ Pushes results
  │   └─ git clone + build v6.15-rc1, grub-reboot → reboot into built kernel
  │
  └─ [boot] systemd fires kernel-analyze --resume
      ├─ Benchmarks fio, stream on v6.15-rc1
      ├─ Pushes results
      ├─ Prints composite score report
      └─ Removes systemd service
```

**Examples:**

```bash
# Three apt kernels, scheduler + memory workloads, 5 iterations, 4-core config
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,6.11.0-25-generic,6.12.0-10-generic \
  --workloads schbench,hackbench,stream,mem_lat \
  --config 4c4t --iterations 5 \
  --push-url http://localhost:8000 --api-key my-key \
  --kernel-config distro-ubuntu

# Compare an apt kernel against mainline from GitHub
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,github:torvalds/linux:master@v6.15-rc1 \
  --workloads schbench,hackbench,mem_lat \
  --config 4c4t --iterations 3

# Benchmark a custom kernel from a tar.gz tarball
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,tarball:https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.tar.gz@6.13.0 \
  --workloads fio,stream --config 4c4t --iterations 5

# Mix all three source types
sudo python3 main.py kernel-analyze \
  --kernels "6.8.0-55-generic,github:torvalds/linux:master@mainline,tarball:https://example.com/linux-custom.tar.gz@custom" \
  --workloads schbench,stream,fio --config 4c4t --iterations 3

# See what kernels are available to install via apt
python3 main.py kernel-analyze --list-kernels

# Dry run — shows which kernels need installation and how
sudo python3 main.py kernel-analyze \
  --kernels 6.8.0-55-generic,github:torvalds/linux:master@v6.15-rc1 --workloads fio --dry-run

# Check progress mid-analysis
sudo python3 main.py kernel-analyze --status
```

**Score report (printed after all kernels complete):**

```
════════════════════════════════════════════════════════════════════════
  KERNEL ANALYSIS — COMPOSITE SCORES  (baseline = 100)
════════════════════════════════════════════════════════════════════════
  Kernel                              Score   vs baseline    vs prev
  ────────────────────────────────────────────────────────────────────
  6.8.0-55-generic                    100.0      baseline          —  [baseline]
  6.11.0-25-generic                   103.4        +3.4%       +3.4%  ▲ better
  6.12.0-10-generic                    98.1        -1.9%       -5.1%  ▼ worse

  Per-workload scores (100 = baseline):
  ────────────────────────────────────────────────────────────────────
  fio                    6.8.0-55-generic: 100.0 | 6.11.0-25-generic: 107.2 | 6.12.0-10-generic: 99.3
  stream                 6.8.0-55-generic: 100.0 | 6.11.0-25-generic: 100.8 | 6.12.0-10-generic: 97.4
════════════════════════════════════════════════════════════════════════
```

---

### `cloud-kernel-analyze`

Same pipeline as `kernel-analyze` but on a cloud VM. The orchestrator pre-installs all kernels before the first reboot, then polls SSH every 30 s — reconnecting transparently when the VM is unreachable during reboots — until the analysis completes. Results are fetched locally at the end.

```bash
python3 main.py cloud-kernel-analyze \
  --provider PROVIDER --region REGION --instance-type TYPE \
  --kernels VER,VER,... --workloads NAME,NAME,... \
  [--config PRESET] [--iterations N] \
  [--push-url URL] [--api-key KEY] [--kernel-config LABEL] \
  [--poll-interval SECONDS] [--no-teardown] [--verbose]
```

Accepts all standard `--provider` / VM configuration flags (same as `cloud-run`). See [Common VM arguments](#common-vm-arguments).

| Flag | Default | Description |
|---|---|---|
| `--kernels SPEC,...` | (required) | Comma-separated kernel specs (apt version, `github:REPO:BRANCH[@label]`, or `tarball:URL[@label]`) |
| `--workloads NAME,...` | (required) | Comma-separated workload names |
| `--config PRESET` | `single_core` | Config preset for workload runs |
| `--iterations N` | `3` | Iterations per workload per kernel |
| `--push-url URL` | — | Kernel Ledger URL; the VM pushes directly after each kernel |
| `--api-key KEY` | `$GARUDA_API_KEY` | Ledger push API key |
| `--kernel-config LABEL` | `analyzed` | Label stored in the ledger |
| `--poll-interval SECONDS` | `30` | SSH poll frequency |
| `--no-teardown` | off | Keep the VM running after analysis (tracked by `cloud-list`) |
| `--verbose` | off | Print detailed remote command output |

**How it works:**

```
python3 main.py cloud-kernel-analyze --provider gcp ...
  │
  ├─ Provision VM (GCP/AWS/Azure)
  ├─ Deploy toolkit via rsync
  ├─ Pre-install all kernels via apt (avoids download stalls mid-reboot)
  ├─ Setup workload binaries
  │
  ├─ Launch: sudo python3 main.py kernel-analyze ... (on VM)
  │   └─ VM installs systemd service, sets grub-reboot, reboots
  │
  ├─ [orchestrator] poll SSH every 30 s
  │   ├─ VM unreachable (rebooting) → retry silently
  │   ├─ VM back up → read state file → print progress
  │   └─ Repeat until status = "done" or "failed"
  │
  ├─ Fetch results/ via rsync
  ├─ Fetch state file via SSH cat
  ├─ Print composite score report (from local data)
  └─ Destroy VM (or --no-teardown to keep it)
```

**Examples:**

```bash
# GCP — three kernels, full scheduler + memory suite, auto-push
python3 main.py cloud-kernel-analyze \
  --provider gcp --region us-central1 --instance-type n2-standard-8 \
  --kernels 6.8.0-55-generic,6.11.0-25-generic,6.12.0-10-generic \
  --workloads schbench,hackbench,stream,fio,mem_lat \
  --config 4c4t --iterations 5 \
  --push-url http://perf.example.com --api-key my-key \
  --kernel-config cloud-gcp-n2

# AWS — two kernels, keep VM for inspection afterwards
python3 main.py cloud-kernel-analyze \
  --provider aws --region us-east-1 \
  --instance-type m5.xlarge --aws-key-name my-keypair \
  --kernels 6.8.0-55-generic,6.12.0-10-generic \
  --workloads hackbench,schbench,stream \
  --config 4c4t --iterations 3 --no-teardown

# Azure — verbose output, fast poll
python3 main.py cloud-kernel-analyze \
  --provider azure --region eastus --instance-type Standard_D8s_v3 \
  --kernels 6.8.0-55-generic,6.11.0-25-generic \
  --workloads fio,stream --iterations 5 \
  --poll-interval 15 --verbose
```

---

## Garuda Kernel Ledger

The Garuda Kernel Ledger is a companion web portal for storing and comparing benchmark results across kernel versions, hardware configurations, and system setups. After running any workload locally or in the cloud, push results to the portal with the `push` subcommand.

### Pushing results

```bash
# Set the API key (generated when deploying the portal)
export GARUDA_API_KEY="your-api-key"

# Push the most recent run
python3 main.py push --url http://perf.example.com

# Push a specific run with an explicit kernel label
python3 main.py push --url http://perf.example.com \
  --run-id 20260510_093347_hackbench_single_core \
  --kernel 6.12.0 \
  --kernel-config defconfig
```

The push command auto-detects system and kernel information and captures a full snapshot of the kernel configuration at push time — CPU frequency governor, THP policy, scheduler knobs, NUMA balancing, security mitigations, and more. This snapshot is stored alongside the results and is visible in the portal's run detail view.

### Push flags

| Flag | Default | Description |
|---|---|---|
| `--url` | (required) | Base URL of the portal |
| `--run-id` | most recent run | Run directory to push |
| `--system-name` | hostname | Override the system name |
| `--kernel` | `uname -r` | Override the kernel version string |
| `--kernel-config` | `unknown` | Kernel config label (e.g. `defconfig`, `distro-ubuntu`) |
| `--api-key` | `$GARUDA_API_KEY` | Push authentication key |

### Run report

After each run, the terminal prints a full system report alongside the metric summary:

```
System
  Host     : lab-server-01
  Kernel   : 6.17.0-23-generic
  CPU      : AMD Ryzen 9 9950X 16-Core Processor
  Topology : 1 socket(s), 16 physical cores, 32 logical CPUs, 1 NUMA node(s)
  SMT      : enabled (2x per core)
  Memory   : 30.4 GB

CPU power & frequency
  pstate driver   : amd-pstate-epp
  governor        : powersave
  boost (turbo)   : enabled
  EPP             : balance_performance
  C-states (cpu0) : POLL(on,0us)  C1(on,1us)  C2(on,18us)  C3(on,350us)

Scheduler
  preempt model   : PREEMPT_DYNAMIC
  NUMA balancing  : off
  RT period (us)  : 1000000
  ...

Memory / VM       THP / hugepages / overcommit / swappiness / dirty ratios ...
CPU isolation     isolated CPUs / nohz_full / rcu_nocbs / IRQ affinity ...
I/O schedulers    per block device
Network           TCP congestion / buffer sizes / SACK ...
Kernel / boot     RCU / NMI watchdog / full cmdline ...
Security          active mitigations + not-affected summary
```

### Portal pages

| Page | Description |
|---|---|
| **Compare** | Line chart of a metric across kernel versions with min/max error bars and a Δ% table |
| **Regressions** | Heatmap of workload/metric × kernel-transition cells, colour-coded red/green |
| **Systems** | Bar chart comparing the same metric across different machines on one kernel |
| **Runs** | Filterable table of all ingested runs; click a row to open the detail view |
| **Run detail** | Full report: metrics table with per-iteration values, complete kernel snapshot |

See the [portal README](portal/README.md) for deployment instructions.

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

After saving the file, it appears immediately in `list-workloads` and works with cloud commands:

```bash
python3 main.py list-workloads
python3 main.py validate --workload my_bench
python3 main.py run --workload my_bench --config full_socket --iterations 3
python3 main.py scaling --workload my_bench --configs 1c1t,2c2t,4c4t,8c8t

# Run on a cloud VM
python3 main.py cloud-run \
  --provider gcp --region us-central1 --instance-type n2-standard-4 \
  --workload my_bench --config full_socket
```

### BaseWorkload interface

All methods in `BaseWorkload` (`benchmark_toolkit/base.py`):

| Method / property | Required | Description |
|---|---|---|
| `validate()` | Yes | Return `(bool, str)`. Check that required binaries exist. |
| `build_command(config)` | Yes | Return `argv` list. Do not include `taskset`/`numactl`; the runner prepends those. |
| `parse_output(stdout, stderr, returncode)` | Yes | Return `{metric: float}`. Empty dict marks the run as failed. |
| `install(install_dir, force=False)` | No | Download/build/install the binary to `install_dir`. May print progress. Return `(bool, str)`. Default: "not supported". |
| `install_hint` | No | Property: one-line description of the install method shown in `setup --list`. |
| `setup(config, work_dir)` | No | Runs once before the first iteration. Write helper scripts, etc. |
| `teardown(config, work_dir)` | No | Runs once after the last iteration. Clean up temp files. |
| `get_env(config)` | No | Return env var overrides merged on top of `os.environ`. Default sets `OMP_NUM_THREADS`. |
| `default_workload_args()` | No | Return default `{key: value}` dict. Merged with (and overridden by) `config.workload_args`. |

### Passing workload arguments

The `--arg` flag on `run`, `scaling`, and all `cloud-*` commands populates `config.workload_args`. Inside `build_command`, merge defaults first so CLI args always win:

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
