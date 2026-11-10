# Operations: limits, reports and alerts

Nothing in this file has been applied. Every setting is a ready-to-paste block with a rollback line. Paths and unit names are the usual ones
for a Debian/Ubuntu server: check them with `systemctl list-units 'mavlit*' 'postgresql*'` first.

Server assumed: 8 GB RAM, 4 cores, running `mavlit` (the API, one uvicorn worker), `mavlit-frontend` (Node), PostgreSQL 18 and Docker (SearXNG).

## 1. The usage report

`scripts/sql/usage_report.sql` is read-only (nine SELECTs: top LLM cost users, users at 80% of a daily cap, Apify and contact reveals per user, failed
logins by address and by hashed account, locks older than 3 minutes, jobs stuck in queued/running). It prints creator ids, 12-character hash
prefixes and client addresses, and no email addresses.

```bash
PGOPTIONS='-c default_transaction_read_only=on' psql "$DATABASE_URL" -X -f scripts/sql/usage_report.sql
```

`DATABASE_URL` is the API's own (`mavlit_app` can read every table in it). The daily caps in query 2 are the defaults (30/20/10/10/10/20): edit its `caps`
table if you changed `PITCHES_PER_DAY`, `RATE_INTELLIGENCE_PER_DAY`, `CONTRACT_ADVICE_PER_DAY`, `MATCH_REFRESH_PER_DAY`, `OWN_BRANDS_PER_DAY`
or `DESCRIPTION_GENERATIONS_PER_DAY`. Run it daily, and whenever an alert below fires.

## 2. What to alert on

| Signal | Threshold | Where to look |
|---|---|---|
| LLM spend | above your daily budget; or one user above 3 times the median spend | report 1 |
| A user at a daily cap | any user at 100% (and many 429s) | report 2 |
| Apify runs | one user above 20 runs in 24 h | report 3 |
| Contact reveals | no limit; watch one user opening far more brands than usual | report 4 |
| Failed logins | 10 or more from one address in 15 minutes; or lockouts on many accounts | reports 5, 6 |
| A held lock | any `feature_locks` row older than 3 minutes | report 7 |
| A stuck job | any job queued/running for over 30 minutes | reports 8, 9 |
| 503 rate | above 1% of requests (the API is busy: slow-route limit or job queue full) | access log / nginx |
| `/health` | slower than 1 s, or failing | uptime monitor on `/health` (it needs no database or worker thread) |
| Memory | `mavlit` above `MemoryHigh` for 10 minutes; any OOM kill | `systemctl status mavlit`, `journalctl -k | grep -i oom` |
| Database connections | above 80 of `max_connections` | `SELECT count(*) FROM pg_stat_activity` |

## 3. systemd limits (drop-in files)

Create each file, then `sudo systemctl daemon-reload && sudo systemctl restart <unit>`. Check with `systemctl show <unit> -p MemoryMax -p TasksMax`.

### mavlit (the API)
`/etc/systemd/system/mavlit.service.d/limits.conf`
```ini
[Service]
MemoryHigh=2G
MemoryMax=2.5G
CPUQuota=250%
TasksMax=512
LimitNOFILE=4096
OOMScoreAdjust=300
```
Rollback: `sudo rm /etc/systemd/system/mavlit.service.d/limits.conf && sudo systemctl daemon-reload && sudo systemctl restart mavlit`

### mavlit-frontend (Node)
`/etc/systemd/system/mavlit-frontend.service.d/limits.conf`
```ini
[Service]
MemoryHigh=384M
MemoryMax=512M
CPUQuota=100%
TasksMax=256
OOMScoreAdjust=300
```
Rollback: `sudo rm /etc/systemd/system/mavlit-frontend.service.d/limits.conf && sudo systemctl daemon-reload && sudo systemctl restart mavlit-frontend`

### PostgreSQL (protected from the OOM killer)
`/etc/systemd/system/postgresql.service.d/oom.conf` (on Debian the running unit may be `postgresql@18-main.service`: use that name for the directory)
```ini
[Service]
OOMScoreAdjust=-900
```
No `MemoryMax` on PostgreSQL: killing it is worse than anything it could save. Rollback: `sudo rm /etc/systemd/system/postgresql.service.d/oom.conf && sudo systemctl daemon-reload && sudo systemctl restart postgresql`

## 4. PostgreSQL settings (8 GB server)

`/etc/postgresql/18/main/conf.d/90-mavlit.conf` (or the `include_dir` of your install)
```conf
shared_buffers = 2GB            # needs a restart
effective_cache_size = 4500MB   # 4.5 GB; a reload is enough
work_mem = 16MB                 # per sort/hash per connection; the API holds at most 17 connections, so this is safe
```
Apply: `sudo systemctl restart postgresql` (for `shared_buffers`). Check: `SHOW shared_buffers;`.
Rollback: `sudo rm /etc/postgresql/18/main/conf.d/90-mavlit.conf && sudo systemctl restart postgresql`

## 5. SearXNG container limits

Limit the container, not Docker itself. For a running container named `searxng`:
```bash
docker update --memory 768m --memory-swap 768m --cpus 1 --pids-limit 256 searxng
```
For `docker run` / a compose file: `--memory 768m --memory-swap 768m --cpus 1 --pids-limit 256` (compose: `mem_limit`, `memswap_limit`, `cpus`, `pids_limit`).
Rollback: `docker update --memory 0 --memory-swap -1 --cpus 0 --pids-limit -1 searxng`

## 6. Swap (optional, recommended)

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/90-mavlit.conf && sudo sysctl --system
```
Add `/swapfile none swap sw 0 0` to `/etc/fstab` to keep it after a reboot.
Rollback: `sudo swapoff /swapfile && sudo rm /swapfile /etc/sysctl.d/90-mavlit.conf` (and remove the `/etc/fstab` line).

## 7. Bind addresses: only nginx faces the internet

The API and the helper services must listen on the loopback address only, with nginx (or Cloudflare Tunnel) in front: a port bound to `0.0.0.0` is reachable
from the internet unless a firewall blocks it, and anyone who reaches it directly bypasses nginx's limits and the real-client-address logic.

* **uvicorn (the API):** start it with `--host 127.0.0.1 --port <port>` (in the `ExecStart` of the `mavlit` unit) and point nginx's `proxy_pass` at
  `http://127.0.0.1:<port>`. Keep `--workers 1` (startup recovery in `api/recovery.py` is only safe with one worker).
* **gmaps_scraper:** it is a separate service that is not part of this repository, so I could not inspect how it starts. Bind it the same way
  (`127.0.0.1`, or a Unix socket) and let only the processes on the same machine call it.
* **Check:** `sudo ss -ltnp | grep -E 'uvicorn|python|node|gmaps'`. Every line should show `127.0.0.1:<port>` (or `[::1]`), never `0.0.0.0` or `*`; only nginx
  should listen on 80/443. Also keep PostgreSQL on `localhost` and the firewall (`ufw status`) closed to everything but 22, 80 and 443.
* **Rollback:** restore the previous `ExecStart` line and `sudo systemctl daemon-reload && sudo systemctl restart mavlit`.

## 8. New environment variables (all optional)

`MATCH_SHORTLIST_MAX` (500), `THUMBNAIL_CACHE_MAX_BYTES` (67108864), `THUMBNAIL_CACHE_MAX_ENTRIES` (300); see `.env.example`. A bad value stops startup.
