# SAP → DataTwin sync on AWS: 1-minute cron setup

As of 2026-11-25

> **Not the current setup.** The 1-minute sync now runs inside the sap-bank-clearing-api container and ships with its normal CodeBuild deploy: see "SAP sync every minute" in the repository's main `README.md`. Use this guide only to run the sync as a separate service with an OS cron instead. Never run both.

This guide sets up the SAP sync on the EC2 server, step by step. When you finish, a cron job runs every minute and:

1. Reads SAP tables BSID, BSAD, VBRK and VBRP through OData.
2. Finds the rows that are new or changed since the last minute. Each new SAP row gets a `passportid`.
3. Sends only those rows to the Data Modelling & Ingestion API, which writes them to `active_bt3h` / `active_bt3i`.

```
cron (every minute, user sapapp)
  └─ deploy/run_cron.sh delta --cron
       ├─ poll SAP BSID / BSAD / VBRK / VBRP  ──►  active_bsid / active_bsad / active_vbrk / active_vbrp  (queue)
       └─ one ingestion job at a time          ──►  Ingestion API  ──►  active_bt3h / active_bt3i
```

Each cron run takes a few seconds and then exits. It does not wait for the ingestion job: the next minute's run checks it. If a run is still busy when the next one starts, the new run skips the busy part (file locks), so nothing runs twice.

> The cron replaces the systemd worker (`sap-extraction-worker`). Run one or the other, never both. The install script switches the worker off for you.

---

## 0. Before you start

| # | Check | How |
|---|---|---|
| 1 | You can open a shell on the EC2 server (dev: `i-019cd9689708261b3`, us-east-1) | AWS console → EC2 → instance → **Connect** → *Session Manager*, or `aws ssm start-session --target i-019cd9689708261b3` |
| 2 | The server can reach SAP OData and PostgreSQL | Tested automatically in step 5. If it fails, open the security group / firewall for the SAP host port (`44343`) and the DB port |
| 3 | The server can reach PyPI (to install Python packages) | `curl -sI https://pypi.org | head -1` |
| 4 | Python 3.9 or newer is installed | `python3 --version` (Amazon Linux 2023 has 3.9). On Ubuntu also run `sudo apt-get install -y python3-venv` |
| 5 | The ingestion service accepts `process_code` | `curl -s https://lv.datatwin.ai/data-model-ingestion/openapi.json | grep -c process_code` must print a number greater than 0. If it prints 0, every run fails with "does not accept" and rows wait as PENDING |
| 6 | DB objects exist (themes, extra columns, delta tables) | **Dev DB `dm00005`: already done.** For a new DB, see [DEPLOYMENT.md](DEPLOYMENT.md) sections 1 and 5 |

---

## 1. Build the bundle (your Windows PC)

```powershell
cd C:\sap-bank-clearing-api\sap-extraction-api
python deploy\build_bundle.py
```

This creates `dist\sap-extraction-api-<YYYYMMDD-HHMM>.tar.gz`. The bundle contains the code, the `deploy/` scripts and these docs, but **never `config.ini`**.

## 2. Copy the bundle to the server

The EC2 role can read S3, so S3 is the easiest way to get files to the server:

```powershell
# your PC (needs AWS credentials); <stamp> = the date-time in the file name from step 1
aws s3 cp dist\sap-extraction-api-<stamp>.tar.gz s3://codepipeline-us-east-1-922151790333/sap-extraction-api/ --sse aws:kms
```

```bash
# on the server (Session Manager shell)
aws s3 cp s3://codepipeline-us-east-1-922151790333/sap-extraction-api/sap-extraction-api-<stamp>.tar.gz /tmp/
```

If you have SSH access, `scp` straight to `/tmp/` works too.

## 3. Unpack it into `/opt/sap-extraction-api`

```bash
cd /opt
sudo tar -xzf /tmp/sap-extraction-api-<stamp>.tar.gz     # creates /opt/sap-extraction-api
ls /opt/sap-extraction-api                                      # cli.py, deploy/, docs/, config.example.ini ...
```

The cron file and the systemd units expect exactly this folder.

## 4. Create `config.ini` on the server

`config.ini` holds the SAP and DB passwords. It lives only on the server: never in git, never in S3, never in the bundle.

```bash
cd /opt/sap-extraction-api
sudo cp config.example.ini config.ini
sudo chmod 600 config.ini
sudo nano config.ini            # or vi
```

Fill in these values. Everything else can keep its default. The SAP and DB values are the same ones the bank API's `config.ini` uses (same key names).

```ini
[environment]
; which DB section below is used
current_env = development

[development]
ec2_ip = <postgres host>
port = 5433
db_name = dm00005
db_user = <db user>
db_password = <db password>

[defaults]
tenant_id = DM00005
; DataTwin org of the SAP data (on dev the SAP rows in active_bt3h have orgid OM0000000002)
org_id = OM0000000002

[sap]
host = <sap host>
port = 44343
client = 200
user = <sap user>
password = <sap password>
; keep KNA1 here only if you also want the daily full load; delta tables are skipped here anyway
tables = KNA1

[delta]
; the 4 tables synced every minute (header VBRK before items VBRP)
tables = BSID, BSAD, VBRK, VBRP
poll_seconds = 60

[ingestion]
enabled = true
base_url = https://lv.datatwin.ai/data-model-ingestion
```

The `[ingestion]` keys `process_code_*`, `transaction_type_*`, `theme_name_*` and `columns_*` come pre-filled in `config.example.ini` for all 4 tables. Leave them as they are.

Rules for `config.ini`:
- Put each comment on its own line. A comment after a value becomes part of the value.
- To keep a secret out of the file, put it in `/opt/sap-extraction-api/.env` instead, e.g. `DB_PASSWORD=...` or `SAP_PASSWORD=...`. Environment variables override `config.ini`, and the cron wrapper loads `.env`.
- Every cron run reads `config.ini` fresh, so **a change takes effect from the next minute**. You don't need to restart anything.

## 5. Install and start the cron

```bash
cd /opt/sap-extraction-api
sudo DELTA_MODE=cron bash deploy/install.sh                    # add CLOUDWATCH=true to ship the logs to CloudWatch
```

What the script does (you can run it again any time):

| Step | What happens |
|---|---|
| 1 | Creates the service user `sapapp` |
| 2 | Creates `.venv` and installs `requirements.txt` |
| 3 | Creates `log/` and `data/`, makes `sapapp` the owner, sets `config.ini` to mode 600 |
| 4 | **Tests SAP + DB** (`cli.py check`). If either fails, the script stops here and nothing is started |
| 5 | Shows `delta-status` (the 4 tables must show `initialized: true`) |
| 6 | Starts the local status API `sap-extraction-api` (`127.0.0.1:8002`) |
| 7 | Installs `cron` if missing (Amazon Linux 2023: `cronie`) and writes `/etc/cron.d/sap-extraction-delta`; stops and disables the systemd worker; sets up daily rotation for `log/cron.out` |
| (opt.) | With `CLOUDWATCH=true`: installs and starts the CloudWatch agent for the log files (see [Send the logs to CloudWatch](#send-the-logs-to-cloudwatch)) |

The cron file it writes:

```
* * * * * sapapp /opt/sap-extraction-api/deploy/run_cron.sh delta --cron >> /opt/sap-extraction-api/log/cron.out 2>&1
```

## 6. Check that it works

```bash
cat /etc/cron.d/sap-extraction-delta                    # the cron line is there
systemctl is-active crond 2>/dev/null || systemctl is-active cron   # active
systemctl is-enabled sap-extraction-worker              # disabled (must not run next to cron)

tail -f /opt/sap-extraction-api/log/cron.log            # a line when SAP data arrives or is sent
tail -f /opt/sap-extraction-api/log/cron.out            # one JSON line per run that moved data
```

On a minute with nothing new in SAP, **nothing is written** (see [Logs](#logs-where-and-when)). To confirm the cron is alive, check that `last_poll_at` moves forward every minute:

```bash
sudo -u sapapp /opt/sap-extraction-api/.venv/bin/python /opt/sap-extraction-api/cli.py delta-status   # last_poll_at per table
journalctl -u crond --since "5 min ago"        # Amazon Linux 2023: one CMD line per minute (Ubuntu: -u cron)
```

```sql
-- PENDING should go to DONE within a few minutes
SELECT ingestion_status, count(*) FROM active_bsid GROUP BY 1;
-- last ingestion runs
SELECT run_id, sap_table, status, record_count, left(error, 120), started_at
FROM sap_ingestion_run ORDER BY started_at DESC LIMIT 10;
```

**End-to-end test:** post a customer invoice in SAP, then wait 1–2 minutes. It should appear in `active_bsid` as `NEW` / `PENDING`, then `DONE`, and then in `active_bt3h` (theme `SAP_BSID_INV`) with its `passportid`.

---

## Logs: where and when

The cron runs every minute, but **a quiet minute writes nothing**. Logs are written only when data moves or something fails.

| File (on the server) | Written when | Example |
|---|---|---|
| `log/cron.log` | SAP rows found (new / changed / removed), passport IDs reserved, an ingestion job submitted / finished, or an error | `2026-11-25 14:02:05 [INFO] [SAP_Pipeline.Delta] [DELTA] BSID: new=2 changed=0 removed=0 pending=2 passportids=['PPSAP0226270000001', ...]` |
| `log/cron.out` | Same runs as above: one JSON line per run with the full result (`at` = run time), plus any Python crash output | `{"at":"2026-11-25T14:02:05","poll":{...},"ingestion":{...},"status":"success"}` |

- A minute where SAP has nothing new: no SAP rows found, no ingestion call (the ingestion service is called only when rows are PENDING), no log line.
- While an ingestion job is running, each minute writes one "still running" line until the job finishes.
- **Errors are always written**: SAP or DB not reachable, ingestion service down while rows are waiting, a failed job. An outage therefore writes an error every minute until it is fixed.
- Folder: `/opt/sap-extraction-api/log/`. `cron.log` rotates at midnight (kept 30 days, `[logging] retention_days`); `cron.out` rotates daily (kept 14 days, logrotate).
- More detail for a while: set `[logging] level = DEBUG` in `config.ini` (applies from the next minute). This logs every poll, including "no changes". Set it back to `INFO` afterwards.

### Send the logs to CloudWatch

The CloudWatch agent tails the log files and ships each new line to CloudWatch Logs. Because quiet minutes write nothing, CloudWatch only receives the minutes where data moved or something failed.

**1. Give the EC2 role permission (one time, AWS console / IAM).** Add this statement to the instance role's policy. It is already in this repo's `deploy/iam/ec2-policy.json`. If the role already has the AWS managed policy `CloudWatchAgentServerPolicy`, nothing is needed.

```json
{ "Sid": "SapExtractionLogsToCloudWatch", "Effect": "Allow",
  "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
             "logs:DescribeLogStreams", "logs:PutRetentionPolicy"],
  "Resource": ["arn:aws:logs:us-east-1:203456943124:log-group:/sap-extraction-api",
               "arn:aws:logs:us-east-1:203456943124:log-group:/sap-extraction-api:*"] },
{ "Sid": "CloudWatchAgentDescribe", "Effect": "Allow", "Action": "logs:DescribeLogGroups", "Resource": "*" }
```

**2. Install the agent (on the server).** Run the install with `CLOUDWATCH=true`:

```bash
cd /opt/sap-extraction-api
sudo DELTA_MODE=cron CLOUDWATCH=true bash deploy/install.sh
```

This installs `amazon-cloudwatch-agent` if it is missing, writes `deploy/cloudwatch-agent.json` and starts the agent. It adds to the agent's configuration, so any other log shipping already set up on this server is kept.

| Setting | Default | Change with |
|---|---|---|
| Log group | `/sap-extraction-api` | `CW_LOG_GROUP=/datatwin/dev/sap-extraction` (also change the IAM resource) |
| Retention | 30 days | `CW_RETENTION_DAYS=90` |

**3. Find the logs.** CloudWatch → Log groups → `/sap-extraction-api`. There is one stream per file: `<instance-id>/cron.log`, `<instance-id>/cron.out` (plus `worker.log` and `api.log` when those run).

Useful Logs Insights query (only data that came in):

```
fields @timestamp, @message
| filter @message like /\[DELTA\]/ or @message like /\[INGEST\] run/
| sort @timestamp desc
| limit 100
```

**4. Optional alarm on errors:** create a metric filter on the log group with the pattern `"[ERROR]"`, then add a CloudWatch alarm on that metric (> 0 in 5 minutes) that sends to an SNS topic or email.

Check the agent on the server: `sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status`. The agent's own log is `/opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log`; look there for `AccessDenied` if no logs arrive.

---

## Daily operations

| Task | Command (on the server) |
|---|---|
| Pause the sync | `sudo sed -i 's/^\* \* \* \* \*/# * * * * */' /etc/cron.d/sap-extraction-delta` |
| Resume the sync | `sudo sed -i 's/^# \* \* \* \* \*/* * * * */' /etc/cron.d/sap-extraction-delta` |
| Remove the cron entirely | `sudo rm /etc/cron.d/sap-extraction-delta` |
| Run one cycle by hand | `sudo -u sapapp /opt/sap-extraction-api/deploy/run_cron.sh delta --cron` |
| Re-queue rows that failed 3 times | `sudo -u sapapp /opt/sap-extraction-api/.venv/bin/python /opt/sap-extraction-api/cli.py delta-retry BSID` |
| Change a setting | edit `config.ini`; it takes effect at the next minute |
| Deploy a new version | steps 1–3 with the new bundle (`config.ini`, `log/` and `data/` are kept), then `sudo DELTA_MODE=cron bash deploy/install.sh` |
| Switch to the systemd worker instead | `sudo DELTA_MODE=worker bash deploy/install.sh` (removes the cron file) |

Pausing is always safe. New SAP rows are picked up by the first run after you resume, and rows already queued wait as PENDING.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `delta-status` shows `last_poll_at` not moving | Cron is not running (an empty `cron.out` alone is normal on quiet minutes). Start it: `sudo systemctl enable --now crond` (Ubuntu: `cron`). The cron file must be owned by root, mode 644, and its name must have no dot |
| Nothing arrives in CloudWatch | `amazon-cloudwatch-agent-ctl -a status` must say running; `AccessDenied` in the agent log means the IAM statement is missing (see [Send the logs to CloudWatch](#send-the-logs-to-cloudwatch)) |
| `"status":"failed"` from the `ingestion` step: *does not accept [...]* or *not answering* | Ingestion service is old or down (check 5 in section 0). Rows wait as PENDING and are sent once it works |
| A table shows `"status":"skipped"` | The previous minute's poll of that table is still running. Fine now and then. If it happens every minute, the poll takes more than 60 s (see `elapsed_seconds` in `delta-status`) |
| `"status":"failed"` in a poll, *not a delta table yet* | Run `cli.py delta-init <TABLE>` once (see [DEPLOYMENT.md](DEPLOYMENT.md) section 5) |
| Install stops at step 4 | SAP or DB not reachable, or a wrong password in `config.ini`. Fix it and run the install again |
| Ingestion runs FAILED again and again | `error` column in `sap_ingestion_run`. After 3 failures the rows are `FAILED`; fix the cause, then `delta-retry` |

Exit codes of `run_cron.sh` (shown with `echo $?` after a run by hand): `0` ok, `1` failed, `2` partly failed, `75` skipped (another run still holds the lock).
