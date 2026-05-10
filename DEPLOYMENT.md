# Deployment Runbook — Lead Generation Engine

Complete steps to deploy from scratch on Byteplus Cloud.

---

## Prerequisites (local machine)

- Docker Desktop installed and running
- Access to Byteplus Console
- Project cloned locally

---

## Part 1 — Byteplus Infrastructure

### 1.1 Create VPC

Console → **VPC → Create VPC**

| Setting | Value |
|---|---|
| Region | ap-southeast-3 (Jakarta) |
| CIDR | `192.168.0.0/16` |

Create a **subnet** inside it: `192.168.0.0/24`, same availability zone.

---

### 1.2 Create Security Groups

**ECS Security Group** (`lead-engine-app-sg`):

| Direction | Protocol | Port | Source |
|---|---|---|---|
| Inbound | TCP | 22 | Your IP |
| Inbound | TCP | 8080 | Your IP |
| Outbound | All | All | `0.0.0.0/0` |

**RDS Security Group** (`lead-engine-db-sg`):

| Direction | Protocol | Port | Source |
|---|---|---|---|
| Inbound | TCP | 5432 | `192.168.0.0/24` (VPC subnet) |
| Outbound | All | All | `0.0.0.0/0` |

---

### 1.3 Create Internet Gateway

Console → **VPC → Internet Gateways → Create** → attach to your VPC.

---

### 1.4 Create Route Table for ECS (public subnet)

Console → **VPC → Route Tables → Create**

Add routes:

| Destination | Next Hop |
|---|---|
| `192.168.0.0/24` | Local |
| `0.0.0.0/0` | Internet Gateway |

Associate this route table with the ECS subnet.

> **Important:** A NAT Gateway route only handles outbound traffic. An Internet Gateway is required for inbound access (browser → ECS). Keep the NAT Gateway route table for other private resources — ECS needs its own route table with an Internet Gateway.

---

### 1.5 Create ECS Instance

Console → **ECS → Create Instance**

| Setting | Value |
|---|---|
| Image | Ubuntu 22.04 LTS |
| Spec | 4 vCPU / 8 GB RAM |
| Storage | 50 GB SSD |
| VPC | your VPC |
| Subnet | your subnet |
| Security Group | `lead-engine-app-sg` |
| Key Pair | create and download `.pem` |

After creation → **Elastic IPs → Allocate → Bind to ECS instance**.

---

### 1.6 Create RDS Instance

Console → **RDS → Create Instance**

| Setting | Value |
|---|---|
| Engine | PostgreSQL 16 |
| Spec | 2 vCPU / 4 GB RAM |
| Storage | 50 GB SSD |
| VPC | same VPC as ECS |
| Subnet | same subnet |
| Security Group | `lead-engine-db-sg` |
| Public Access | **OFF** |
| DB Name | `lead-pipeline` |
| Username | `mittul` (or your choice) |
| Password | strong password |

After creation → **Connection Info → copy Private Endpoint**.

> **Note:** RDS hostname may not resolve inside Docker containers. Use the private IP address in `DATABASE_URL` instead of the hostname. Find the private IP by running `nslookup <rds-endpoint>` from the ECS terminal.

---

### 1.7 Create Container Registry

Console → **Container Registry → Create Instance**

| Setting | Value |
|---|---|
| Instance name | `cr-mittul` |
| Region | ap-southeast-3 |

Then: **Create Namespace** (e.g. `mittul`) → **Create Repository** (e.g. `lead-engine`).

Create access credentials: **CR → Access Credentials → Create** → save username and password.

Registry URL format: `[instance]-[region].cr.bytepluses.com`
Example: `cr-mittul-ap-southeast-3.cr.bytepluses.com`

---

## Part 2 — Build and Push Docker Image

### 2.1 Build image (run locally)

```bash
docker build --platform linux/amd64 -t lead-engine:latest .
```

> `--platform linux/amd64` is required on Mac M1/M2 — ECS runs on x86.

---

### 2.2 Login to Byteplus CR

```bash
docker login --username=YOUR_USERNAME@YOUR_USER_ID cr-mittul-ap-southeast-3.cr.bytepluses.com
```

Username format: `username@user-id` (shown in CR console under Access Credentials).

---

### 2.3 Tag the image

```bash
docker tag lead-engine:latest cr-mittul-ap-southeast-3.cr.bytepluses.com/mittul/lead-engine:latest
```

---

### 2.4 Push to CR

```bash
docker push cr-mittul-ap-southeast-3.cr.bytepluses.com/mittul/lead-engine:latest
```

Verify in Console → **CR → your repository → Tags**.

---

## Part 3 — Configure and Run on ECS

### 3.1 Open ECS browser terminal

Console → **ECS → your instance → Remote Connection**

This opens a browser-based terminal. No SSH needed.

---

### 3.2 Install Docker on ECS

```bash
apt-get update -qq
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update -qq
apt-get install -y docker-ce docker-ce-cli containerd.io
systemctl enable --now docker
docker --version
```

---

### 3.3 Get RDS private IP

```bash
nslookup YOUR_RDS_ENDPOINT
# note the IP address returned e.g. 192.168.1.109
```

Use this IP in `DATABASE_URL` — the hostname may not resolve inside Docker containers.

---

### 3.4 Login to CR from ECS

```bash
docker login --username=YOUR_USERNAME@YOUR_USER_ID cr-mittul-ap-southeast-3.cr.bytepluses.com
```

---

### 3.5 Run the container

Run as a single line (multiline backslash commands may fail in browser terminal):

```bash
docker run -d --name lead-engine --restart unless-stopped -p 8080:8080 -e DATABASE_URL="postgresql://DB_USER:DB_PASSWORD@192.168.1.109:5432/lead-pipeline" -e LLM_MODEL="seed-2-0-pro-260328" -e LLM_API_KEY="YOUR_ARK_API_KEY" -e LLM_PROVIDER="openai" -e LLM_API_BASE="https://ark.ap-southeast.bytepluses.com/api/v3" -e LLM_TIMEOUT_SECONDS="30" -e OUTPUT_DIR="./output" cr-mittul-ap-southeast-3.cr.bytepluses.com/mittul/lead-engine:latest
```

---

### 3.6 Verify the container is running

```bash
docker logs lead-engine --tail=5
```

Expected output:
```
INFO | PostgreSQL connected: 192.168.1.109:5432/lead-pipeline
INFO | Application startup complete.
INFO | Uvicorn running on http://0.0.0.0:8080
```

---

## Part 4 — Access the App

Open in browser:
```
http://YOUR_ECS_ELASTIC_IP:8080
```

---

## Part 5 — Subsequent Deploys (ECS + RDS)

This is the standard deploy flow once infrastructure is already running.

### Step 1 — Push new image from your local machine

```bash
make deploy
# This runs deploy.sh: builds linux/amd64 image, tags :latest + :YYYYMMDD-HHMMSS, pushes both to CR
```

Verify the new tag appeared: **CR Console → your repository → Tags**.

---

### Step 2 — SSH or open browser terminal on ECS

Console → **ECS → your instance → Remote Connection**

---

### Step 3 — Pull new image and restart container

Run as **one single line** in the ECS terminal (browser terminal breaks on multiline backslash commands):

```bash
docker stop lead-engine && docker rm lead-engine
```

```bash
docker pull cr-mittul-ap-southeast-3.cr.bytepluses.com/mittul/lead-engine:latest
```

```bash
docker run -d --name lead-engine --restart unless-stopped -p 80:8080 -e DATABASE_URL="postgresql://DB_USER:DB_PASSWORD@RDS_PRIVATE_IP:5432/lead-pipeline" -e LLM_MODEL="seed-2-0-pro-260328" -e LLM_API_KEY="YOUR_ARK_API_KEY" -e LLM_PROVIDER="openai" -e LLM_API_BASE="https://ark.ap-southeast.bytepluses.com/api/v3" -e LLM_TIMEOUT_SECONDS="30" -e OUTPUT_DIR="./output" -e APP_USERNAME="admin" -e APP_PASSWORD="YOUR_APP_PASSWORD" -e SESSION_SECRET="YOUR_SESSION_SECRET" cr-mittul-ap-southeast-3.cr.bytepluses.com/mittul/lead-engine:latest
```

> Replace `DB_USER`, `DB_PASSWORD`, `RDS_PRIVATE_IP`, `YOUR_ARK_API_KEY`, `YOUR_APP_PASSWORD`, `YOUR_SESSION_SECRET` with your actual values.
> Keep all secrets out of git — never commit them to this file.

---

### Step 4 — Verify startup

```bash
docker logs lead-engine --tail=20
```

Expected healthy output:
```
INFO | Loaded campaign 'kling' (Kling AI) — stage1 prompt: ...
INFO | Loaded campaign 'seedance' (Seedance) — stage1 prompt: ...
INFO | Campaigns loaded: kling, seedance
INFO | skill_files: seeded N files from disk (INSERT … DO NOTHING)
INFO | PostgreSQL connected: RDS_PRIVATE_IP:5432/lead-pipeline
INFO | Application startup complete.
INFO | Uvicorn running on http://0.0.0.0:8080
```

If you see `DB connection failed` — the container cannot reach RDS. Check:
- `RDS_PRIVATE_IP` is correct (use IP not hostname — see Step 3.3)
- RDS security group allows TCP 5432 from the ECS subnet CIDR
- The error message will tell you the exact Python exception

---

### Step 5 — Verify in browser

```
http://YOUR_ECS_ELASTIC_IP
```

Run history and skill files in the DB are untouched by a redeploy — only the application code changes.

---

### Rollback

Each deploy also pushes a timestamped tag (e.g. `:20260510-142301`). To roll back:

```bash
docker stop lead-engine && docker rm lead-engine
docker pull cr-mittul-ap-southeast-3.cr.bytepluses.com/mittul/lead-engine:20260510-142301
# then docker run ... with the timestamped tag instead of :latest
```

---

## Local Docker dev (no ECS)

```bash
make dev        # docker compose up --build  (app + local postgres on pg_data volume)
make dev-stop   # docker compose down        (stops containers, keeps pg_data volume intact)
```

> `docker compose down` preserves the `pg_data` named volume. Only `docker compose down -v` or Docker Desktop "Clean data" destroys it. Your local run history survives restarts.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `docker push` → unexpected EOF | Wrong registry domain | Use `cr.bytepluses.com` not `cr.byteplus.com` |
| Port 80 timeout | Security group missing inbound rule | Add TCP 80 inbound from your IP |
| `DB connection failed: has no attribute 'seed_skill_files_from_disk'` | Running stale image built before recent code changes | Rebuild and redeploy: `make deploy` then restart container |
| `DB connection failed: Name not known` | Docker can't resolve RDS hostname | Use private IP instead of hostname in `DATABASE_URL` |
| App loads but runs show empty | App running without DB (check `docker logs`) | See above two rows |
| `telnet RDS_IP 5432` hangs | RDS security group blocks port 5432 | Add inbound TCP 5432 from `192.168.0.0/24` |
| `docker` not found on ECS | Docker not installed | Install Docker manually via browser terminal (Step 3.2) |
| Browser terminal shows `>` prompt | Multiline command paste issue | Run each command separately, no backslashes |
