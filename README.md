# ⚡️ EOS Smart Charge Planner

> **Smart EV charging optimization with dynamic electricity tariffs (e.g. Ostrom, Tibber, EPEX Spot) powered by [Akkudoktor EOS](https://github.com/Akkudoktor-EOS/EOS).**
>
> Designed for local self-hosting **or** as a secure, multi-tenant online service for multiple users.

The **EOS Smart Charge Planner** provides a clean, modern, and responsive web interface that answers key questions for EV owners:
1. **When to plug in**: Schedule charging so the vehicle reaches the desired state-of-charge (SoC) before your departure deadline at the lowest market electricity rates.
2. **Transparent total costs**: Clear breakdown of day-ahead spot market prices, local grid fees, and value-added tax (VAT).
3. **Savings estimation**: Immediate comparison showing how much money is saved compared to unmanaged charging.

---

## 🛡️ Architecture & Multi-User Privacy

Akkudoktor EOS is designed as a single-user system without built-in authentication. The **EOS Smart Charge Planner** wraps it into a safe public service:

* **Central Spot Price Feed**: A single EOS instance fetches EPEX Spot day-ahead auction data for the bidding zone (e.g., DE-LU, AT) and keeps it in memory with an automatic cache.
* **100% Privacy via LocalStorage**: Vehicle parameters (battery capacity, charging power, SoC), custom 24-hour hourly grid fees, and tax rates are stored **exclusively in the user's browser**.
* **Zero User Database Needed**: Fully GDPR-compliant, no registration, no passwords, and no server-side tracking cookies.
* **Read-Only Protection Shield (`READ_ONLY_MODE=true`)**:
  * Write actions (`PUT /v1/config`, etc.) are blocked.
  * In Docker, EOS runs in an isolated internal network without exposed public ports.
* **Cross-Device Sync (QR Code & Link)**:
  * Export your settings as a URL hash (`#cfg=...`).
  * Hash fragments are never sent to HTTP servers in access logs.
  * Integrated QR code generator auto-detects local LAN IP for seamless mobile transfer.

```mermaid
flowchart LR
    User((Browser / Mobile)) -->|Public HTTPS / Port 8505| UI[Smart Charge UI / Proxy]
    UI -->|Internal Docker Network only: Port 8503| EOS[(Akkudoktor EOS - Read Only)]
    style EOS fill:#1e293b,stroke:#ef4444,stroke-width:2px
    style UI fill:#0f172a,stroke:#10b981,stroke-width:2px
```

---

## ✨ Key Features

* **⚡️ Spot Prices & 48-Hour Forecast**: Direct integration with day-ahead electricity prices.
* **💶 Hourly Grid Fees (0–23h)**:
  * Configure fixed or time-variable network fees (e.g., German § 14a EnWG module 3, peak/off-peak tariffs).
  * Quick presets for common tariffs.
* **🧾 VAT on Total Price**:
  * Configurable tax rate (e.g., 19% DE, 20% AT) applied on top of the spot price plus fixed grid charges.
* **📊 Multi-Day Interactive Price Chart**:
  * Smooth horizontal scrolling across 48+ hours (supports mouse wheel horizontal drag).
  * View modes: `Stacked` (spot price + fees), `Total Price`, and `Spot Market Only`.
  * Interactive tooltips with exact breakdowns per 15-minute slot.
  * Day transition separators and quick-jump buttons (`Now`, `To Window`, `Tomorrow`).
* **🚗 EV Presets & Charging Strategies**:
  * Vehicle presets for common models (Kia EV3, Tesla Model 3, etc.).
  * Sliders for current SoC, target SoC, and charging speed (3.7 – 22 kW).
  * Deadline presets (*Tomorrow 07:00*, *Tomorrow 08:00*, *in 8h*, *in 12h*, etc.).
  * Strategy selection: *Continuous window* vs. *Smart split hours* (cheapest individual slots).

---

## 🚀 Quick Start

### Option 1: Local Python Setup

```bash
git clone https://github.com/YOUR-USER/eos-smart-charge-ui.git
cd eos-smart-charge-ui

# Install dependencies
pip install -r requirements.txt

# Start application (Default: Port 8505, EOS at http://127.0.0.1:8503)
./start.sh
```

Open **[http://localhost:8505](http://localhost:8505)** in your web browser.

### Option 2: Docker Compose (Recommended for Production)

Launches EOS internally and exposes only the Smart Charge UI:

```bash
docker compose up -d
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|---|---|---|
| `READ_ONLY_MODE` | `true` | Enforces read-only behavior for EOS and activates client-side LocalStorage. |
| `EOS_API_URL` | `http://127.0.0.1:8503` | Address of the Akkudoktor EOS backend server. |
| `SMART_CHARGE_PORT` | `8505` | Listening port for the web service. |
| `ADMIN_TOKEN` | *empty* | Optional authorization token for server-side config modifications. |

---

## 🛠 REST API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Responsive Single Page Application |
| `GET` | `/api/system` | System status, read-only mode, and EOS connectivity |
| `GET` | `/api/prices` | 15-minute price series with gross/net calculations (cached) |
| `POST` | `/api/plan` | Evaluates optimal charging schedule in memory |
| `GET` | `/api/config/provider` | Retrieves default provider information |
| `POST` | `/api/config/provider` | *(Blocked in read-only mode)* Updates server configuration |

---

## 📄 License

This project is open-source under the terms of the [MIT License](LICENSE).
Built on top of the open-source energy management system [Akkudoktor EOS](https://github.com/Akkudoktor-EOS/EOS).

