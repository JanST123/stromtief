# Stromtief — Gemini & Agent Architecture Guide

This document captures the project architecture, deployment setup, configuration, and operational learnings for Stromtief.

---

## 1. Project Overview

**Stromtief** ([stromtief.de](https://stromtief.de) / [app.stromtief.de](https://app.stromtief.de)) is an open-source, mobile-friendly smart charging planner for electric vehicles (EVs) utilizing dynamic electricity tariffs (e.g., Ostrom, EPEX Spot day-ahead prices) powered by the [Akkudoktor EOS](https://github.com/Akkudoktor-EOS/EOS) engine.

- **Repository**: [https://github.com/JanST123/stromtief](https://github.com/JanST123/stromtief)
- **Local Workspace**: `/Users/jan/dev/eos-smart-charge-ui`
- **License**: MIT

---

## 2. Infrastructure & Production Environment

- **Server IP**: `185.241.34.36` (Debian 13 Trixie)
- **SSH Config**: `Host stromtief.de` (`ssh root@stromtief.de`)
- **System Specs**: 1 vCPU, 1 GB RAM, 2 GB Swap (`/swapfile`)
- **Webserver / Reverse Proxy**: Caddy (managed via systemd, auto Let's Encrypt SSL)
- **Container Runtime**: Docker CE & Docker Compose plugin

### Directory Layout on Server
- `/opt/stromtief/`: Git checkout of repository (`docker-compose.yml`, `app.py`, `Dockerfile`, etc.)
- `/var/www/stromtief/`: Static landing page & legal pages served by Caddy for `stromtief.de`
- `/etc/caddy/Caddyfile`: Caddy configuration

---

## 3. Architecture & Security

### Multi-Tenant Isolation & EOS Backend
- EOS is designed as a single-user home energy management system.
- To safely expose it publicly without multi-tenant leakage:
  1. The EOS container (`akkudoktor/eos:latest`) runs inside an isolated internal Docker bridge network (`stromtief-net`) with **no exposed host ports**.
  2. The Stromtief web UI (`app.py`) runs with environment variable `READ_ONLY_MODE=true`.
  3. In `READ_ONLY_MODE`:
     - EOS API is only used as a read-only price feed aggregator (`GET /v1/prediction/series`).
     - User charging plans, battery profiles, and target SoC are calculated locally/ephemerally in-memory or in the browser.
     - Dangerous endpoints (`/v1/config`, `/v1/optimization`) are blocked or kept internal.
  4. The UI container port `8505` is bound strictly to `127.0.0.1:8505`.

### Automatic EOS Initialization & Periodic Price Updater
- Fresh EOS containers lack default price prediction settings.
- `app.py` contains `ensure_eos_initialized()`: on startup, if no electricity price provider is active, it automatically configures `ElecPriceEnergyCharts` for region `DE-LU` via `PUT /v1/config` and triggers `POST /v1/prediction/update`.
- **Periodic Background Updater (`periodic_price_updater_loop`)**: Akkudoktor EOS only pulls day-ahead prices when EMS is running or when `POST /v1/prediction/update` is explicitly triggered. A background worker thread runs every 20 minutes to call EOS `/v1/prediction/update` and fetch the newest prices into the cache. In addition, `fetch_eos_prices()` proactively triggers an update whenever remaining future prediction slots drop below 8 hours.

### Caddy Reverse Proxy & Routing
- `https://stromtief.de`: Serves `/var/www/stromtief/` with clean URL rewrites:
  ```caddyfile
  stromtief.de, www.stromtief.de {
      root * /var/www/stromtief
      encode gzip zstd
      file_server
      try_files {path} {path}.html {path}/index.html
  }
  ```
- `https://app.stromtief.de`: Reverse proxies to `127.0.0.1:8505`.

---

## 4. Key Configurations & Environment

### Deployment Variables (`docker-compose.yml`)
```yaml
services:
  eos:
    image: akkudoktor/eos:latest
    restart: unless-stopped
    volumes:
      - ./data/eos:/data
    networks:
      - stromtief-net

  ui:
    build: .
    restart: unless-stopped
    ports:
      - "127.0.0.1:8505:8505"
    environment:
      - EOS_API_URL=http://eos:8503
      - READ_ONLY_MODE=true
      - PORT=8505
    networks:
      - stromtief-net
```

### Ostrom Referral & Tariffs
- **Referral Link**: `https://join.ostrom.de?referralCode=JANS2MXWNE`
- **Referral Code**: `JANS2MXWNE`
- **Default Ostrom Add-on Fee**: 17.0 ct/kWh netto (calculated with 19% VAT)

### Analytics & Privacy
- **Analytics**: Cookieless Matomo tracker hosted on `https://piwik.jan8.de/` (Site ID: `20`).
- No cookies are set (`_paq.push(['disableCookies']);`).
- Legal compliance: Impressum (`/impressum`), Datenschutz (`/datenschutz`), and Haftungsausschluss (`/haftungsausschluss`).

### Progressive Web App (PWA)
- Supported on mobile and desktop via `manifest.webmanifest` and Service Worker (`sw.js`).
- Icons: 192x192, 512x512, and maskable SVG/PNG in `/static/`.
- Native install banner integration (`beforeinstallprompt`).

---

## 5. Deployment & Maintenance Cheat Sheet

### Deploying Changes to Production
From local machine:
```bash
git add .
git commit -m "Description of changes"
git push origin main

# Deploy on VPS:
ssh stromtief.de "cd /opt/stromtief && git pull && docker compose up -d --build && cp -r landing/* /var/www/stromtief/ && systemctl reload caddy"
```

### Checking Production Logs & Status
```bash
ssh stromtief.de
cd /opt/stromtief
docker compose ps
docker compose logs -f ui
docker compose logs -f eos
journalctl -u caddy -n 50 --no-pager
```

---

## 6. WordPress Blog Cross-References
Articles published on `janpedia.de` via REST API:
- **German Article**: Post ID 759 (`https://janpedia.de/?p=759`)
- **English Article**: Post ID 763 (`https://janpedia.de/en/?p=763`)
- Linked via Polylang translation group.
