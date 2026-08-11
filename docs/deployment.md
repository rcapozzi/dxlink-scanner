# Deployment & Operations

Guide for deploying, operating, and troubleshooting the Options Radar Zero Scanner.

## Deployment Options

### 1. Direct (Systemd Service)

```ini
# /etc/systemd/system/dxlink_scanner.service
[Unit]
Description=Options Radar Zero Scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=scanner
WorkingDirectory=/opt/scanner
EnvironmentFile=/opt/scanner/.env
ExecStart=/opt/scanner/.venv/bin/python -m dxlink_scanner.cli --config /opt/scanner/production.yaml
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=scanner

# Resource limits
MemoryMax=2G
CPUQuota=200%

# Graceful shutdown
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

```bash
# Install
sudo cp dxlink_scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable scanner
sudo systemctl start scanner

# Logs
sudo journalctl -u scanner -f
```

### 2. Docker

```dockerfile
# Dockerfile
FROM python:3.14-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/
COPY production.yaml ./
COPY .env ./

# Data directory
VOLUME ["/app/data"]

# Run scanner
CMD ["uv", "run", "dxlink-scanner", "--config", "production.yaml"]
```

```yaml
# docker-compose.yml
version: '3.8'
services:
 scanner:
  build: .
  volumes:
   - ./data:/app/data
   - ./production.yaml:/app/production.yaml:ro
   - ./.env:/app/.env:ro
  environment:
   - TASTY_CLIENT_ID=${TASTY_CLIENT_ID}
   - TASTY_CLIENT_SECRET=${TASTY_CLIENT_SECRET}
   - TASTY_REFRESH_TOKEN=${TASTY_REFRESH_TOKEN}
  restart: unless-stopped
  logging:
   driver: json-file
   options:
    max-size: "10m"
    max-file: "5"
```

```bash
# Build & run
docker compose up -d --build

# Logs
docker compose logs -f scanner
```

### 3. Kubernetes (CronJob for Daily Restart)

```yaml
# k8s/scanner-cronjob.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
 name: scanner
 namespace: trading
spec:
 schedule: "0 21 * * 1-5" # 17:00 ET = 21:00 UTC, Mon-Fri (equity options only)
 timeZone: "America/New_York"
 concurrencyPolicy: Forbid
 jobTemplate:
  spec:
   template:
    spec:
     serviceAccountName: scanner
     restartPolicy: OnFailure
     containers:
     - name: scanner
      image: scanner:latest
      command: ["uv", "run", "dxlink-scanner", "--config", "/config/production.yaml"]
      envFrom:
      - secretRef:
        name: scanner-secrets
      volumeMounts:
      - name: data
       mountPath: /app/data
      - name: config
       mountPath: /config
       readOnly: true
      resources:
       requests:
        memory: "512Mi"
        cpu: "500m"
       limits:
        memory: "2Gi"
        cpu: "2000m"
     volumes:
     - name: data
      persistentVolumeClaim:
       claimName: scanner-data
     - name: config
      configMap:
       name: scanner-config
```

```bash
# Deploy
kubectl apply -f k8s/

# Manual trigger
kubectl create job --from=cronjob/scanner scanner-manual-$(date +%s)

# Logs
kubectl logs -l job-name=scanner-... -f
```

## Environment Setup

### Required Environment Variables

```bash
# .env file (never commit!)
TASTY_CLIENT_ID=your_client_id
TASTY_CLIENT_SECRET=your_client_secret
TASTY_REFRESH_TOKEN=your_refresh_token
TASTY_SANDBOX=false # or true for testing
```

### Directory Structure

```
/opt/scanner/
├── .venv/       # Virtual environment
├── src/        # Source code
├── production.yaml   # Config (gitignored)
├── .env        # Secrets (gitignored, 600 perms)
├── data/        # Parquet output (persistent volume)
│  └── events/
│    ├── 2024-07-30/
│    └── 2024-07-31/
└── logs/        # Application logs (if not using journal)
```

### Permissions

```bash
# Create service user
sudo useradd -r -s /bin/false -d /opt/scanner scanner

# Set ownership
sudo chown -R scanner:scanner /opt/scanner
sudo chmod 600 /opt/scanner/.env
sudo chmod 750 /opt/scanner/data
```

## Daily Operations

### Market Day Schedule

| Time (ET) | Action |
|-----------|--------|
| 09:00 | Pre-market: verify scanner running, check logs |
| 09:30 | Market open: monitor alert rate, queue depth |
| 16:00 | Market close (RTH): volume spike expected |
| 16:15 | Post-market: verify Parquet flush |
| 17:00 | **SIGTERM sent** — graceful shutdown (equity-only) / **continue** (futures overnight) |
| 17:00-18:00 | Futures overnight session (if futures in watchlist); equity scanner restart |
| 02:00 | Compaction job runs (cron) |

### Graceful Shutdown Process

The scanner handles `SIGTERM` for clean shutdown:

```python
# In cli.py:_run_scanner()
shutdown_event = asyncio.Event()

def _signal_handler():
  shutdown_event.set()

signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

await shutdown_event.wait()

# Cleanup sequence:
logger.info("Shutdown signal received, flushing...")
await store.flush_remaining()  # Write remaining Parquet
await store.stop_flush_loop()  # Stop background task
for task in producer_tasks:
  task.cancel()
await asyncio.gather(*producer_tasks, return_exceptions=True)
await consumer_task
```

**Trigger**: External scheduler (systemd timer, cron, K8s CronJob) sends `SIGTERM` at 17:00 ET (equity options only). For futures, the scanner continues running overnight — no auto-shutdown until Friday 17:00 ET (weekend).

### Health Checks

```bash
# Check process
systemctl status scanner
# or
docker compose ps

# Check recent alerts
tail -100 /opt/scanner/data/alerts.log # if logging to file

# Check queue depth (via logs)
grep "queue depth" /var/log/dxlink_scanner.log

# Check Parquet flush
ls -la /opt/scanner/data/events/$(date +%F)/

# Verify session metadata
cat /opt/scanner/data/events/$(date +%F)/session_meta.json
```

### Key Metrics to Monitor

| Metric | Healthy Range | Alert If |
|--------|---------------|----------|
| Process uptime | 6.5 hours (market day) | < 5 hours |
| Alert rate | 10-1000/min (varies) | 0 for > 10 min |
| Queue depth | < 100 | > 400 (80% of max) |
| Parquet flush latency | < 50ms | > 500ms |
| Backpressure drops | 0 | > 10/min |
| DXLink reconnects | 0-1/day | > 5/day |

## Troubleshooting

### Common Issues

#### Scanner Won't Start

```bash
# Check config syntax
uv run python -c "from dxlink_scanner.config import load_config; load_config('production.yaml')"

# Check credentials
uv run python -c "
from tastytrade.session import Session
import os
s = Session(
  provider_secret=os.environ['TASTY_CLIENT_SECRET'],
  refresh_token=os.environ['TASTY_REFRESH_TOKEN'],
  is_test=os.environ.get('TASTY_SANDBOX', 'false').lower() == 'true'
)
print('Session OK, DXLink token:', s.dxlink_token[:20] + '...')
"
```

**Common causes**:
- Invalid YAML syntax → `yamllint production.yaml`
- Missing env vars → check `.env` file exists and is readable
- Expired refresh token → regenerate in Tastytrade developer portal
- Network/firewall blocking `api.tastytrade.com` or DXLink WS

#### No Alerts Firing

```bash
# Check rule config
grep -A 20 "alert_rules:" production.yaml

# Check detection thresholds
grep -A 5 "detection:" production.yaml

# Verify data flowing
grep "Received" /var/log/dxlink_scanner.log | tail -5
```

**Common causes**:
- `alert_rules` empty and no default rules
- `size_mult` too high / `abs_min_size` too high
- Rolling stats not warmed up (first 50 trades per symbol)

#### High Backpressure Drops

```bash
# Check drops
grep "backpressure_dropped_total" /var/log/dxlink_scanner.log

# Check queue config
grep "backpressure_queue_size" production.yaml
```

**Fixes**:
- Increase `stream.backpressure_queue_size` (default 500 → 1000-2000)
- Reduce `flush_interval_sec` (default 5s → 2s)
- Check consumer latency (rule engine too slow?)

#### Parquet Write Failures

```bash
# Check disk space
df -h /opt/scanner/data

# Check permissions
ls -la /opt/scanner/data/events/

# Check recent errors
grep "Failed to flush parquet" /var/log/dxlink_scanner.log
```

**Common causes**:
- Disk full
- Permission denied on data directory
- Schema mismatch (mixed v1/v2 files)

#### DXLink Disconnection

```bash
# Check reconnect logs
grep -i "reconnect\|disconnect\|connection" /var/log/dxlink_scanner.log

# Test WS connectivity
wscat -c "wss://dxlink.tastytrade.com" # or sandbox
```

**Note**: Current MVP lets process die on DXLink error. Post-MVP will add exponential backoff + resubscribe.

### Debugging Commands

```bash
# Run in foreground with debug logging
uv run dxlink-scanner --config production.yaml 2>&1 | head -100

# Dump raw DXLink messages (for schema validation)
uv run dxlink-scanner --config production.yaml --debug-messages

# Test config loading only
uv run python -c "
from dxlink_scanner.config import load_config
cfg = load_config('production.yaml')
print('Symbols:', cfg.watchlist.symbols)
for t in cfg.watchlist.tickers:
  print(f'  {t.symbol}: rules={len(t.alert_rules)}, underlying_alert_rules={len(t.underlying_alert_rules)}')
"

# Inspect parquet files
uv run python -c "
import polars as pl
df = pl.read_parquet('data/events/2024-07-31/*.parquet')
print(df.shape)
print(df.columns)
print(df.filter(pl.col('source_type') == 'TIME_AND_SALE').head())
"
```

## Maintenance

### Daily
- [ ] Verify scanner started before 09:30 ET
- [ ] Check alert rate is non-zero during market hours
- [ ] Confirm Parquet files written for the day
- [ ] Review any ERROR logs

### Weekly
- [ ] Run compaction manually to verify: `uv run python scripts/compact_parquet.py --data-dir data/events --date YYYY-MM-DD`
- [ ] Check disk usage: `du -sh data/events/`
- [ ] Review backpressure drop counts
- [ ] Verify webhook delivery success rate (if enabled)

### Monthly
- [ ] Rotate refresh token (Tastytrade tokens expire ~90 days)
- [ ] Update dependencies: `uv sync --upgrade`
- [ ] Run full test suite: `uv run pytest tests/ -v`
- [ ] Review and archive old parquet data (> 90 days)

### Quarterly
- [ ] Review and update detection thresholds based on market regime
- [ ] Evaluate CEL rule performance and accuracy
- [ ] Capacity planning: project data growth
- [ ] Security audit: dependency scan `uv run pip-audit`

## Backup & Disaster Recovery

### What to Back Up

| Data | Frequency | Retention |
|------|-----------|-----------|
| `data/events/` (Parquet) | Daily (rsync/S3 sync) | 90 days hot, 1 year cold |
| `production.yaml` | On change | Indefinite (git) |
| `.env` (secrets) | On change | Secure vault |
| `session_meta.json` | With Parquet | Same as Parquet |

### Recovery Procedure

```bash
# 1. Restore config & secrets
cp /backup/production.yaml /opt/scanner/
cp /backup/.env /opt/scanner/

# 2. Restore data (if needed)
rsync -av /backup/data/events/ /opt/scanner/data/events/

# 3. Reinstall dependencies
cd /opt/scanner && uv sync --frozen

# 4. Start scanner
systemctl start scanner

# 5. Verify
systemctl status scanner
journalctl -u scanner -n 50
```

## Scaling Considerations

### Vertical Scaling (Single Instance)
- **Memory**: ~20 MB per 10K symbols (dataclass with slots)
- **CPU**: ~1 core handles 100K events/sec
- **Disk I/O**: Parquet flush every 5s / 10K events
- **Network**: DXLink ~1-5 MB/s sustained

### Horizontal Scaling (Multiple Instances)
**Not currently supported** — single DXLink connection per account.

**Future options**:
- Multiple Tastytrade accounts → multiple scanner instances
- Shared Parquet storage (S3/NFS) for consolidated analytics
- Redis-backed `SnapshotStore` for cross-instance state

### Adding Symbols
```yaml
watchlist:
 tickers:
  - symbol: "NEW_SYMBOL"
   option_type: "equity" # or "futures"
   strikes_around_atm: 10
   # ... other settings
```
Restart required to pick up new symbols (dynamic strike management is roadmap item).

## Security

### Secrets Management
- **Never** commit `.env` or `production.yaml` with real credentials
- Use secret manager (HashiCorp Vault, AWS Secrets Manager, Kubernetes Secrets)
- Rotate refresh tokens every 60-90 days
- Use sandbox for development: `TASTY_SANDBOX=true`

### Network
- DXLink: `wss://dxlink.tastytrade.com` (443/WS)
- REST API: `https://api.tastytrade.com` (443/HTTPS)
- Outbound only — no inbound ports required
- Webhook: outbound HTTPS to configured URL

### File Permissions
```bash
# Enforce
chmod 600 /opt/scanner/.env
chmod 640 /opt/scanner/production.yaml
chown scanner:scanner /opt/scanner/.env /opt/scanner/production.yaml
```

## Version Upgrades

```bash
# 1. Backup
tar -czf /backup/scanner-$(date +%F).tar.gz /opt/scanner/production.yaml /opt/scanner/.env /opt/scanner/data

# 2. Pull latest code
cd /opt/scanner && git pull

# 3. Update dependencies
uv sync --frozen

# 4. Run migrations (if any)
uv run python -m dxlink_scanner.migrations # if schema changes

# 5. Run tests
uv run pytest tests/ -q

# 6. Restart
systemctl restart scanner

# 7. Verify
systemctl status scanner
journalctl -u scanner -f
```

## Support Contacts

| Issue | Contact |
|-------|---------|
| Tastytrade API/DXLink | developer@tastytrade.com |
| Scanner code | Internal team |
| Infrastructure | DevOps team |
| Emergency (market hours) | On-call rotation |

---

*See also: [architecture.md](architecture.md), [configuration.md](configuration.md), [data_files.md](data_files.md)*