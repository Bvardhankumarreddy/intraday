# Deployment runbook: k3s on sidestream-app (AWS ap-south-1)

PAPER TRADING ONLY. The cluster can only simulate orders. Real orders stay
manual.

## What runs where

| Item | Value |
|---|---|
| Host | EC2 `sidestream-app` (`i-07ac211ef76c64405`, t3.large, Ubuntu 22.04), shared with the video-streaming platform (Docker + Caddy 80/443) |
| Kubernetes | k3s v1.36.5, single node. Traefik and ServiceLB **disabled** (Caddy keeps 80/443). Secrets encrypted at rest (`--secrets-encryption`) |
| Namespace | `nse-intraday`. Quota: requests 1.5 CPU / 2 Gi; limits 3 CPU / 4 Gi; no LoadBalancer or NodePort services |
| Code | `/opt/nse-intraday/src` (clone of this repo); image `nse-intraday:<commit>`, built on the node and imported into k3s (no registry) |
| Data | PVC `nse-data` (5 Gi, local-path) mounted at `/app/data` (paper DBs, ledgers, reports, logs, backups) |
| Secrets | Secret `nse-env` (key `env` = the `.env` file), mounted read-only at `/secrets/env` |
| Schedules (IST) | CronJob `session` 08:55 Mon–Fri: `python -m app.integrated run` (both arms + agent + monitoring; skips NSE holidays itself). CronJob `post-session` 16:00 Mon–Fri: report, learning, full monitoring, backups, retention |
| Dashboard | Deployment `dashboard` + ClusterIP Service `10.43.71.225:8501` (pinned). Public URL **https://intraday.sidestream.in** through the host's Caddy: Let's Encrypt TLS, **login required** (basic auth, user `nse`), HSTS, `noindex`. DNS: Route 53 A record in zone `sidestream.in` → Elastic IP `3.111.119.69` |
| Pods | non-root uid 10001, read-only root filesystem, no capabilities, seccomp RuntimeDefault, CPU and memory limits |

Integrated evaluation started 2026-10-20 (from 2026-10-21): baseline
`EVAL-b3d0fa9f3a`, AI-assisted `EVAL-d18cb2b8f9`, protocol `ai-eval-v1`, live
configuration matching the frozen baseline.

Verified at deployment (2026-10-20):

- the video platform was identical before and after the k3s install (ports,
  11 healthy containers, Postgres, Redis, Caddy on 80/443);
- the Anthropic check from a pod returned OK;
- the Groww probe from AWS authenticated and connected (a stale quote was
  expected on the holiday);
- the health check inside the cluster passed for config.

## Daily use

```bash
ssh -i ~/.ssh/sidestream-keypair.pem ubuntu@3.111.119.69
alias k='sudo k3s kubectl -n nse-intraday'

k get cronjobs,jobs,pods                 # what ran / is running
k logs -l app=nse-session --tail=100     # today's session log
k logs -l app=nse-post-session --tail=100
cd /opt/nse-intraday/src && sudo sh deploy/k8s/run-once.sh python -m app.integrated status
sudo sh deploy/k8s/run-once.sh python -m app.monitoring status
```

**Dashboard: https://intraday.sidestream.in** (user `nse`; the password is in
`~/.nse-dashboard-credentials` on the Mac that deployed it, mode 600, and only
its bcrypt hash is on the server). Caddy site block in `/etc/caddy/Caddyfile`
(backed up before the change):

```
intraday.sidestream.in {
	basic_auth { nse <bcrypt hash> }
	header { Strict-Transport-Security "max-age=31536000"; X-Robots-Tag "noindex, nofollow"; X-Frame-Options "DENY"; Referrer-Policy "no-referrer"; -Server }
	reverse_proxy 10.43.71.225:8501
}
```

Change the password: run `caddy hash-password` on the server (it reads the new
password from the prompt), replace the hash in the Caddyfile, then
`sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`.
Remove public access: delete that site block and reload Caddy, then delete the
Route 53 record `intraday.sidestream.in`.

**Without the public URL (SSH tunnel).** On the server, run
`sudo k3s kubectl -n nse-intraday port-forward svc/dashboard 8501:8501`.
On your Mac, run
`ssh -i ~/.ssh/sidestream-keypair.pem -L 8501:localhost:8501 ubuntu@3.111.119.69`,
then open http://localhost:8501. SSH is allowed only from the IP in
`sidestream-app-sg`. Update that rule if your IP changes.

## Deploy a new version

```bash
cd /opt/nse-intraday/src && git pull
T=$(git rev-parse --short HEAD)
nice -n 10 docker build -f deploy/Dockerfile -t nse-intraday:$T .
docker save nse-intraday:$T | sudo k3s ctr images import -
sed "s/IMAGE_TAG/$T/g" deploy/k8s/nse-intraday.yaml | sudo k3s kubectl apply -f -
```

Do not change the strategy, prompt, model or universe during the evaluation.
Such changes are new versions: see `docs/experiments.md`. Deploy between
sessions; never during 09:15–15:30 IST.

## Operations

- **Pause the schedules:** `k patch cronjob session -p '{"spec":{"suspend":true}}'`.
  Do the same for `post-session`; set `false` to resume.
- **Stop a running session safely:** `k delete pod -l app=nse-session`. It
  receives SIGTERM and stops between steps; positions are kept. The Job
  retries and resumes.
- **Rotate secrets:** edit `.env` locally, then:
  `scp` it to `/tmp/nse.env`, run
  `k create secret generic nse-env --from-file=env=/tmp/nse.env --dry-run=client -o yaml | sudo k3s kubectl apply -f -`,
  then `shred -u /tmp/nse.env`, then `k rollout restart deploy/dashboard`.
- **Backups:** the post-session job writes them to the PVC. Copy them off the
  machine periodically, for example
  `k cp <pod>:/app/data/backups ./backups` from a run-once pod, or snapshot
  the EBS volume.
- **Resource check:** `sudo k3s kubectl top pod -n nse-intraday`;
  `k describe resourcequota`.

## Remove everything

`k delete namespace nse-intraday` removes the pods, jobs, data volume and
secret. **Back up `/app/data` first.** Then `sudo /usr/local/bin/k3s-uninstall.sh`
removes k3s. Docker and the video platform are separate and unaffected.

## Known limitations

- Single node, shared with another production-like workload. The quota and
  limits protect both, but a host outage stops both.
- No external alert channel: alerts are in the monitoring DB, the logs and
  `data/monitoring/alerts.jsonl` on the PVC.
- The first real market-hours session (2026-10-21) is the first live test of
  Groww data during trading hours from this host.
