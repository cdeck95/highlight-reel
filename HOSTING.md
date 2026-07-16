# Hosting Plan

Notes for deploying this app so others can use it over the network.

---

## Current state

The app runs fine locally with `python3 server.py`. Flask's built-in server is used, which is single-threaded and not intended for multi-user traffic. All state (`_uploads`, `_jobs` dicts) lives in memory, and files are stored on the local filesystem.

---

## Changes needed for shared/hosted deployment

### 1. Replace the dev server with gunicorn

Flask's built-in server is not safe for production traffic.

```bash
pip install gunicorn
gunicorn -w 1 -b 0.0.0.0:5000 --timeout 300 server:app
```

**Keep `-w 1` (single worker).** The in-memory `_uploads` and `_jobs` dicts are not shared across processes. If multiple workers are ever needed, job state must be moved to Redis or a SQLite database first.

---

### 2. Add upload file size limit

Without a limit, any client can upload an arbitrarily large file. Add to `server.py`:

```python
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB
```

---

### 3. Temp file cleanup

`uploads/` and `outputs/` grow indefinitely. Add a cleanup strategy:

**Option A — background thread in server.py** (run at startup, delete files older than 24 h):

```python
import threading, time

def _cleanup_loop():
    while True:
        time.sleep(3600)
        cutoff = time.time() - 86400
        for folder in ("uploads", "outputs"):
            for f in Path(folder).iterdir():
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)

threading.Thread(target=_cleanup_loop, daemon=True).start()
```

**Option B — cron job on the host:**

```cron
0 * * * * find /path/to/uploads /path/to/outputs -mtime +1 -delete
```

---

### 4. Put nginx in front (HTTPS + large upload support)

nginx handles TLS termination, buffers large file uploads, and can serve the `outputs/` directory directly without going through Python.

Minimal nginx config:

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    client_max_body_size 4g;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # Serve completed montages directly (bypasses gunicorn for large downloads)
    location /download/ {
        alias /path/to/outputs/;
        add_header Content-Disposition 'attachment';
    }
}
```

Get a free TLS cert with [certbot](https://certbot.eff.org/):

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

---

### 5. Authentication

Currently any client that reaches the server can upload files and consume CPU/disk. Options in order of effort:

- **nginx Basic Auth** — quick, good enough for a small private group
  ```bash
  sudo htpasswd -c /etc/nginx/.htpasswd username
  ```
  Add to the nginx `server` block: `auth_basic "Goals"; auth_basic_user_file /etc/nginx/.htpasswd;`
- **Secret URL** — share a long random path prefix (e.g. `/xK9m2p/`) and mount the app there
- **OAuth / SSO** — overkill for internal use

---

### 6. FFmpeg on the server

On a Linux VPS (Ubuntu/Debian):

```bash
sudo apt update && sudo apt install ffmpeg
ffmpeg -version  # verify
```

---

### 7. Disk space

GoPro footage is typically 3–8 GB per video. Each montage job also creates intermediate clips. A $5–10/mo VPS has ~25–50 GB — tight for heavy use. Options:

- Mount a separate storage volume for `uploads/` and `outputs/`
- Use aggressive cleanup (option in §3 above) to delete source files once montage is done
- Move to object storage (S3/R2) for long-term output storage — requires refactoring the download route

---

## Suggested deployment stack (small private group)

| Layer         | Choice                                              |
| ------------- | --------------------------------------------------- |
| VPS           | Any $10/mo Linux VPS (DigitalOcean, Hetzner, Vultr) |
| OS            | Ubuntu 24.04 LTS                                    |
| App server    | gunicorn (`-w 1 --timeout 300`)                     |
| Reverse proxy | nginx                                               |
| TLS           | Let's Encrypt via certbot                           |
| Auth          | nginx Basic Auth                                    |
| Cleanup       | Hourly cron, delete files older than 24 h           |

---

## Not needed (for small internal use)

- Task queue (Celery/Redis) — only needed if you want multiple concurrent montage jobs or multiple gunicorn workers
- Database — only needed if you want job persistence across server restarts
- Cloud storage — only needed if local disk is insufficient
