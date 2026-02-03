# Deploy X Bot on your VPS (Nginx)

Deploy the dashboard behind Nginx with Gunicorn and run the bot daily via server cron.

---

## 1. On the server (e.g. Ubuntu/Debian)

```bash
# Clone or upload the project
cd /var/www  # or your preferred path
git clone https://github.com/YOUR_USER/tweetpy.git
cd tweetpy

# Python venv and dependencies
python3 -venv venv
source venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
```

---

## 2. Environment variables

Create a `.env` in the project root (or export in the systemd unit):

```bash
FLASK_SECRET_KEY=your-long-random-secret
CRON_SECRET=another-long-random-secret
DB_HOST=localhost
DB_PORT=3306
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_NAME=twitter
# Optional: X API and Gemini (or set in dashboard after first login)
# X_CONSUMER_KEY=...
# X_CONSUMER_SECRET=...
# X_ACCESS_TOKEN=...
# X_ACCESS_TOKEN_SECRET=...
# X_BEARER_TOKEN=...
# GEMINI_API_KEY=...
```

---

## 3. Run the app with Gunicorn

Run Gunicorn bound to localhost (Nginx will proxy to it):

```bash
gunicorn -w 1 -b 127.0.0.1:5001 --timeout 120 dashboard:app
```

- `-w 1` – one worker (enough for the dashboard and cron; bot runs in-thread).
- `5001` – port the app listens on; Nginx will use this.

---

## 4. Nginx reverse proxy

Add a server block (or location inside an existing server). Replace `your-domain.com` and paths as needed:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 120s;
        proxy_read_timeout 120s;
    }
}
```

For HTTPS, use Certbot (Let’s Encrypt) or your SSL config as usual.

Reload Nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## 5. Keep the app running (systemd)

Create `/etc/systemd/system/tweetpy.service`:

```ini
[Unit]
Description=X Bot Dashboard (Gunicorn)
After=network.target mysql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/tweetpy
Environment="PATH=/var/www/tweetpy/venv/bin"
EnvironmentFile=/var/www/tweetpy/.env
ExecStart=/var/www/tweetpy/venv/bin/gunicorn -w 1 -b 127.0.0.1:5001 --timeout 120 dashboard:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Adjust `User`, `Group`, and `WorkingDirectory` to match your server.

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tweetpy
sudo systemctl start tweetpy
sudo systemctl status tweetpy
```

---

## 6. Run the bot daily (cron)

Use the server’s cron so the bot runs once per day:

```bash
crontab -e
```

Add a line (run once daily at 9:00 AM server time; use your actual `CRON_SECRET`):

```
0 9 * * * curl -s "http://127.0.0.1:5001/api/run-once?secret=YOUR_CRON_SECRET" > /dev/null 2>&1
```

Or with the public URL:

```
0 9 * * * curl -s "https://your-domain.com/api/run-once?secret=YOUR_CRON_SECRET" > /dev/null 2>&1
```

---

## 7. First-time setup

- Open `https://your-domain.com` (or `http://` if not using SSL), log in with your allowed email.
- In the dashboard, set **Credentials** (X API, optional Gemini) and **Keywords** if not set via env.

The bot will run once per day at the time defined in crontab.

---

## Security

- **CRON_SECRET:** Keep it long and random; anyone with this URL can trigger a run. Don’t commit it to the repo.
- **FLASK_SECRET_KEY:** Required for sessions; use a different long random string.
- **Dashboard login:** Only the allowed email can log in; credentials are stored in the DB and (if present) `config.json`.
