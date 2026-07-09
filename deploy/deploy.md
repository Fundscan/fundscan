# FundScan — Fresh Droplet Deployment

Target: Ubuntu 22.04 LTS on DigitalOcean (1GB RAM minimum, 2GB recommended).

## 1. Initial server setup

```bash
# As root on the droplet:
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip git sqlite3 curl

# Install Caddy
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# Create app user (no login shell, no sudo)
useradd --system --no-create-home --shell /usr/sbin/nologin fundscan
mkdir -p /opt/fundscan /var/lib/fundscan /var/backups/fundscan /var/log/caddy
chown fundscan:fundscan /var/lib/fundscan /var/backups/fundscan
```

## 2. Deploy application

```bash
cd /opt/fundscan
git clone https://github.com/yourrepo/fundscan.git .

python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env          # fill in all values
chown root:fundscan .env
chmod 640 .env     # fundscan user can read, not world-readable

chown -R fundscan:fundscan /opt/fundscan
```

## 3. systemd service

```bash
cp deploy/fundscan.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable fundscan
systemctl start fundscan
systemctl status fundscan    # should show active (running)
journalctl -u fundscan -f    # tail logs
```

## 4. Caddy (HTTPS)

Point your domain's A record to the droplet IP first, then:

```bash
# Edit domain in Caddyfile
nano deploy/Caddyfile        # replace fundscan.io with your domain
cp deploy/Caddyfile /etc/caddy/Caddyfile
systemctl enable caddy
systemctl restart caddy
# Caddy auto-provisions Let's Encrypt cert on first request
```

## 5. Backfill historical data

```bash
sudo -u fundscan /opt/fundscan/venv/bin/python -m scripts.backfill
```

## 6. Nightly backup cron

```bash
chmod +x /opt/fundscan/deploy/backup.sh
# Add to root crontab (crontab -e):
0 2 * * * /opt/fundscan/deploy/backup.sh >> /var/log/fundscan-backup.log 2>&1
```

## 7. Verify

```bash
curl http://localhost:8000/health
curl http://localhost:8000/rates | python3 -m json.tool | head -40
```

## SQLite → Postgres migration (when load justifies it)

1. `apt install -y postgresql postgresql-contrib`
2. Create DB: `sudo -u postgres createdb fundscan`
3. In `fundscan/db.py`: swap `sqlite3.connect` for `psycopg2.connect`, change `?` placeholders to `%s`, change `INTEGER PRIMARY KEY` to `SERIAL PRIMARY KEY`, change `TEXT` timestamps to `TIMESTAMPTZ`.
4. `pgloader fundscan.db postgresql:///fundscan` for a one-shot migration.
5. Update `DB_PATH` → `DATABASE_URL` in `.env`.

The schema was written to be identical between SQLite and Postgres — no SQLite-isms were used.

## Firewall

```bash
ufw allow 22/tcp     # SSH
ufw allow 80/tcp     # HTTP (Caddy redirects to HTTPS)
ufw allow 443/tcp    # HTTPS
ufw enable
```
