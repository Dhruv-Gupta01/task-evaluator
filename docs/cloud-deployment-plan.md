# Cloud deployment plan for Task Evaluator

Status: planning only. Nothing in this plan has been deployed or run in the cloud. Written 26 Oct 2026.
Every number below is marked as **verified** (checked against the provider's own page or API in this session), **measured** (computed from this machine's data), **derived** (arithmetic on verified figures) or **assumed**.
LLM spend is not in any infrastructure figure; it is larger (see section 7.6).

## 1. Summary of decisions
1. **Run on one VM that has Docker.** Serverless functions and container services cannot run Harbor or `docker build` (section 3).
2. **No Kubernetes.** At up to 3 concurrent users it adds cost and work for no benefit (section 4.2).
3. **Start/stop the VM on demand** on AWS, GCP or Azure: compute is billed only while the VM runs (section 7). Hetzner Cloud bills a powered-off server, so it is only an always-on option.
4. **Put the frontend, backend and Postgres on the same VM**; login at the proxy (Cloudflare Access is free below 50 users).
5. **Change very little code**: platform setting, CORS, frontend build target, plus an optional scheduler and an idle-stop check (section 5).
6. **Expected infrastructure cost**: about $30-110 a month on AWS start/stop for 1-3 concurrent users, against $140-300 for the same VM always on. Which end you land at depends on VM uptime, which is not measured yet (section 7).
7. **Sandbox services (E2B, Daytona, Modal) are phase 2**, not phase 1: they still need a small VM and would need real code changes (section 6).

## 2. What the platform is (evidence from the code)
- Backend: FastAPI in `backend/`, sync SQLAlchemy over Postgres 16, in-process asyncio task queue (`workers/task_queue.py`).
- Each stage (`POST /submissions/{id}/<stage>`) is a coroutine in `services/task_runner.py` that shells out to `harbor` or `docker` subprocesses.
- Harbor 0.23.0 (installed as a `uv` tool) builds and runs the task's containers through `docker compose` and bind-mounts its job folders into them, so the backend and the Docker daemon must share a filesystem.
- Direct Docker use by the backend: `docker build`, `docker run` (image inventory, CPU-stress sibling container), `docker exec` (memory peak, OOM flag, `id -u`, Codex session cost), `docker image rm`, `docker buildx imagetools`, and container reaping by label at startup.
- State: files under `backend/storage/submissions/<id>/` and absolute paths in the database; run status in memory plus Postgres. Startup marks every pending/running row failed and removes labelled containers, so a restart kills running work (found in a code survey of `main.py`, `task_runner.reset_stuck_rows`, `docker_orchestrator.reap_orphans`).
- No authentication and no user concept (no auth middleware, no owner column on `Submission`).
- Frontend: TanStack Start (React 19). Its build defaults to a Cloudflare Workers target; the API URL is fixed at build time through `VITE_API_BASE_URL`.
- Harbor environment types shipped in the installed package: docker, podman, daytona, e2b, modal, runloop, langsmith, ec2, gke, ack, openshift, novita, apple-container, singularity. The platform only uses docker today.
- GPU: the platform does not support it. Harbor's Docker environment declares only CPU and memory limits, and the platform only warns when `gpus` is set (`platform_checks.check_resources`).
- Resource limits: Harbor applies each task's `cpus` and `memory_mb` as container limits. A limit is a ceiling, not a reservation: Docker does not set CPUs aside, and a task above its memory limit is OOM-killed.

## 3. Where it can and cannot run
| Option | Verdict | Reason |
|---|---|---|
| AWS Lambda | No | 15-minute limit, no Docker daemon; trials run for hours |
| Fargate, Cloud Run, Azure Container Apps | No for the worker | No Docker daemon for Harbor or `docker build` |
| Runpod pods | Poor fit | A pod is already a container, with no usable Docker-in-Docker; Runpod is aimed at GPU model hosting |
| A VM with Docker (AWS EC2, GCP Compute Engine, Azure VM, Oracle, Vultr, DigitalOcean, Hetzner) | Yes | Same shape as the Mac it runs on today |

## 4. Design
### 4.1 Architecture (phase 1)
```
Browser
  | HTTPS
Gateway (tiny always-on service; starts the VM)  ->  cloud API: StartInstances
  |
Reverse proxy on the VM (Caddy or nginx, TLS, login: Cloudflare Access or basic auth)
  |-- /      -> Frontend (Node server, built with NITRO_PRESET=node-server)
  `-- /api   -> Backend (FastAPI, uvicorn, systemd)
                  |-- Postgres (same VM, same disk)
                  |-- Disk volume (OS, Docker images, uploads, run folders)
                  `-- Docker daemon + Harbor
                        |-- task container (agent environment)
                        |-- verifier container (separate image)
                        `-- build, probe and stress containers
Outbound only: LLM APIs, GPTZero, Docker Hub, package mirrors
Idle check on the VM: stop the VM after no running stage and no activity for ~15 min
```
Where containers run: all on the VM's single Docker daemon as sibling containers started by Harbor. Task containers need public internet (`HARBOR_NETWORK_MODE=public`) because Codex and Claude Code install themselves and call their APIs from inside; Terminus-2 runs on the host and keeps keys out of the container.

### 4.2 Why this design (alternatives considered)
- **Kubernetes:** Harbor and the backend expect a local Docker daemon. Harbor ships `gke` and `ec2` environments, so Kubernetes is possible in principle, but the platform's own Docker calls (build, inventory, probes, reaping) would still need rework, and at a peak of 3 concurrent tasks it only adds nodes and operations work. Revisit past about 10 concurrent runs or when a second machine is needed.
- **Sandbox services (E2B, Daytona, Modal) from day one:** cheaper in raw compute at low use, but a control machine is still needed, and the code changes in section 6 apply. Deferred to phase 2.
- **Always-on VM:** simplest, but about $140-300 a month for compute alone; the measured usage is low enough that start/stop saves most of it.
- **Hetzner:** cheapest for always on (section 7.3), but a powered-off Hetzner Cloud server is still billed (Hetzner billing FAQ), so it cannot be start/stop.

### 4.3 Security (the part that is easy to underrate)
Task code is untrusted (arbitrary Dockerfiles and solutions) and runs on the same machine as the API keys and the database, and the platform has no login of its own. Minimum controls: a dedicated VM and AWS account/project, no cloud IAM role on the instance, firewall open only on 443, login at the proxy, only the API keys that are needed, a monthly LLM budget (`LLM_BUDGET_USD`) and per-run caps kept on, snapshots of the disk.

## 5. Code and configuration changes
| Change | Needed? | Notes |
|---|---|---|
| `DOCKER_PLATFORM=linux/amd64` | Required | The default is `linux/arm64` (Apple Silicon). Risk: results measured on arm64 here may differ on amd64 for architecture-sensitive tasks; re-check a few known tasks after the move |
| Add `PUT` to CORS methods (`backend/app/main.py`) | Required | The UI's AI Detection ruling (`ai-detection-decision`) uses PUT; cross-origin it fails without it. Not needed if proxy serves frontend and API on one origin, but cheap to fix |
| Frontend build for Node instead of Cloudflare | Required | **Verified locally**: `NITRO_PRESET=node-server VITE_API_BASE_URL=<url> bun run build` succeeds, produces `.output/server/index.mjs`, and `PORT=3999 node .output/server/index.mjs` served the home page with HTTP 200 |
| `VITE_API_BASE_URL` at build time | Required | Baked into the bundle; use the real URL, or a same-origin path behind the proxy |
| Proxy login (outside the code) | Required | Cloudflare Access is free below 50 users (Cloudflare pricing page); login inside the app would mean users and ownership in the code, which is days of work |
| Resource scheduler (section 5.1) | Recommended | Needed once 2-3 people run jobs at the same time |
| Idle-stop check | Recommended for start/stop | Never stop while a stage runs (a stop kills running work) |
| Disk retention job | Recommended | Submissions average 80 MB; Docker images add more |
| Postgres backups | Recommended | Dump plus disk snapshots |
| Linux/Windows install fixes | Already pushed | `.gitattributes`, Harbor stop handling; not yet run on a real Linux VM |

### 5.1 Resource scheduler design
1. Before any Docker or Harbor stage, read the task's `cpus`, `memory_mb` and `gpus` from the cached `TaskConfig`.
2. Keep a ledger of reserved CPU and memory against the host capacity, leaving about 20% free for the backend, Postgres and Docker; reserve 10-20% extra memory per task.
3. If the job fits, start it; otherwise mark the stage `queued` ("waiting for resources" in the UI) and start it when another job releases its share.
4. Release on finish, failure or cancel (reuse the cancel path in `workers/task_queue.py` and `harbor_runner._stop`).
5. Reject or flag any task with `gpus > 0` before it starts (no GPU support).
6. Limits: one heavy stage per user at a time, a global cap on concurrent builds, the stress Oracle reserves the whole VM.
7. Add `queued` to `schemas.py`, `src/lib/api.ts` and the submission page; clear leftover `queued` rows at startup.
Effort: about 2-4 days. Risk: two Harbor jobs at once timed out on the Mac (`EnvironmentStartTimeoutError`); test 3 at once on Linux and lower the limit if it recurs.

## 6. Why on-demand sandboxes need code changes
They run each trial on a remote machine, and these parts of the platform assume a local daemon:
Harbor's Docker mode and its bind-mounted job folders; `docker build`, Fresh Build, Image Contents, the CPU-stress Oracle and the base-image checks (local Docker CLI); the container probes (`docker exec` for memory, OOM, user id, Codex cost); startup reaping and stuck-run reset; the in-memory queue (a scale-to-zero backend would lose a running stage); absolute paths in the database. Without these changes a sandbox could run only Oracle, Nop and agent runs, and Build and the probes would still need a Docker host. Estimate: 2-4+ weeks.
A start/stop VM needs none of these changes.

## 7. Costing
### 7.1 Workload evidence
**Method.** Durations come from `started_at`/`finished_at` in Postgres (`runs` table) and from Harbor `result.json` files under `backend/storage/submissions`. Postgres has 147 agent trial rows over 38 submissions from 10 Aug to 19 Oct 2026; the stored run folders keep only the latest runs (53 trials), so Postgres is the larger sample. 19 rows have no reward and are all "interrupted by backend restart"; their end time is the restart time, so their 110 hours are not real container time and are excluded.
- **Genuine agent trials (measured):** 128 trials, 60.3 container-hours: mean 28 min, median about 15 min.
- **By agent (measured, from the trial log header):** Codex 73 trials, mean 22 min (median 17); Claude Code 21 trials, mean 70 min (median 39); Terminus-2 8 trials, mean 56 min (median 44).
- **Per submission with genuine trials (measured, 33 submissions):** mean 3.9 trials, 1.83 h of trial time, median 1.28 h, 90th percentile 3.74 h, maximum 10.0 h.
- **Pre-agent checks (measured), about 0.5 h:** the core stages on pg-wal-recovery (3), run one after another, took 20.8 min (12:50:55 to 13:11:41): Build 18 s, no-Docker checks 4 s, Sufficiency x3 and Edge cases about 3.5 min, Oracle x3 2.3 min, Nop 33 s, Rubric Check 5.3 min, Valid-variant probe 3.3 min, Weak-answer probe 3.3 min, Edge-case probe 2.1 min. Measured means per run: Oracle 1.5 min, Nop 1.6 min, Fresh Build 1.1 min, stress Oracle 1.3 min. With Fresh Build, 3 stress runs and AI Detection (about 6 min) the total is about 0.45 h, rounded up to 0.5 h.
- **Hours per full submission (derived):** 0.5 h + 1.83 h = **2.3 h on average** (median about 1.8 h, 90th percentile about 4.2 h, maximum about 10.5 h). The Cheat Trial (mean 13.4 min per run) is not included.
- **Agent mix matters (derived):** four Codex trials is about 1.5 h (2.0 h with checks); four Claude Code trials is about 4.7 h (5.2 h with checks).
**Limits.** One user, one machine, trials capped by cost limits, 38 submissions. A few heavy submissions could dominate a month. VM wall-clock uptime was not measured (task-hours were), so the 60/120/200 hours below are scenarios, not forecasts.

### 7.2 Sizing
Each task is about 2 vCPU / 4 GB (the pg-wal size, from `task.toml`). Sizes by concurrency (the VM must hold the tasks plus the backend, Postgres and builds):
| Concurrent tasks | VM | Fit |
|---|---|---|
| 1 | 4 vCPU / 16 GB | fine |
| 2 | 8 vCPU / 32 GB | fine |
| 3 | 8 vCPU / 32 GB | tight when builds run; 16 vCPU / 32 GB is comfortable |

### 7.3 Provider price comparison, always on (730 h/month, Linux, on demand)
| Provider and size | Hourly | Monthly | Status |
|---|---|---|---|
| **8 vCPU / 32 GB** | | | |
| AWS m7i.2xlarge (us-east-1) | $0.4032 | $294 | verified (AWS price list file) |
| GCP n2-standard-8 | $0.388472 | $284 | verified ([GCP page](https://cloud.google.com/products/compute/pricing/general-purpose); region not named) |
| Azure D8s v5 (East US) | $0.384 | $280 | verified (Azure retail prices API) |
| Oracle OCI E4, 8 OCPU + 32 GB (16 threads) | $0.248 | $181 | verified rates ($0.025 per OCPU-h, $0.0015 per GB-h); E5 not checked |
| Vultr dedicated general purpose | n/a | $240 (high-frequency $192, shared $160) | verified (Vultr plans API) |
| DigitalOcean general purpose | $0.375 | $252 (about $258 on the newer per-resource rate) | [DigitalOcean docs](https://docs.digitalocean.com/products/droplets/details/pricing/), ambiguous |
| Hetzner Cloud CCX33 | EUR 0.2219 | EUR 138.49 (EU), $165.99 (US) | verified ([Hetzner price notice](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/), new orders from 15 Jun 2026); other Hetzner pages show different figures |
| Hetzner dedicated AX42 | n/a | EUR 97.30 + EUR 49 setup (limited variant EUR 77.30 + EUR 39) | Hetzner price notice; the AX42 product page shows EUR 99; setup fees changed several times in 2026 |
| **4 vCPU / 16 GB** | | | |
| AWS m7i.xlarge | $0.2016 | $147 | verified |
| GCP n2-standard-4 | $0.194236 | $142 | derived (half of n2-standard-8) |
| Azure D4s v5 | $0.192 | $140 | derived (half of D8s v5) |
| Oracle OCI E4, 4 OCPU + 16 GB | $0.124 | $91 | derived from verified rates |
| Vultr high-frequency 4c/16 GB (dedicated general 4c/16 GB) | n/a | $96 ($120) | verified |
| DigitalOcean general purpose | n/a | about $126 | derived |
| Hetzner Cloud CCX23 | n/a | EUR 85.99 | third-party source only, unverified |
Also verified (AWS price list): m7i.4xlarge $0.8064/h, c7i.4xlarge $0.714/h (16 vCPU / 32 GB), t4g.small $0.0168/h (about $12.3/month, a possible always-on gateway). Spot examples: AWS m7i.2xlarge about $0.14/h and GCP n2-standard-8 about $0.233/h in Iowa; spot is interruptible and would kill a running trial, so it is not used here.
Which providers can stop billing compute: AWS ([stop/start docs](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html): no instance-hour charge while stopped, EBS still billed), GCP and Azure (stopped or deallocated VMs; Azure must be deallocated; GCP and Azure rules not re-verified this session). **Hetzner Cloud does not**: a powered-off server is billed until it is deleted ([Hetzner billing FAQ](https://docs.hetzner.com/cloud/billing/faq/)).

### 7.4 Other infrastructure line items
| Item | Cost | Status |
|---|---|---|
| Disk, 100-150 GB (AWS EBS gp3 $0.08 per GB-month) | $8-12 | verified rate; size is an estimate (Docker image size could not be measured, Docker was down; run folders measured 5.1 GB for 54 submissions) |
| Disk on GCP (Persistent Disk Balanced about $0.10 per GB-month) | $10-15 | rate on [Google's disk page](https://cloud.google.com/compute/disks-image-pricing), which looks old |
| Disk on Azure | assumed like GCP | unverified |
| Public IPv4 on AWS: $0.005 per IP-hour, billed for every public IPv4 including an instance's auto-assigned one ([AWS announcement](https://aws.amazon.com/blogs/aws/new-aws-public-ipv4-address-charge-public-ip-insights/)) | Elastic IP kept all month $3.65; auto-assigned only while running about $0.30-1.00 (but the address changes every start) | verified |
| Snapshots, $0.05 per GB-month on the standard tier, incremental | about $5-8 for 100-150 GB (less in practice) | verified rate ([AWS EBS pricing](https://aws.amazon.com/ebs/pricing/)) |
| Data transfer out to the internet | $0: 100 GB a month free, aggregated across AWS | verified ([AWS EC2 pricing](https://aws.amazon.com/ec2/pricing/on-demand/)) |
| Login at the proxy (Cloudflare Access) | $0 below 50 users | verified ([Cloudflare pricing](https://www.cloudflare.com/sase/products/access/)); paid is $7 per user per month beyond |
| Domain and DNS | about $1-2 | assumed |
| Gateway (starts the VM) | $0 (serverless) or about $12.3 (always-on t4g.small) | $12.3 derived; the free option is unverified |
Fixed extras used in the totals below: AWS $17.65-25.65 a month (disk $8-12, Elastic IP $3.65, snapshots $5-8, domain $1-2); GCP and Azure $19.65-28.65 (assumed equivalents).

### 7.5 Start/stop VM, monthly, by concurrency (no gateway cost; add about $12.3 for an always-on gateway)
VM column is the hourly rate times hours on, plus the extras range above.
| Concurrent tasks | Provider | VM always on (VM only) | VM on 60 h | VM on 120 h | VM on 200 h |
|---|---|---|---|---|---|
| 1 | AWS | $147 | $30-38 | $42-50 | $58-66 |
| 1 | GCP | $142 | $31-40 | $43-52 | $58-67 |
| 1 | Azure | $140 | $31-40 | $43-52 | $58-67 |
| 2 | AWS | $294 | $42-50 | $66-74 | $98-106 |
| 2 | GCP | $284 | $43-52 | $66-75 | $97-106 |
| 2 | Azure | $280 | $43-52 | $66-75 | $96-105 |
| 3 | AWS | $294 | $42-50 | $66-74 | $98-106 |
| 3 | GCP | $284 | $43-52 | $66-75 | $97-106 |
| 3 | Azure | $280 | $43-52 | $66-75 | $96-105 |
| 3 (comfortable, 16 vCPU) | AWS c7i.4xlarge | $521 | $60-68 | $103-111 | $160-168 |

### 7.6 What this costs per submission
AWS only, midpoint of the range, three workloads: pre-agent checks only (0.5 h), four Codex trials (2.3 h), four Claude Code trials (5.2 h). Cells read "submissions per month at a 100% busy ceiling / $ each at 100% busy / $ each at 50% busy". The VM idles between users and waits about 15 minutes before it stops, so 50% busy is the more realistic column.
| Concurrent | VM on (h) | Month total (mid) | Pre-agent only, 0.5 h: subs / $ each / $ each at 50% busy | Codex x4, 2.3 h | Claude Code x4, 5.2 h |
|---|---|---|---|---|---|
| 1 | 60 | $34 | 120 / $0.28 / $0.56 | 26 / $1.29 / $2.59 | 12 / $2.92 / $5.85 |
| 1 | 120 | $46 | 240 / $0.19 / $0.38 | 52 / $0.88 / $1.76 | 23 / $1.99 / $3.97 |
| 1 | 200 | $62 | 400 / $0.15 / $0.31 | 87 / $0.71 / $1.43 | 38 / $1.61 / $3.22 |
| 2 | 60 | $46 | 240 / $0.19 / $0.38 | 52 / $0.88 / $1.76 | 23 / $1.99 / $3.97 |
| 2 | 120 | $70 | 480 / $0.15 / $0.29 | 104 / $0.67 / $1.34 | 46 / $1.52 / $3.03 |
| 2 | 200 | $102 | 800 / $0.13 / $0.26 | 174 / $0.59 / $1.18 | 77 / $1.33 / $2.66 |
| 3 | 60 | $46 | 360 / $0.13 / $0.25 | 78 / $0.59 / $1.17 | 35 / $1.32 / $2.65 |
| 3 | 120 | $70 | 720 / $0.10 / $0.19 | 157 / $0.45 / $0.89 | 69 / $1.01 / $2.02 |
| 3 | 200 | $102 | 1200 / $0.09 / $0.17 | 261 / $0.39 / $0.78 | 115 / $0.89 / $1.77 |
LLM spend dominates everything above: recorded agent-trial spend is $290 across 117 spend rows (mean $2.48, maximum $53.65 per row; the agent column is empty for older rows) and this month's total recorded spend is about $230.

### 7.7 Sandbox alternative (phase 2), monthly
Rates: Daytona $0.0504 per vCPU-hour and $0.0162 per GiB-hour (same as E2B per its docs), so a 2 vCPU / 4 GB task is $0.1656 an hour (verified from [Daytona](https://www.daytona.io/pricing) and [E2B](https://docs.e2b.dev/faq/calculate-sandbox-price) docs). Modal sandbox rates are $0.00003942 per core-second and $0.00000667 per GiB-second (a core is 2 vCPU), about $0.238 an hour for the same task; Modal lists sandboxes at 3x list price for non-preemptible. Plans: E2B Pro is $150 a month plus usage; Modal Team is $250 a month plus compute (Starter $0 with $30 free credit); Daytona offers $200 of free compute. Session-length limits (an 8-hour agent timeout) must be checked per provider; I did not verify them.
A small always-on control machine is still needed for the frontend, backend and Postgres: about $20-23 a month (t4g.small $12.3, small disk, address, snapshots, domain; derived).
Monthly total = control machine + submissions x hours per submission x hourly rate. Cells read "Daytona or E2B rate / Modal rate":
| Submissions per month | Pre-agent only (0.5 h each): Daytona-or-E2B rate / Modal rate | Codex x4 (2.3 h) | Claude Code x4 (5.2 h) |
|---|---|---|---|
| 10 | $21-24 / $21-24 | $24-27 / $25-28 | $29-32 / $32-35 |
| 30 | $22-25 / $24-27 | $31-34 / $36-39 | $46-49 / $57-60 |
| 100 | $28-31 / $32-35 | $58-61 / $75-78 | $106-109 / $144-147 |
| 300 | $45-48 / $56-59 | $134-137 / $184-187 | $278-281 / $391-394 |
Not included: any E2B Pro or Modal Team plan fee.

### 7.8 Comparing the options for 1-3 concurrent users (monthly, AWS rates)
| Approach | Concurrency 1 | Concurrency 2 | Concurrency 3 |
|---|---|---|---|
| Always-on VM | $147 + extras = $165-173 | $294 + extras = $312-320 | $294 + extras = $312-320 (16 vCPU: $521 + extras) |
| Start/stop VM, 60-200 h on | $30-66 | $42-106 | $42-106 |
| Hetzner AX42 dedicated, always on | EUR 97 + setup (fits 1-3) | same | same |
| Sandboxes + control machine, 30 submissions of 2.3 h | $31-34 | $31-34 | $31-34 |
Hetzner AX42 has no start/stop: it is rented by the month.

## 8. Recommendation
1. Phase 1: AWS (or GCP/Azure; they are within a few dollars of each other for this design) start/stop VM, 8 vCPU / 32 GB, with the proxy login, a gateway to start it, the idle-stop check and the scheduler. GCP is natural if you want to reuse the existing GCP service account.
2. Estimated cost: about $40-110 a month plus LLM spend at 2-3 concurrent users and 60-200 hours of uptime; about $30-66 for one user.
3. Phase 2 only after real usage is measured: sandbox execution for the Oracle, Nop and agent stages.
4. If always-on is preferred for simplicity: a Hetzner dedicated server (about EUR 97 a month plus setup) is the cheapest, with no start/stop.

## 9. Risks and open items
- **Never run in the cloud.** Linux install, concurrent Harbor jobs, the Node frontend behind a proxy, and Docker image disk size are untested on a real VM. A one-day AWS trial was considered and deferred; it would turn most of the assumptions into measurements for a few dollars.
- **VM uptime is a guess** (60/120/200 h). Task-hours are measured; wall-clock hours the VM must stay on are not.
- **Small sample.** One user, 38 submissions, capped trials; an 8-hour trial or a heavy month changes the picture.
- **arm64 to amd64.** Results measured here were on arm64.
- **Unverified items:** GCP and Azure stop-billing rules and disk/IP prices, the free gateway, domain cost, Hetzner CCX23, Linode, OCI E5, sandbox session-length limits, how E2B/Modal plan fees apply to this use.
- **Restarts kill running work.** Use a drain step before every deploy and stop.
- **Security** (section 4.3).
- **GPU tasks** cannot run; the scheduler rejects them.

## 10. Rollout steps and effort
1. Frontend: Node build, `VITE_API_BASE_URL`, CORS `PUT` (about half a day; the build is verified locally).
2. Provision the VM (Docker, Harbor via `uv tool install harbor==0.23.0`, Postgres, backend and frontend under systemd, proxy and TLS, login, firewall, backups): 1-2 days.
3. Gateway page and idle-stop check: 1 day.
4. Scheduler and `queued` status: 2-4 days.
5. Smoke test: Build, Oracle, Nop, one agent trial, then 3 at once, watching for startup timeouts and disk growth.
6. Measure VM uptime and disk for a month, then revisit sandboxes.
Phase 1 total: about 4-8 days. Sandbox phase: 2-4+ weeks.
