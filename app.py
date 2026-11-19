#!/usr/bin/env python3
"""EOS Smart Charge Planner.

A sleek, intuitive web application for electric vehicle owners with dynamic electricity tariffs
(e.g., Ostrom SimplyDynamic, Tibber, EPEX Spot). Communicates with Akkudoktor EOS API
as a read-only price feed, models hourly grid fees, VAT, battery capacity,
and calculates the cheapest continuous or split charging windows before your deadline.

Supports Multi-User & Public SaaS operation with client-side LocalStorage,
server-side Read-Only protection, and seamless Cross-Device Settings Sync via QR Code & Link.
"""

from datetime import datetime, timedelta
import json
import os
import socket
import time
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
import urllib.error

import uvicorn
from fastapi import FastAPI, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Configuration & Security
READ_ONLY_MODE = os.getenv("READ_ONLY_MODE", "true").lower() in ("true", "1", "yes")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DEFAULT_EOS_URL = os.getenv("EOS_API_URL", "http://127.0.0.1:8503").rstrip("/")
PORT = int(os.getenv("SMART_CHARGE_PORT", "8505"))
SETTINGS_FILE = os.getenv(
    "SMART_CHARGE_SETTINGS",
    os.path.join(os.path.dirname(__file__), "data", "settings.json"),
)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

DEFAULT_SETTINGS = {
    "eos_url": DEFAULT_EOS_URL,
    "provider": "ElecPriceEnergyCharts",
    "bidding_zone": "DE-LU",
    "vat_percent": 19.0,
    "hourly_fees": [17.0] * 24,  # Net fixed fee default: 17 ct/kWh (e.g. Ostrom Standard)
}

# In-memory Price Cache to protect EOS from multi-user load spikes
_PRICE_CACHE: Dict[str, float] = {}
_PRICE_CACHE_TIME: float = 0.0
CACHE_TTL_SECONDS = 180  # 3 minutes


def get_local_ip() -> str:
    """Detect primary local network IP (for convenient Wi-Fi QR code scanning)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def load_settings() -> Dict[str, Any]:
    """Load settings from JSON file or create with defaults."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                fees = data.get("hourly_fees", [17.0] * 24)
                if len(fees) != 24:
                    fees = (fees + [17.0] * 24)[:24]
                return {
                    "eos_url": data.get("eos_url", DEFAULT_EOS_URL).rstrip("/"),
                    "provider": data.get("provider", "ElecPriceEnergyCharts"),
                    "bidding_zone": data.get("bidding_zone", "DE-LU"),
                    "vat_percent": float(data.get("vat_percent", 19.0)),
                    "hourly_fees": [float(x) for x in fees],
                }
        except Exception as e:
            print(f"[Warning] Failed to load settings from {SETTINGS_FILE}: {e}")
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: Dict[str, Any]) -> None:
    """Save settings to JSON file."""
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


app = FastAPI(title="EOS Smart Charge Planner")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_active_eos_url() -> str:
    """Get currently configured EOS API URL."""
    settings = load_settings()
    return settings.get("eos_url", DEFAULT_EOS_URL).rstrip("/")


def ensure_eos_initialized(eos_url: Optional[str] = None) -> None:
    """Ensure EOS has an active electricity price provider configured on cold start."""
    base_url = (eos_url or get_active_eos_url()).rstrip("/")
    try:
        req = Request(f"{base_url}/v1/config", headers={"User-Agent": "SmartChargePlanner/1.0"})
        with urlopen(req, timeout=4) as response:
            cfg = json.loads(response.read().decode("utf-8"))
            current_provider = cfg.get("elecprice", {}).get("provider")
            if not current_provider:
                settings = load_settings()
                provider = settings.get("provider", "ElecPriceEnergyCharts")
                bidding_zone = settings.get("bidding_zone", "DE-LU")
                config_body = {"elecprice": {"provider": provider}}
                if provider == "ElecPriceEnergyCharts":
                    config_body["elecprice"]["energycharts"] = {"bidding_zone": bidding_zone}
                put_req = Request(
                    f"{base_url}/v1/config",
                    data=json.dumps(config_body).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "SmartChargePlanner/1.0"},
                    method="PUT",
                )
                with urlopen(put_req, timeout=5):
                    pass
                update_req = Request(
                    f"{base_url}/v1/prediction/update",
                    data=b"{}",
                    headers={"Content-Type": "application/json", "User-Agent": "SmartChargePlanner/1.0"},
                    method="POST",
                )
                with urlopen(update_req, timeout=10):
                    pass
    except Exception as e:
        print(f"[Info] EOS initialization check: {e}")


def fetch_eos_prices(eos_url: Optional[str] = None, force_refresh: bool = False) -> Dict[str, float]:
    """Fetch current price predictions with in-memory caching."""
    global _PRICE_CACHE, _PRICE_CACHE_TIME
    now = time.time()

    if not force_refresh and _PRICE_CACHE and (now - _PRICE_CACHE_TIME < CACHE_TTL_SECONDS):
        return _PRICE_CACHE

    base_url = (eos_url or get_active_eos_url()).rstrip("/")
    url = f"{base_url}/v1/prediction/series?key=elecprice_marketprice_wh"
    req = Request(url, headers={"User-Agent": "SmartChargePlanner/1.0"})
    try:
        with urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            prices = data.get("data", {})
            if prices:
                _PRICE_CACHE = prices
                _PRICE_CACHE_TIME = now
            elif not _PRICE_CACHE:
                ensure_eos_initialized(base_url)
            return prices
    except urllib.error.HTTPError as e:
        if e.code == 404 and not _PRICE_CACHE:
            ensure_eos_initialized(base_url)
        return _PRICE_CACHE if _PRICE_CACHE else {}
    except Exception as e:
        print(f"[Warning] Failed to fetch prices from EOS ({url}): {e}")
        return _PRICE_CACHE if _PRICE_CACHE else {}


def update_eos_provider(provider: str, bidding_zone: str = "DE-LU", eos_url: Optional[str] = None) -> bool:
    """Update EOS config with new electricity price provider."""
    if READ_ONLY_MODE:
        return False

    base_url = (eos_url or get_active_eos_url()).rstrip("/")
    try:
        config_body: Dict[str, Any] = {"elecprice": {"provider": provider}}
        if provider == "ElecPriceEnergyCharts":
            config_body["elecprice"]["energycharts"] = {"bidding_zone": bidding_zone}
        elif provider == "ElecPriceSMARD":
            config_body["elecprice"]["smard"] = {"filter_id": 4169, "region": "DE"}

        put_req = Request(
            f"{base_url}/v1/config",
            data=json.dumps(config_body).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "SmartChargePlanner/1.0"},
            method="PUT",
        )
        with urlopen(put_req, timeout=5) as response:
            pass

        # Trigger prediction update
        update_req = Request(
            f"{base_url}/v1/prediction/update",
            data=b"{}",
            headers={"Content-Type": "application/json", "User-Agent": "SmartChargePlanner/1.0"},
            method="POST",
        )
        with urlopen(update_req, timeout=5) as response:
            pass

        fetch_eos_prices(force_refresh=True)
        return True
    except Exception as e:
        print(f"[Warning] Failed to update EOS provider on {base_url}: {e}")
        return False


class TestEosRequest(BaseModel):
    eos_url: str = Field(default="http://127.0.0.1:8503")


class ProviderConfigRequest(BaseModel):
    eos_url: Optional[str] = Field(default=None)
    provider: str = Field(default="ElecPriceEnergyCharts")
    bidding_zone: str = Field(default="DE-LU")
    vat_percent: float = Field(default=19.0, ge=0.0, le=50.0)
    hourly_fees: List[float] = Field(default_factory=lambda: [17.0] * 24)


class CalcRequest(BaseModel):
    battery_capacity_kwh: float = Field(default=53.0, ge=5.0, le=200.0)
    current_soc: float = Field(default=25.0, ge=0.0, le=100.0)
    target_soc: float = Field(default=80.0, ge=0.0, le=100.0)
    charge_power_kw: float = Field(default=11.0, ge=1.0, le=50.0)
    efficiency_percent: float = Field(default=90.0, ge=70.0, le=100.0)
    deadline_iso: Optional[str] = None
    strategy: str = Field(default="continuous")  # 'continuous' or 'split'
    vat_percent: Optional[float] = Field(default=19.0)
    hourly_fees: Optional[List[float]] = None
    eos_url: Optional[str] = None


@app.get("/manifest.webmanifest")
def get_manifest():
    manifest_path = os.path.join(STATIC_DIR, "manifest.webmanifest")
    return FileResponse(manifest_path, media_type="application/manifest+json")


@app.get("/sw.js")
def get_service_worker():
    sw_path = os.path.join(STATIC_DIR, "sw.js")
    return FileResponse(
        sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/api/system")
def get_system_info():
    """Get system mode, read-only status, detected local IP, and EOS connection status."""
    eos_url = get_active_eos_url()
    eos_online = bool(fetch_eos_prices())
    local_ip = get_local_ip()
    return {
        "status": "ok",
        "read_only": READ_ONLY_MODE,
        "eos_online": eos_online,
        "eos_url": eos_url if not READ_ONLY_MODE else "[Geschützt]",
        "cached_slots": len(_PRICE_CACHE),
        "local_ip": local_ip,
        "port": PORT,
    }


@app.post("/api/eos/test")
def test_eos_connection(req: TestEosRequest):
    """Test connection to a specified EOS server endpoint."""
    if READ_ONLY_MODE:
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "Server läuft im sicheren Read-Only-Modus. EOS-Endpunkt ist fest konfiguriert."},
        )

    url = req.eos_url.rstrip("/")
    try:
        check_url = f"{url}/v1/config"
        test_req = Request(check_url, headers={"User-Agent": "SmartChargePlanner/1.0"})
        with urlopen(test_req, timeout=4) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                version = data.get("general", {}).get("version", "aktiv")
                return {"ok": True, "version": version, "message": f"EOS Server erreichbar (v{version})"}
            return {"ok": False, "error": f"HTTP {response.status}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/config/provider")
def get_provider_config():
    """Get default provider, EOS endpoint, VAT, and hourly fees configuration."""
    settings = load_settings()
    available_providers = [
        {
            "id": "ElecPriceEnergyCharts",
            "name": "Energy-Charts (EPEX Spot / Ostrom)",
            "description": "Börsenpreise aus Deutschland & Europa. Standard für Ostrom & Tibber.",
            "bidding_zones": ["DE-LU", "AT", "CH", "FR", "NL"],
        },
        {
            "id": "ElecPriceSMARD",
            "name": "SMARD (Bundesnetzagentur)",
            "description": "Offizielle deutsche Strommarkt-Daten der Bundesnetzagentur.",
            "bidding_zones": ["DE"],
        },
        {
            "id": "ElecPriceAkkudoktor",
            "name": "Akkudoktor Community Server",
            "description": "Preisvorhersagen der Akkudoktor Community.",
            "bidding_zones": ["DE"],
        },
    ]
    return {
        "status": "ok",
        "read_only": READ_ONLY_MODE,
        "settings": settings,
        "available_providers": available_providers,
    }


@app.post("/api/config/provider")
def set_provider_config(req: ProviderConfigRequest, x_admin_token: Optional[str] = Header(None)):
    """Save provider, EOS endpoint, VAT, and 24h hourly fee configuration."""
    if READ_ONLY_MODE:
        if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Der Server läuft im sicheren Read-Only-Modus. Ihre Tarifeinstellungen wurden automatisch in Ihrem Browser (LocalStorage) gespeichert.",
                    "read_only": True,
                },
            )

    current = load_settings()
    new_eos_url = (req.eos_url or current.get("eos_url", DEFAULT_EOS_URL)).rstrip("/")

    provider_changed = (
        current.get("provider") != req.provider
        or current.get("bidding_zone") != req.bidding_zone
        or current.get("eos_url") != new_eos_url
    )

    fees = [float(x) for x in req.hourly_fees]
    if len(fees) != 24:
        fees = (fees + [15.0] * 24)[:24]

    new_settings = {
        "eos_url": new_eos_url,
        "provider": req.provider,
        "bidding_zone": req.bidding_zone,
        "vat_percent": float(req.vat_percent),
        "hourly_fees": fees,
    }
    save_settings(new_settings)

    eos_updated = False
    if provider_changed and not READ_ONLY_MODE:
        eos_updated = update_eos_provider(req.provider, req.bidding_zone, eos_url=new_eos_url)

    return {
        "status": "ok",
        "settings": new_settings,
        "provider_changed": provider_changed,
        "eos_updated": eos_updated,
    }


@app.get("/api/prices")
def get_prices(eos_url: Optional[str] = None):
    settings = load_settings()
    active_url = (eos_url or settings.get("eos_url", DEFAULT_EOS_URL)).rstrip("/")
    prices = fetch_eos_prices(active_url)
    fees = settings.get("hourly_fees", [17.0] * 24)
    vat_percent = settings.get("vat_percent", 19.0)
    vat_factor = 1.0 + (vat_percent / 100.0)

    items = []
    for dt_str, val_wh in sorted(prices.items()):
        spot_net = round(val_wh * 100_000.0, 2)
        try:
            dt_obj = datetime.fromisoformat(dt_str)
            hour = dt_obj.hour
        except Exception:
            hour = 0
        fee_net = round(fees[hour], 2)
        total_net = round(spot_net + fee_net, 2)
        total_gross = round(total_net * vat_factor, 2)
        spot_gross = round(spot_net * vat_factor, 2)
        fee_gross = round(fee_net * vat_factor, 2)
        items.append(
            {
                "datetime": dt_str,
                "spot_price_net": spot_net,
                "spot_price_gross": spot_gross,
                "fixed_fee_net": fee_net,
                "fixed_fee_gross": fee_gross,
                "total_price_net": total_net,
                "total_price_gross": total_gross,
                "vat_percent": vat_percent,
            }
        )
    return {"count": len(items), "prices": items}


@app.post("/api/plan")
def calculate_plan(req: CalcRequest):
    """Calculate charging plan purely in memory using cached spot market prices."""
    settings = load_settings()
    active_eos_url = (req.eos_url or settings.get("eos_url", DEFAULT_EOS_URL)).rstrip("/")
    prices_raw = fetch_eos_prices(active_eos_url)
    if not prices_raw:
        return JSONResponse(
            status_code=503,
            content={
                "error": "Keine Strompreisdaten von der EOS-API verfügbar. Bitte prüfen Sie die Verbindung des Servers zu EOS."
            },
        )

    # Use user-supplied hourly fees & VAT (from LocalStorage) with server fallback
    fees = req.hourly_fees if req.hourly_fees and len(req.hourly_fees) == 24 else settings["hourly_fees"]
    vat_percent = req.vat_percent if req.vat_percent is not None else settings.get("vat_percent", 19.0)
    vat_factor = 1.0 + (vat_percent / 100.0)

    now_dt = datetime.now().astimezone()
    sorted_items = []
    for dt_str, val_wh in sorted(prices_raw.items()):
        spot_net = val_wh * 100_000.0
        try:
            slot_dt = datetime.fromisoformat(dt_str)
        except Exception:
            continue
        hour = slot_dt.hour
        fee_net = fees[hour]
        total_net = spot_net + fee_net

        # Apply VAT to total price (spot + fixed fee)
        total_gross = total_net * vat_factor
        spot_gross = spot_net * vat_factor
        fee_gross = fee_net * vat_factor
        vat_amount = total_gross - total_net

        sorted_items.append(
            {
                "dt": dt_str,
                "dt_obj": slot_dt,
                "spot_price_net": spot_net,
                "fixed_fee_net": fee_net,
                "total_price_net": total_net,
                "spot_price": spot_gross,
                "fixed_fee": fee_gross,
                "total_price": total_gross,
                "vat_amount": vat_amount,
                "price": total_gross,  # Gross price used for consumer ranking & cost
                "val_wh": val_wh,
            }
        )

    # Filter future items (slots starting after now minus 15 min buffer)
    threshold = now_dt - timedelta(minutes=14)
    future_items = [item for item in sorted_items if item["dt_obj"] >= threshold]
    if not future_items:
        future_items = sorted_items[-96:] if len(sorted_items) > 96 else sorted_items

    # Parse deadline if provided
    deadline_dt: Optional[datetime] = None
    if req.deadline_iso:
        try:
            deadline_dt = datetime.fromisoformat(req.deadline_iso)
            if deadline_dt.tzinfo is None:
                deadline_dt = deadline_dt.replace(tzinfo=now_dt.tzinfo)
        except Exception as e:
            print(f"[Warning] Invalid deadline_iso: {e}")

    # Energy calculations
    soc_diff = max(0.0, req.target_soc - req.current_soc)
    energy_battery_kwh = (soc_diff / 100.0) * req.battery_capacity_kwh
    eff_factor = req.efficiency_percent / 100.0
    energy_grid_kwh = energy_battery_kwh / eff_factor

    # Charging duration in hours
    duration_hours = energy_grid_kwh / req.charge_power_kw
    slot_duration = 0.25
    slots_needed = max(1, int(round(duration_hours / slot_duration)))

    warning_msg = None
    eligible_items = future_items
    if deadline_dt:
        before_deadline = [it for it in future_items if it["dt_obj"] < deadline_dt]
        if len(before_deadline) < slots_needed:
            warning_msg = (
                f"Achtung: Die benötigte Ladedauer ({int(duration_hours)}h {int((duration_hours%1)*60):02d}m) "
                f"ist länger als die verbleibende Zeit bis zur gewünschten Abfahrtszeit. "
                "Es muss sofort geladen werden."
            )
            eligible_items = before_deadline if before_deadline else future_items[:slots_needed]
        else:
            eligible_items = before_deadline

    if not eligible_items:
        return JSONResponse(
            status_code=400,
            content={"error": "Keine passenden Preiszeitfenster gefunden."},
        )

    slots_to_select = min(slots_needed, len(eligible_items))

    selected_slots = []
    if req.strategy == "continuous":
        best_avg_price = float("inf")
        best_start_idx = 0
        max_idx = len(eligible_items) - slots_to_select
        for i in range(max_idx + 1):
            window = eligible_items[i : i + slots_to_select]
            window_end = window[-1]["dt_obj"] + timedelta(minutes=15)
            if deadline_dt and window_end > deadline_dt and i > 0:
                continue
            avg_p = sum(s["total_price"] for s in window) / len(window)
            if avg_p < best_avg_price:
                best_avg_price = avg_p
                best_start_idx = i

        selected_slots = eligible_items[best_start_idx : best_start_idx + slots_to_select]
    else:
        sorted_by_price = sorted(enumerate(eligible_items), key=lambda x: x[1]["total_price"])
        chosen_indices = sorted([x[0] for x in sorted_by_price[:slots_to_select]])
        selected_slots = [eligible_items[i] for i in chosen_indices]

    # Costs based on total consumer price (Arbeitspreis brutto)
    avg_total_gross = sum(s["total_price"] for s in selected_slots) / len(selected_slots)
    avg_total_net = sum(s["total_price_net"] for s in selected_slots) / len(selected_slots)
    avg_spot_gross = sum(s["spot_price"] for s in selected_slots) / len(selected_slots)
    avg_fee_gross = sum(s["fixed_fee"] for s in selected_slots) / len(selected_slots)

    total_cost_euro = (avg_total_gross / 100.0) * energy_grid_kwh
    total_net_cost_euro = (avg_total_net / 100.0) * energy_grid_kwh
    vat_cost_euro = total_cost_euro - total_net_cost_euro
    spot_cost_euro = (avg_spot_gross / 100.0) * energy_grid_kwh
    fee_cost_euro = (avg_fee_gross / 100.0) * energy_grid_kwh

    # Comparison with immediate charging
    immediate_slots = future_items[:slots_to_select]
    immediate_avg_gross = sum(s["total_price"] for s in immediate_slots) / len(immediate_slots)
    immediate_cost_euro = (immediate_avg_gross / 100.0) * energy_grid_kwh
    saved_euro_vs_now = max(0.0, immediate_cost_euro - total_cost_euro)

    # Comparison with worst charging window
    worst_avg_price = 0.0
    for i in range(len(eligible_items) - slots_to_select + 1):
        window = eligible_items[i : i + slots_to_select]
        avg_p = sum(s["total_price"] for s in window) / len(window)
        if avg_p > worst_avg_price:
            worst_avg_price = avg_p
    worst_cost_euro = (worst_avg_price / 100.0) * energy_grid_kwh
    saved_euro_vs_worst = max(0.0, worst_cost_euro - total_cost_euro)

    range_km = round((energy_battery_kwh / 16.5) * 100)

    start_dt_obj = selected_slots[0]["dt_obj"]
    end_dt_obj = selected_slots[-1]["dt_obj"] + timedelta(minutes=15)
    seconds_until_start = (start_dt_obj - now_dt).total_seconds()
    if seconds_until_start <= 120:
        countdown_text = "Sofort anstecken!"
        start_soon = True
    else:
        h = int(seconds_until_start // 3600)
        m = int((seconds_until_start % 3600) // 60)
        countdown_text = f"Start in {h}h {m:02d}m"
        start_soon = False

    selected_set = set(s["dt"] for s in selected_slots)
    timeline = []
    # Return all available future slots for full horizontal scroll
    for idx, item in enumerate(future_items):
        is_dl = deadline_dt and abs((item["dt_obj"] - deadline_dt).total_seconds()) < 600
        # Detect day transition (00:00 hour)
        is_day_start = (item["dt_obj"].hour == 0 and item["dt_obj"].minute == 0)
        timeline.append(
            {
                "dt": item["dt"],
                "spot_price": round(item["spot_price"], 2),
                "spot_price_net": round(item["spot_price_net"], 2),
                "fixed_fee": round(item["fixed_fee"], 2),
                "fixed_fee_net": round(item["fixed_fee_net"], 2),
                "total_price": round(item["total_price"], 2),
                "total_price_net": round(item["total_price_net"], 2),
                "vat_amount": round(item["vat_amount"], 2),
                "vat_percent": vat_percent,
                "price": round(item["total_price"], 2),
                "is_charging": item["dt"] in selected_set,
                "is_deadline": bool(is_dl),
                "is_day_start": bool(is_day_start),
            }
        )

    return {
        "energy_grid_kwh": round(energy_grid_kwh, 2),
        "energy_battery_kwh": round(energy_battery_kwh, 2),
        "duration_hours": round(duration_hours, 2),
        "duration_formatted": f"{int(duration_hours)}h {int((duration_hours % 1) * 60):02d}m",
        "start_time": selected_slots[0]["dt"],
        "end_time": end_dt_obj.isoformat(),
        "countdown_text": countdown_text,
        "start_soon": start_soon,
        "total_cost_euro": round(total_cost_euro, 2),
        "total_net_cost_euro": round(total_net_cost_euro, 2),
        "vat_cost_euro": round(vat_cost_euro, 2),
        "vat_percent": vat_percent,
        "spot_cost_euro": round(spot_cost_euro, 2),
        "fee_cost_euro": round(fee_cost_euro, 2),
        "avg_price_cent": round(avg_total_gross, 2),
        "avg_spot_price_cent": round(avg_spot_gross, 2),
        "avg_fee_cent": round(avg_fee_gross, 2),
        "immediate_cost_euro": round(immediate_cost_euro, 2),
        "savings_vs_now_euro": round(saved_euro_vs_now, 2),
        "savings_vs_worst_euro": round(saved_euro_vs_worst, 2),
        "range_km": range_km,
        "warning": warning_msg,
        "slots_count": len(selected_slots),
        "timeline": timeline,
    }


HTML_CONTENT = """<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stromtief — E-Auto Ladeplaner</title>
    <link rel="manifest" href="/manifest.webmanifest">
    <meta name="theme-color" content="#10b981">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Stromtief">
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32x32.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
    <!-- Zero-dependency QR Code Generator -->
    <script src="/static/qrcode.min.js"></script>
    <!-- Matomo -->
    <script>
      var _paq = window._paq = window._paq || [];
      /* tracker methods like "setCustomDimension" should be called before "trackPageView" */
      _paq.push(['trackPageView']);
      _paq.push(['enableLinkTracking']);
      (function() {
        var u="//piwik.jan8.de/";
        _paq.push(['setTrackerUrl', u+'matomo.php']);
        _paq.push(['setSiteId', '20']);
        var d=document, g=d.createElement('script'), s=d.getElementsByTagName('script')[0];
        g.async=true; g.src=u+'matomo.js'; s.parentNode.insertBefore(g,s);
      })();
    </script>
    <!-- End Matomo Code -->
    <style>
        :root {
            --bg: #0b0f17;
            --surface: #141b26;
            --surface-hover: #1c2636;
            --surface-border: #233044;
            --text-primary: #f1f5f9;
            --text-secondary: #94a3b8;
            --brand-green: #10b981;
            --brand-green-glow: rgba(16, 185, 129, 0.28);
            --brand-blue: #38bdf8;
            --brand-purple: #a855f7;
            --brand-purple-glow: rgba(168, 85, 247, 0.25);
            --brand-amber: #f59e0b;
            --brand-red: #ef4444;
            --radius-md: 12px;
            --radius-lg: 18px;
            accent-color: var(--brand-green);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }

        body {
            background-color: var(--bg);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 24px 16px;
            display: flex;
            justify-content: center;
        }

        .container {
            width: 100%;
            max-width: 1100px;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        /* Toast notification */
        #syncToast {
            position: fixed;
            top: 20px;
            left: 50%;
            transform: translateX(-50%) translateY(-100px);
            background: #14231e;
            border: 1px solid var(--brand-green);
            color: #34d399;
            padding: 12px 24px;
            border-radius: 30px;
            font-size: 0.9rem;
            font-weight: 700;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.7);
            z-index: 999999;
            display: flex;
            align-items: center;
            gap: 10px;
            transition: transform 0.4s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.4s;
            opacity: 0;
            pointer-events: none;
        }

        #syncToast.show {
            transform: translateX(-50%) translateY(0);
            opacity: 1;
        }

        /* Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding-bottom: 14px;
            border-bottom: 1px solid var(--surface-border);
            flex-wrap: wrap;
            gap: 12px;
        }

        .title-group {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .logo-icon {
            background: linear-gradient(135deg, var(--brand-green), var(--brand-blue));
            width: 44px;
            height: 44px;
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 22px;
            box-shadow: 0 4px 14px var(--brand-green-glow);
        }

        h1 {
            font-size: 1.45rem;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .subtitle {
            font-size: 0.85rem;
            color: var(--text-secondary);
        }

        .header-actions {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .config-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--surface-border);
            color: var(--text-primary);
            padding: 7px 14px;
            border-radius: 20px;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s;
        }

        .config-btn:hover {
            background: var(--surface-hover);
            border-color: #38bdf8;
            color: #38bdf8;
        }

        .share-btn {
            border-color: rgba(168, 85, 247, 0.4);
            color: #d8b4fe;
            background: rgba(168, 85, 247, 0.1);
        }

        .share-btn:hover {
            background: rgba(168, 85, 247, 0.2);
            border-color: #c084fc;
            color: #fff;
        }

        .config-pill {
            background: rgba(56, 189, 248, 0.15);
            color: #38bdf8;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.72rem;
            font-weight: 700;
        }

        .badge-live {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: rgba(16, 185, 129, 0.12);
            color: var(--brand-green);
            padding: 7px 14px;
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 600;
            border: 1px solid rgba(16, 185, 129, 0.25);
            cursor: pointer;
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            background-color: var(--brand-green);
            border-radius: 50%;
            box-shadow: 0 0 8px var(--brand-green);
            animation: pulse 2s infinite;
        }

        .pulse-dot.offline {
            background-color: var(--brand-red);
            box-shadow: 0 0 8px var(--brand-red);
            animation: none;
        }

        @keyframes pulse {
            0% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.4; transform: scale(0.85); }
            100% { opacity: 1; transform: scale(1); }
        }

        /* Layout Grid */
        .grid-main {
            display: grid;
            grid-template-columns: 1fr 1.35fr;
            gap: 20px;
        }

        @media (max-width: 860px) {
            .grid-main {
                grid-template-columns: 1fr;
            }
        }

        /* Card styles */
        .card {
            background: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: var(--radius-lg);
            padding: 20px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.5);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .card-title {
            font-size: 1.1rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        /* Form elements */
        .form-group {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .form-label {
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--text-secondary);
            display: flex;
            justify-content: space-between;
        }

        .val-badge {
            color: var(--brand-green);
            font-weight: 700;
            font-size: 0.95rem;
        }

        input[type="range"] {
            width: 100%;
            height: 8px;
            border-radius: 4px;
            background: #252e3e;
            outline: none;
            cursor: pointer;
        }

        .preset-buttons {
            display: flex;
            gap: 6px;
            flex-wrap: wrap;
        }

        .chip-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--surface-border);
            color: var(--text-secondary);
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 0.8rem;
            cursor: pointer;
            transition: all 0.2s;
        }

        .chip-btn:hover {
            background: var(--surface-hover);
            color: var(--text-primary);
        }

        .chip-btn.active {
            background: rgba(16, 185, 129, 0.15);
            border-color: var(--brand-green);
            color: var(--brand-green);
            font-weight: 600;
        }

        .row-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        input[type="datetime-local"] {
            background: #1b2433;
            border: 1px solid var(--surface-border);
            color: var(--text-primary);
            padding: 10px 12px;
            border-radius: var(--radius-md);
            font-size: 0.9rem;
            outline: none;
            cursor: pointer;
            width: 100%;
        }

        input[type="datetime-local"]:focus-visible {
            border-color: var(--brand-green);
            box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.2);
        }

        /* Result Card - Hero */
        .hero-result {
            background: linear-gradient(145deg, #16242c, #101923);
            border: 1px solid rgba(16, 185, 129, 0.35);
            border-radius: var(--radius-lg);
            padding: 22px;
            display: flex;
            flex-direction: column;
            gap: 16px;
            position: relative;
        }

        /* Action Callout Box */
        .action-callout {
            background: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.3);
            padding: 18px 20px;
            border-radius: var(--radius-md);
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .callout-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .callout-tag {
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--brand-green);
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .countdown-badge {
            background: #0f2e22;
            color: #34d399;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 700;
            border: 1px solid rgba(52, 211, 153, 0.3);
        }

        .countdown-badge.urgent {
            background: rgba(239, 68, 68, 0.2);
            color: #fca5a5;
            border-color: rgba(239, 68, 68, 0.4);
        }

        .callout-times {
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .time-row {
            display: flex;
            align-items: baseline;
            gap: 8px;
        }

        .time-label {
            font-size: 0.85rem;
            color: var(--text-secondary);
            min-width: 140px;
        }

        .time-val {
            font-size: 1.25rem;
            font-weight: 800;
            color: #ffffff;
        }

        .window-duration {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        .warning-banner {
            background: rgba(245, 158, 11, 0.15);
            border: 1px solid rgba(245, 158, 11, 0.35);
            color: #fcd34d;
            padding: 10px 14px;
            border-radius: var(--radius-md);
            font-size: 0.82rem;
            line-height: 1.4;
            display: none;
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 10px;
        }

        .metric-box {
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.07);
            padding: 12px;
            border-radius: var(--radius-md);
            display: flex;
            flex-direction: column;
            gap: 4px;
        }

        .metric-label {
            font-size: 0.72rem;
            color: var(--text-secondary);
        }

        .metric-val {
            font-size: 1.22rem;
            font-weight: 700;
            color: var(--text-primary);
        }

        .metric-sub {
            font-size: 0.72rem;
            color: var(--brand-green);
        }

        /* Timeline Chart */
        .chart-card {
            background: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: var(--radius-lg);
            padding: 18px 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .chart-top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 10px;
        }

        .view-switch-group {
            display: flex;
            background: rgba(0, 0, 0, 0.35);
            border: 1px solid var(--surface-border);
            padding: 2px;
            border-radius: 8px;
            gap: 2px;
        }

        .view-switch-btn {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            padding: 5px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .view-switch-btn:hover {
            color: var(--text-primary);
        }

        .view-switch-btn.active {
            background: #233044;
            color: #ffffff;
            box-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }

        .nav-chips {
            display: flex;
            gap: 6px;
        }

        .nav-chip {
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid var(--surface-border);
            color: var(--text-primary);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.75rem;
            cursor: pointer;
            transition: all 0.2s;
        }

        .nav-chip:hover {
            background: var(--surface-hover);
            border-color: #38bdf8;
            color: #38bdf8;
        }

        .chart-scroll-wrapper {
            position: relative;
            width: 100%;
        }

        .bars-container {
            display: flex;
            align-items: flex-end;
            gap: 3px;
            height: 180px;
            padding: 24px 8px 30px 8px;
            position: relative;
            border-bottom: 1px solid var(--surface-border);
            overflow-x: auto;
            overflow-y: hidden;
            scroll-behavior: smooth;
            -webkit-overflow-scrolling: touch;
        }

        /* Scrollbar styling */
        .bars-container::-webkit-scrollbar {
            height: 8px;
        }
        .bars-container::-webkit-scrollbar-track {
            background: #0f1520;
            border-radius: 4px;
        }
        .bars-container::-webkit-scrollbar-thumb {
            background: #27364b;
            border-radius: 4px;
        }
        .bars-container::-webkit-scrollbar-thumb:hover {
            background: #38bdf8;
        }

        .bar-col {
            min-width: 22px;
            width: 22px;
            height: 100%;
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            align-items: center;
            position: relative;
            cursor: pointer;
            flex-shrink: 0;
        }

        /* Stacked bar elements */
        .bar-stack {
            width: 100%;
            display: flex;
            flex-direction: column;
            justify-content: flex-end;
            align-items: center;
            transition: transform 0.15s ease;
        }

        .bar-seg-fee {
            width: 100%;
            background: rgba(168, 85, 247, 0.4);
            border-top: 2px solid #c084fc;
            border-radius: 4px 4px 0 0;
            transition: height 0.2s ease, opacity 0.2s;
        }

        .bar-seg-spot {
            width: 100%;
            background: linear-gradient(180deg, #38bdf8, #0369a1);
            border-radius: 0 0 2px 2px;
            transition: height 0.2s ease;
        }

        .bar-seg-spot.standalone {
            border-radius: 4px 4px 0 0;
        }

        .bar-col:hover .bar-stack {
            transform: scaleY(1.04);
            filter: brightness(1.25);
            outline: 2px solid #38bdf8;
            outline-offset: 1px;
            border-radius: 4px;
        }

        /* Charging slot styles */
        .bar-col.charging .bar-seg-spot {
            background: linear-gradient(180deg, #10b981, #059669);
            box-shadow: 0 0 10px var(--brand-green-glow);
        }

        .bar-col.charging .bar-seg-fee {
            background: rgba(16, 185, 129, 0.35);
            border-top-color: #34d399;
        }

        .bar-col.deadline {
            border-right: 2px dashed #f59e0b;
        }

        .bar-col.day-start {
            border-left: 2px dashed rgba(56, 189, 248, 0.5);
            margin-left: 8px;
            padding-left: 4px;
        }

        .day-label-tag {
            position: absolute;
            top: 2px;
            left: 6px;
            font-size: 0.68rem;
            font-weight: 700;
            color: #38bdf8;
            background: rgba(15, 23, 42, 0.9);
            padding: 2px 6px;
            border-radius: 4px;
            white-space: nowrap;
            border: 1px solid rgba(56, 189, 248, 0.4);
            pointer-events: none;
            z-index: 5;
        }

        .bar-time-label {
            position: absolute;
            bottom: -24px;
            font-size: 0.65rem;
            color: var(--text-secondary);
            white-space: nowrap;
            user-select: none;
        }

        /* Floating Tooltip */
        #floatingTooltip {
            position: fixed;
            display: none;
            background: #0f172a;
            color: #f8fafc;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 10px 14px;
            font-size: 0.8rem;
            pointer-events: none;
            z-index: 99999;
            box-shadow: 0 12px 30px -5px rgba(0, 0, 0, 0.8);
            line-height: 1.45;
            transform: translate(-50%, -125%);
            min-width: 230px;
        }

        .tt-header {
            font-weight: 700;
            font-size: 0.85rem;
            padding-bottom: 6px;
            margin-bottom: 6px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .tt-tag {
            font-size: 0.7rem;
            padding: 2px 6px;
            border-radius: 4px;
            font-weight: 700;
        }

        .tt-tag.charge {
            background: rgba(16, 185, 129, 0.2);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.4);
        }

        .tt-tag.dl {
            background: rgba(245, 158, 11, 0.2);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.4);
        }

        .tt-row {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            font-size: 0.78rem;
            color: #cbd5e1;
        }

        .tt-row.total {
            margin-top: 6px;
            padding-top: 6px;
            border-top: 1px dashed rgba(255, 255, 255, 0.15);
            font-weight: 700;
            font-size: 0.85rem;
            color: #ffffff;
        }

        .chart-legend {
            display: flex;
            gap: 16px;
            font-size: 0.75rem;
            color: var(--text-secondary);
            flex-wrap: wrap;
            margin-top: 6px;
        }

        .legend-item {
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .legend-box {
            width: 12px;
            height: 12px;
            border-radius: 3px;
        }

        .legend-box.spot {
            background: #38bdf8;
        }

        .legend-box.fee {
            background: rgba(168, 85, 247, 0.7);
            border: 1px solid #c084fc;
        }

        .legend-box.charging {
            background: #10b981;
        }

        .legend-line.deadline {
            width: 14px;
            height: 2px;
            border-top: 2px dashed #f59e0b;
        }

        /* Modal Styles */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(4px);
            display: none;
            align-items: center;
            justify-content: center;
            z-index: 10000;
            padding: 16px;
        }

        .modal-overlay.active {
            display: flex;
        }

        .modal-box {
            background: #141b26;
            border: 1px solid var(--surface-border);
            border-radius: var(--radius-lg);
            width: 100%;
            max-width: 680px;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.8);
            display: flex;
            flex-direction: column;
            gap: 20px;
            padding: 24px;
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--surface-border);
            padding-bottom: 14px;
        }

        .modal-title {
            font-size: 1.25rem;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .modal-close-btn {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 1.4rem;
            cursor: pointer;
            padding: 4px;
            line-height: 1;
        }

        .modal-close-btn:hover {
            color: #ffffff;
        }

        .privacy-callout {
            background: rgba(16, 185, 129, 0.08);
            border: 1px solid rgba(16, 185, 129, 0.25);
            padding: 10px 14px;
            border-radius: var(--radius-md);
            font-size: 0.8rem;
            color: #34d399;
            line-height: 1.4;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .modal-section {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .modal-sec-title {
            font-size: 0.95rem;
            font-weight: 700;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .modal-desc {
            font-size: 0.8rem;
            color: var(--text-secondary);
            line-height: 1.45;
        }

        .custom-input, select.custom-select {
            background: #1b2433;
            border: 1px solid var(--surface-border);
            color: var(--text-primary);
            padding: 10px 12px;
            border-radius: var(--radius-md);
            font-size: 0.9rem;
            outline: none;
            width: 100%;
        }

        select.custom-select {
            cursor: pointer;
        }

        .custom-input:focus-visible, select.custom-select:focus-visible {
            border-color: #38bdf8;
        }

        .bulk-set-row {
            display: flex;
            align-items: center;
            gap: 8px;
            background: rgba(0,0,0,0.25);
            padding: 10px 12px;
            border-radius: var(--radius-md);
            border: 1px solid var(--surface-border);
            flex-wrap: wrap;
        }

        .bulk-input {
            width: 90px;
            background: #1b2433;
            border: 1px solid var(--surface-border);
            color: #fff;
            padding: 6px 10px;
            border-radius: 6px;
            font-size: 0.9rem;
            font-weight: 600;
            outline: none;
        }

        .bulk-apply-btn {
            background: #233044;
            border: 1px solid #38bdf8;
            color: #38bdf8;
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
        }

        .bulk-apply-btn:hover {
            background: #38bdf8;
            color: #0b0f17;
        }

        /* 24h Matrix */
        .hours-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
        }

        @media (max-width: 600px) {
            .hours-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        .day-part-card {
            background: rgba(0, 0, 0, 0.25);
            border: 1px solid var(--surface-border);
            border-radius: var(--radius-md);
            padding: 10px;
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .day-part-title {
            font-size: 0.75rem;
            font-weight: 700;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .hour-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.8rem;
        }

        .hour-label {
            color: var(--text-secondary);
            font-size: 0.78rem;
            font-weight: 600;
        }

        .hour-input {
            width: 68px;
            background: #1b2433;
            border: 1px solid var(--surface-border);
            color: #fff;
            padding: 4px 6px;
            border-radius: 5px;
            font-size: 0.8rem;
            text-align: right;
            outline: none;
        }

        .hour-input:focus {
            border-color: #38bdf8;
        }

        .modal-footer {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-top: 1px solid var(--surface-border);
            padding-top: 16px;
            flex-wrap: wrap;
            gap: 10px;
        }

        .btn-reset {
            background: transparent;
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #f87171;
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 0.82rem;
            cursor: pointer;
        }

        .btn-reset:hover {
            background: rgba(239, 68, 68, 0.15);
        }

        .btn-cancel {
            background: transparent;
            border: 1px solid var(--surface-border);
            color: var(--text-secondary);
            padding: 8px 16px;
            border-radius: 8px;
            font-size: 0.85rem;
            cursor: pointer;
        }

        .btn-cancel:hover {
            background: var(--surface-hover);
            color: #fff;
        }

        .btn-save {
            background: var(--brand-green);
            border: none;
            color: #052e16;
            padding: 8px 20px;
            border-radius: 8px;
            font-size: 0.85rem;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 4px 12px var(--brand-green-glow);
            transition: all 0.2s;
        }

        .btn-save:hover {
            filter: brightness(1.1);
        }

        /* QR Code Share Modal Specifics */
        .qr-display-container {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 16px;
            padding: 10px 0;
        }

        .qr-code-box {
            background: #ffffff;
            padding: 16px;
            border-radius: 14px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.5);
            display: flex;
            align-items: center;
            justify-content: center;
            width: 230px;
            height: 230px;
        }

        .qr-code-box svg {
            width: 100%;
            height: 100%;
            display: block;
        }

        .copy-url-row {
            display: flex;
            width: 100%;
            gap: 8px;
        }
    </style>
</head>
<body>
    <!-- Toast Notification for Sync Success -->
    <div id="syncToast">
        <span>🎉</span>
        <span id="syncToastMsg">Einstellungen erfolgreich übernommen!</span>
    </div>

    <div class="container">
        <!-- Header -->
        <header>
            <div class="title-group">
                <div class="logo-icon">⚡️</div>
                <div>
                    <h1>Smart Charge Planner</h1>
                    <div class="subtitle">Akkudoktor EOS · Dynamischer Stromtarif & Ladeoptimierung</div>
                </div>
            </div>
            <div class="header-actions">
                <button class="config-btn" id="btnInstallPwa" onclick="installPwa()" style="display: none; background: rgba(16, 185, 129, 0.18); border-color: rgba(16, 185, 129, 0.45); color: #34d399;" title="Stromtief als eigenständige App auf deinem Gerät installieren">
                    <span>⚡️ App installieren</span>
                </button>
                <button class="config-btn share-btn" onclick="openShareModal()" title="Einstellungen per QR-Code oder Link auf Smartphone übertragen">
                    <span>📲 Aufs Handy übertragen</span>
                </button>
                <button class="config-btn" onclick="openConfigModal()">
                    <span>⚙️ Mein Tarif & Gebühren</span>
                    <span class="config-pill" id="headerProviderPill">17 ct + 19% MwSt</span>
                </button>
                <div class="badge-live" onclick="openConfigModal()" title="EOS Börsendaten Status">
                    <span class="pulse-dot" id="liveDot"></span>
                    <span id="liveText">EOS Börse Aktiv</span>
                </div>
            </div>
        </header>

        <!-- Main Form & Recommendation Grid -->
        <div class="grid-main">
            <!-- Left: Settings & Sliders -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">🚗 E-Auto Ladeeinstellungen</div>
                </div>

                <!-- Vehicle Presets -->
                <div class="form-group">
                    <div class="form-label">Fahrzeug Preset</div>
                    <div class="preset-buttons">
                        <button class="chip-btn active" id="btnPresetEV3_53" onclick="applyPreset('ev3_53', this)">Kia EV3 (53 kWh)</button>
                        <button class="chip-btn" id="btnPresetEV3_81" onclick="applyPreset('ev3_81', this)">Kia EV3 Long Range (81 kWh)</button>
                        <button class="chip-btn" id="btnPresetModel3" onclick="applyPreset('model3', this)">Tesla Model 3 SR (60 kWh)</button>
                    </div>
                </div>

                <!-- Battery Capacity -->
                <div class="form-group">
                    <div class="form-label">
                        <span>Batterie-Nettokapazität</span>
                        <span class="val-badge" id="capVal">53 kWh</span>
                    </div>
                    <input type="range" id="batteryCap" min="20" max="120" step="1" value="53" oninput="updateVal('capVal', this.value + ' kWh'); persistAndRecalc();">
                </div>

                <!-- SoC Sliders -->
                <div class="row-2">
                    <div class="form-group">
                        <div class="form-label">
                            <span>Ist-SoC (Aktuell)</span>
                            <span class="val-badge" id="socIstVal">25%</span>
                        </div>
                        <input type="range" id="socIst" min="5" max="95" step="5" value="25" oninput="onIstChange(this.value)">
                    </div>

                    <div class="form-group">
                        <div class="form-label">
                            <span>Soll-SoC (Ziel)</span>
                            <span class="val-badge" id="socSollVal">80%</span>
                        </div>
                        <input type="range" id="socSoll" min="20" max="100" step="5" value="80" oninput="onSollChange(this.value)">
                    </div>
                </div>

                <!-- Deadline (Spätestens fertig um) -->
                <div class="form-group">
                    <div class="form-label">
                        <span>⏰ Spätestens fertig geladen um</span>
                        <span id="deadlineStatusText" style="font-size:0.75rem; color: #38bdf8;"></span>
                    </div>
                    <div class="preset-buttons" style="margin-bottom: 6px;">
                        <button class="chip-btn active" onclick="setDeadlinePreset('morgen7', this)">Morgen 07:00</button>
                        <button class="chip-btn" onclick="setDeadlinePreset('morgen8', this)">Morgen 08:00</button>
                        <button class="chip-btn" onclick="setDeadlinePreset('in8h', this)">in 8 Std</button>
                        <button class="chip-btn" onclick="setDeadlinePreset('in12h', this)">in 12 Std</button>
                        <button class="chip-btn" onclick="setDeadlinePreset('heute22', this)">Heute 22:00</button>
                        <button class="chip-btn" onclick="setDeadlinePreset('none', this)">Beliebig</button>
                    </div>
                    <input type="datetime-local" id="deadlineInput" onchange="onDeadlineInputChange()">
                </div>

                <!-- Charging Power & Strategy -->
                <div class="row-2">
                    <div class="form-group">
                        <div class="form-label">
                            <span>Wallbox Ladeleistung</span>
                            <span class="val-badge" id="powerVal">11 kW</span>
                        </div>
                        <input type="range" id="chargePower" min="3.7" max="22" step="0.1" value="11" oninput="updateVal('powerVal', parseFloat(this.value).toFixed(1) + ' kW'); persistAndRecalc();">
                    </div>

                    <div class="form-group">
                        <div class="form-label">Lade-Strategie</div>
                        <div class="preset-buttons" style="margin-top: 4px;">
                            <button class="chip-btn active" id="stratCont" onclick="setStrategy('continuous')">Am Stück</button>
                            <button class="chip-btn" id="stratSplit" onclick="setStrategy('split')">Smart Einzelstunden</button>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Right: Recommended Charging Window Hero Card -->
            <div class="hero-result" id="resultCard">
                <div class="card-header">
                    <div class="card-title">💡 Optimaler Ladeplan</div>
                    <span id="addedRangeBadge" class="countdown-badge">-- km Reichweite</span>
                </div>

                <div class="warning-banner" id="warningBanner"></div>

                <!-- Action Box: Wann anstecken? -->
                <div class="action-callout">
                    <div class="callout-header">
                        <div class="callout-tag">🔌 Ladeempfehlung</div>
                        <div class="countdown-badge" id="countdownBadge">Berechne...</div>
                    </div>

                    <div class="callout-times">
                        <div class="time-row">
                            <span class="time-label">Auto anstecken um:</span>
                            <span class="time-val" id="recStartTime">--:-- Uhr</span>
                        </div>
                        <div class="time-row">
                            <span class="time-label">Fertig geladen um:</span>
                            <span class="time-val" id="recEndTime">--:-- Uhr</span>
                        </div>
                        <div class="window-duration" id="recDuration">Dauer: --</div>
                    </div>
                </div>

                <!-- KPI Metric Grid -->
                <div class="metrics-grid">
                    <div class="metric-box">
                        <span class="metric-label">Ladeenergie (Netz)</span>
                        <span class="metric-val" id="resEnergy">-- kWh</span>
                        <span class="metric-sub" id="resBatteryEnergy">-- kWh netto</span>
                    </div>

                    <div class="metric-box">
                        <span class="metric-label">Gesamtkosten (Brutto)</span>
                        <span class="metric-val" id="resCost">-- €</span>
                        <span class="metric-sub" id="resCostSub">inkl. MwSt.</span>
                    </div>

                    <div class="metric-box">
                        <span class="metric-label">Ø Arbeitspreis (Brutto)</span>
                        <span class="metric-val" id="resAvgPrice">-- ct</span>
                        <span class="metric-sub" id="resSpotPriceSub">-- ct Börse</span>
                    </div>
                </div>

                <div class="metrics-grid">
                    <div class="metric-box" style="grid-column: span 3; background: rgba(16, 185, 129, 0.08); border-color: rgba(16, 185, 129, 0.2);">
                        <span class="metric-label" style="color: #34d399;">💰 Ersparnis durch zeitgesteuertes Laden</span>
                        <span class="metric-val" id="resSavings" style="color: #34d399;">-- €</span>
                        <span class="metric-sub" id="resSavingsSub" style="color: var(--text-secondary);">im Vergleich zum sofortigen Start</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Timeline Chart Card with Horizontal Scroll & View Switch -->
        <div class="chart-card">
            <div class="chart-top-bar">
                <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
                    <span style="font-size: 0.9rem; font-weight: 700; color: #fff;">Strompreise & Ladefenster</span>
                    <div class="view-switch-group">
                        <button class="view-switch-btn active" id="btnViewStacked" onclick="setChartView('stacked')">📊 Gestapelt (Arbeitspreis)</button>
                        <button class="view-switch-btn" id="btnViewTotal" onclick="setChartView('total')">💰 Nur Arbeitspreis</button>
                        <button class="view-switch-btn" id="btnViewSpot" onclick="setChartView('spot')">⚡️ Nur Spotmarkt</button>
                    </div>
                </div>
                <div class="nav-chips">
                    <button class="nav-chip" onclick="scrollToNow()">📍 Jetzt</button>
                    <button class="nav-chip" onclick="scrollToWindow()">⚡️ Zum Ladefenster</button>
                    <button class="nav-chip" onclick="scrollToTomorrow()">➡️ Morgen</button>
                </div>
            </div>

            <!-- Scrollable Bar Chart -->
            <div class="chart-scroll-wrapper">
                <div class="bars-container" id="chartBars">
                    <!-- Dynamic Bars Generated by JS -->
                </div>
            </div>

            <div class="chart-legend">
                <div class="legend-item"><div class="legend-box spot"></div> Spotmarkt (inkl. MwSt.)</div>
                <div class="legend-item"><div class="legend-box fee"></div> Fixe Gebühr / Netzentgelt (inkl. MwSt.)</div>
                <div class="legend-item"><div class="legend-box charging"></div> Geplantes Ladefenster</div>
                <div class="legend-item"><div class="legend-line deadline"></div> Abfahrtszeit / Deadline</div>
            </div>
        </div>

        <!-- Ostrom Referral Hint in App -->
        <div style="margin-top: 24px; background: rgba(245, 158, 11, 0.06); border: 1px solid rgba(245, 158, 11, 0.22); border-radius: 12px; padding: 12px 18px; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; font-size: 0.85rem;">
            <div style="color: #cbd5e1;">
                <span style="color: #fbbf24; font-weight: 700;">🎁 Du willst auch zu Ostrom und mir gleichzeitig danken?</span>
                <span style="color: #94a3b8; margin-left: 6px;">Sichere dir ein Startguthaben mit Code <strong style="color: #fff; font-family: monospace; background: rgba(255,255,255,0.08); padding: 2px 6px; border-radius: 4px; user-select: all;">JANS2MXWNE</strong>.</span>
            </div>
            <a href="https://join.ostrom.de?referralCode=JANS2MXWNE" target="_blank" rel="noopener" style="color: #f59e0b; font-weight: 700; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; white-space: nowrap;">
                <span>Zu Ostrom wechseln</span>
                <span>→</span>
            </a>
        </div>

        <!-- App Footer with Legal Links -->
        <footer style="margin-top: 36px; padding: 20px 0 30px; border-top: 1px solid var(--surface-border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; font-size: 0.8rem; color: var(--text-secondary);">
            <div>⚡️ <strong>stromtief</strong> • Angetrieben von <a href="https://github.com/Akkudoktor-EOS/EOS" target="_blank" rel="noopener" style="color: var(--brand-blue); text-decoration: none;">Akkudoktor EOS</a></div>
            <div style="display: flex; gap: 16px;">
                <a href="https://stromtief.de" style="color: var(--text-secondary); text-decoration: none;">Startseite</a>
                <a href="https://stromtief.de/impressum.html" target="_blank" rel="noopener" style="color: var(--text-secondary); text-decoration: none;">Impressum</a>
                <a href="https://stromtief.de/datenschutz.html" target="_blank" rel="noopener" style="color: var(--text-secondary); text-decoration: none;">Datenschutz</a>
                <a href="https://stromtief.de/haftungsausschluss.html" target="_blank" rel="noopener" style="color: var(--text-secondary); text-decoration: none;">Haftungsausschluss</a>
                <a href="https://github.com/JanST123/stromtief" target="_blank" rel="noopener" style="color: var(--text-secondary); text-decoration: none;">GitHub</a>
            </div>
        </footer>
    </div>

    <!-- Floating Tooltip for Chart -->
    <div id="floatingTooltip"></div>

    <!-- Modal 1: Personal Tariff & VAT Configuration (Stored locally in Browser) -->
    <div class="modal-overlay" id="configModal" onclick="onModalOverlayClick(event)">
        <div class="modal-box">
            <div class="modal-header">
                <div class="modal-title">⚙️ Tarif & Netzentgelte</div>
                <button class="modal-close-btn" onclick="closeConfigModal()">✕</button>
            </div>

            <div class="privacy-callout">
                <span>🔒</span>
                <span><strong>Privat & Sicher:</strong> Ihre individuellen Tarifeinstellungen werden direkt in Ihrem Browser (LocalStorage) gespeichert. Keine Registrierung und keine Daten auf dem Server.</span>
            </div>

            <!-- Section 1: Mehrwertsteuer (VAT) -->
            <div class="modal-section">
                <div class="modal-sec-title">1. Mehrwertsteuer (MwSt. / USt.)</div>
                <div class="modal-desc">
                    Börsenpreise und regulierte Netzentgelte verstehen sich üblicherweise netto. Die Mehrwertsteuer wird auf den <strong>Gesamtpreis (Spotmarkt + fixe Gebühren)</strong> aufgeschlagen.
                </div>
                <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <input type="number" id="modalVatInput" class="bulk-input" style="width: 80px;" value="19.0" step="0.5" min="0" max="50">
                        <span style="font-weight: 700; color: #fff;">%</span>
                    </div>
                    <div class="preset-buttons">
                        <button class="chip-btn" onclick="setVatPreset(19.0)">🇩🇪 19% (Deutschland)</button>
                        <button class="chip-btn" onclick="setVatPreset(20.0)">🇦🇹 20% (Österreich)</button>
                        <button class="chip-btn" onclick="setVatPreset(0.0)">0% (Gewerblich / Netto)</button>
                    </div>
                </div>
            </div>

            <!-- Section 2: 24h Hourly Fixed Fees (Netto) -->
            <div class="modal-section">
                <div class="modal-sec-title">2. Fixe Gebühren & Netzentgelte je Stunde (0-23 Uhr, netto)</div>
                <div class="modal-desc">
                    Geben Sie hier Ihre festen Abgaben, Netzentgelte, Steuern und Anbieteraufschläge in <strong>ct/kWh (netto)</strong> für jede Stunde des Tages (00:00 bis 23:00) ein.
                </div>

                <!-- Presets & Bulk input -->
                <div class="preset-buttons" style="margin-bottom: 4px;">
                    <button class="chip-btn" onclick="applyFeePreset('ostrom')">🇩🇪 Ostrom Standard (17.0 ct netto)</button>
                    <button class="chip-btn" onclick="applyFeePreset('enwg14a')">⚡️ § 14a EnWG Zeitvariabel (10 / 16 ct)</button>
                    <button class="chip-btn" onclick="applyFeePreset('htnt')">🌙 Tag/Nacht HT/NT (11 / 17 ct)</button>
                    <button class="chip-btn" onclick="applyFeePreset('zero')">🆓 Nur Börsenpreis (0.0 ct)</button>
                </div>

                <div class="bulk-set-row">
                    <span style="font-size: 0.8rem; color: #cbd5e1;">Pauschalwert für alle Stunden:</span>
                    <input type="number" id="bulkFeeInput" class="bulk-input" value="17.0" step="0.1" min="0" max="100">
                    <span style="font-size: 0.8rem; color: #94a3b8;">ct/kWh</span>
                    <button class="bulk-apply-btn" onclick="applyBulkFee()">Auf alle 24h anwenden</button>
                </div>

                <!-- 24h Matrix in 4 Day-Part Cards -->
                <div class="hours-grid">
                    <!-- Night: 00 - 05 -->
                    <div class="day-part-card">
                        <div class="day-part-title">🌙 Nacht (00–05h)</div>
                        <div class="hour-row"><span class="hour-label">00:00</span><input type="number" id="fee_0" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">01:00</span><input type="number" id="fee_1" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">02:00</span><input type="number" id="fee_2" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">03:00</span><input type="number" id="fee_3" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">04:00</span><input type="number" id="fee_4" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">05:00</span><input type="number" id="fee_5" class="hour-input" step="0.1"></div>
                    </div>

                    <!-- Morning: 06 - 11 -->
                    <div class="day-part-card">
                        <div class="day-part-title">🌅 Vormittag (06–11h)</div>
                        <div class="hour-row"><span class="hour-label">06:00</span><input type="number" id="fee_6" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">07:00</span><input type="number" id="fee_7" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">08:00</span><input type="number" id="fee_8" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">09:00</span><input type="number" id="fee_9" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">10:00</span><input type="number" id="fee_10" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">11:00</span><input type="number" id="fee_11" class="hour-input" step="0.1"></div>
                    </div>

                    <!-- Afternoon: 12 - 17 -->
                    <div class="day-part-card">
                        <div class="day-part-title">☀️ Nachmittag (12–17h)</div>
                        <div class="hour-row"><span class="hour-label">12:00</span><input type="number" id="fee_12" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">13:00</span><input type="number" id="fee_13" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">14:00</span><input type="number" id="fee_14" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">15:00</span><input type="number" id="fee_15" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">16:00</span><input type="number" id="fee_16" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">17:00</span><input type="number" id="fee_17" class="hour-input" step="0.1"></div>
                    </div>

                    <!-- Evening: 18 - 23 -->
                    <div class="day-part-card">
                        <div class="day-part-title">🌆 Abend (18–23h)</div>
                        <div class="hour-row"><span class="hour-label">18:00</span><input type="number" id="fee_18" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">19:00</span><input type="number" id="fee_19" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">20:00</span><input type="number" id="fee_20" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">21:00</span><input type="number" id="fee_21" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">22:00</span><input type="number" id="fee_22" class="hour-input" step="0.1"></div>
                        <div class="hour-row"><span class="hour-label">23:00</span><input type="number" id="fee_23" class="hour-input" step="0.1"></div>
                    </div>
                </div>
            </div>

            <!-- Section 3: EOS Server Admin (Only visible in Self-Hosted / Admin mode) -->
            <div class="modal-section" id="adminEosSection" style="display: none; border-top: 1px dashed var(--surface-border); padding-top: 14px;">
                <div class="modal-sec-title">🛠 EOS Server Administration (Admin-Modus)</div>
                <div class="modal-desc">
                    Zentrale EOS-Adresse und Börsen-Provider (nur für Administratoren der Server-Instanz).
                </div>
                <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px;">
                    <input type="text" id="modalEosUrlInput" class="custom-input" style="flex: 1;" placeholder="http://127.0.0.1:8503">
                    <button type="button" class="bulk-apply-btn" onclick="testEosConnection()">Verbindung prüfen</button>
                </div>
                <div id="eosConnStatus" style="font-size: 0.8rem; display: none; margin-bottom: 8px; padding: 6px 10px; border-radius: 6px;"></div>
                <select id="modalProviderSelect" class="custom-select">
                    <option value="ElecPriceEnergyCharts:DE-LU">Energy-Charts (DE-LU) — Deutschland & Luxemburg</option>
                    <option value="ElecPriceEnergyCharts:AT">Energy-Charts (AT) — Österreich</option>
                    <option value="ElecPriceSMARD:DE">SMARD.de — Bundesnetzagentur Deutschland</option>
                </select>

                <!-- Ostrom Referral in Settings Modal -->
                <div style="background: rgba(245, 158, 11, 0.08); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 10px; padding: 12px 14px; margin-top: 14px; font-size: 0.82rem; color: #cbd5e1; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
                    <div>
                        <span style="color: #fbbf24; font-weight: 700;">🎁 Du willst zu Ostrom?</span>
                        <div style="font-size: 0.78rem; color: #94a3b8; margin-top: 2px;">
                            Sichere dir deinen Bonus mit Code <strong style="color: #fff; font-family: monospace;">JANS2MXWNE</strong> und unterstütze stromtief.
                        </div>
                    </div>
                    <a href="https://join.ostrom.de?referralCode=JANS2MXWNE" target="_blank" rel="noopener" style="background: #f59e0b; color: #000; font-weight: 700; font-size: 0.78rem; padding: 6px 12px; border-radius: 6px; text-decoration: none; white-space: nowrap;">
                        Zu Ostrom →
                    </a>
                </div>
            </div>

            <div class="modal-footer">
                <button class="btn-reset" onclick="resetUserStorage()">Standardwerte wiederherstellen</button>
                <div style="display: flex; gap: 10px;">
                    <button class="btn-cancel" onclick="closeConfigModal()">Schließen</button>
                    <button class="btn-save" onclick="savePersonalConfig()">💾 In diesem Browser speichern</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Modal 2: QR-Code & Cross-Device Sync Modal -->
    <div class="modal-overlay" id="shareModal" onclick="onShareOverlayClick(event)">
        <div class="modal-box" style="max-width: 480px; text-align: center;">
            <div class="modal-header">
                <div class="modal-title">📲 Einstellungen auf Smartphone übertragen</div>
                <button class="modal-close-btn" onclick="closeShareModal()">✕</button>
            </div>

            <div class="modal-desc" style="text-align: left;">
                Scannen Sie diesen QR-Code mit der Kamera Ihres Smartphones, um Ihr Fahrzeug, Ihre Batteriegröße und Ihre individuellen Netzentgelte sofort zu übernehmen:
            </div>

            <div class="qr-display-container">
                <div class="qr-code-box" id="qrCodeBox">
                    <!-- SVG QR Code generated by JS -->
                </div>
                <span style="font-size: 0.75rem; color: var(--text-secondary);">100% lokal & privat: Keine Daten werden über externe Server gesendet.</span>
            </div>

            <!-- Optional Network URL switcher (helpful if on localhost) -->
            <div id="hostHintContainer" style="display: none; text-align: left; background: rgba(0,0,0,0.25); padding: 10px 12px; border-radius: 8px; font-size: 0.78rem;">
                <span style="color: #38bdf8; font-weight: 700;">WLAN-Adresse für Smartphone:</span>
                <div style="display: flex; gap: 6px; margin-top: 6px;">
                    <input type="text" id="shareHostOverrideInput" class="custom-input" style="padding: 6px 10px; font-size: 0.8rem;" oninput="onShareHostChange()">
                    <button class="bulk-apply-btn" onclick="resetShareHost()">Standard</button>
                </div>
            </div>

            <div class="modal-section" style="text-align: left;">
                <div style="font-size: 0.8rem; font-weight: 700; color: #fff;">Oder Link kopieren & versenden:</div>
                <div class="copy-url-row">
                    <input type="text" id="shareUrlInput" class="custom-input" readonly style="font-size: 0.78rem; color: #94a3b8;" onclick="this.select()">
                    <button class="bulk-apply-btn" id="btnCopyLink" onclick="copyShareLink()">Kopieren</button>
                </div>
            </div>

            <div class="modal-footer" style="justify-content: flex-end;">
                <button class="btn-cancel" onclick="closeShareModal()">Fertig</button>
            </div>
        </div>
    </div>

    <script>
        // Storage Keys
        const STORAGE_KEY = 'eos_smart_charge_user_v1';

        // App State
        let currentStrategy = 'continuous';
        let currentDeadlineIso = null;
        let chartViewMode = 'stacked'; // 'stacked', 'total', 'spot'
        let cachedPlan = null;
        let isReadOnly = true;
        let detectedLocalIp = '127.0.0.1';
        let serverPort = 8505;

        // User Settings (Stored in LocalStorage)
        let userSettings = {
            vehicle_preset: 'ev3_53',
            battery_cap: 53.0,
            charge_power: 11.0,
            soc_ist: 25.0,
            soc_soll: 80.0,
            vat_percent: 19.0,
            hourly_fees: Array(24).fill(17.0)
        };

        const tooltipEl = document.getElementById('floatingTooltip');

        const PRESETS = {
            'ev3_53': { cap: 53, power: 11.0, ist: 25, soll: 80 },
            'ev3_81': { cap: 81, power: 11.0, ist: 25, soll: 80 },
            'model3': { cap: 60, power: 11.0, ist: 20, soll: 80 }
        };

        function showToast(msg) {
            const toast = document.getElementById('syncToast');
            const msgEl = document.getElementById('syncToastMsg');
            msgEl.textContent = msg;
            toast.classList.add('show');
            setTimeout(() => {
                toast.classList.remove('show');
            }, 4500);
        }

        function loadUserStorage() {
            try {
                const raw = localStorage.getItem(STORAGE_KEY);
                if (raw) {
                    const parsed = jsonParseSafe(raw);
                    if (parsed) {
                        userSettings = Object.assign({}, userSettings, parsed);
                    }
                }
            } catch (e) {
                console.warn('LocalStorage nicht verfügbar', e);
            }
        }

        function saveUserStorage() {
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify(userSettings));
            } catch (e) {
                console.warn('LocalStorage Speicherfehler', e);
            }
        }

        function jsonParseSafe(str) {
            try { return JSON.parse(str); } catch (_) { return null; }
        }

        function resetUserStorage() {
            if (confirm('Möchten Sie alle Einstellungen (Fahrzeug, Tarife und Gebühren) auf die Standardwerte zurücksetzen?')) {
                localStorage.removeItem(STORAGE_KEY);
                location.reload();
            }
        }

        // Cross-device Share & QR Code
        function getEncodedConfig() {
            const compact = {
                v: userSettings.vehicle_preset,
                c: userSettings.battery_cap,
                p: userSettings.charge_power,
                i: userSettings.soc_ist,
                s: userSettings.soc_soll,
                vat: userSettings.vat_percent
            };
            const fees = userSettings.hourly_fees || Array(24).fill(17.0);
            const allSame = fees.every(f => f === fees[0]);
            compact.f = allSame ? fees[0] : fees;

            try {
                const jsonStr = JSON.stringify(compact);
                return encodeURIComponent(btoa(unescape(encodeURIComponent(jsonStr))));
            } catch (e) {
                console.error('Encoding error', e);
                return '';
            }
        }

        function decodeConfig(encodedStr) {
            try {
                const jsonStr = decodeURIComponent(escape(atob(decodeURIComponent(encodedStr))));
                const c = JSON.parse(jsonStr);
                if (!c) return null;

                const fees = Array.isArray(c.f) ? c.f : Array(24).fill(typeof c.f === 'number' ? c.f : 15.0);

                return {
                    vehicle_preset: c.v || 'ev3_53',
                    battery_cap: parseFloat(c.c) || 53.0,
                    charge_power: parseFloat(c.p) || 11.0,
                    soc_ist: parseFloat(c.i) || 25.0,
                    soc_soll: parseFloat(c.s) || 80.0,
                    vat_percent: parseFloat(c.vat !== undefined ? c.vat : 19.0),
                    hourly_fees: fees
                };
            } catch (e) {
                console.error('Decoding error', e);
                return null;
            }
        }

        function checkIncomingSync() {
            const hash = window.location.hash;
            if (hash && (hash.startsWith('#cfg=') || hash.startsWith('#config='))) {
                const encoded = hash.split('=')[1];
                const imported = decodeConfig(encoded);
                if (imported) {
                    userSettings = Object.assign({}, userSettings, imported);
                    saveUserStorage();
                    // Clean URL hash without reloading
                    history.replaceState(null, '', window.location.pathname + window.location.search);
                    showToast('🎉 Einstellungen erfolgreich auf dieses Gerät übertragen!');
                    return true;
                }
            }
            return false;
        }

        function getBaseShareUrl() {
            const override = document.getElementById('shareHostOverrideInput')?.value.trim();
            if (override) return override.replace(/\\/+$/, '');

            const isLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
            if (isLocal && detectedLocalIp && detectedLocalIp !== '127.0.0.1') {
                return `http://${detectedLocalIp}:${serverPort}`;
            }
            return window.location.origin;
        }

        function openShareModal() {
            // Check if on localhost to provide Wi-Fi IP tip
            const isLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
            const hintBox = document.getElementById('hostHintContainer');
            if (isLocal && detectedLocalIp && detectedLocalIp !== '127.0.0.1') {
                hintBox.style.display = 'block';
                document.getElementById('shareHostOverrideInput').value = `http://${detectedLocalIp}:${serverPort}`;
            } else {
                hintBox.style.display = 'none';
            }

            renderShareQr();
            document.getElementById('shareModal').classList.add('active');
        }

        function closeShareModal() {
            document.getElementById('shareModal').classList.remove('active');
        }

        function onShareOverlayClick(e) {
            if (e.target.id === 'shareModal') closeShareModal();
        }

        function renderShareQr() {
            const base = getBaseShareUrl();
            const fullUrl = `${base}/#cfg=${getEncodedConfig()}`;
            document.getElementById('shareUrlInput').value = fullUrl;

            const qrBox = document.getElementById('qrCodeBox');
            qrBox.innerHTML = '';

            try {
                if (typeof qrcode !== 'undefined') {
                    const qr = qrcode(0, 'M');
                    qr.addData(fullUrl);
                    qr.make();
                    // Create responsive SVG tag
                    qrBox.innerHTML = qr.createSvgTag(5, 2);
                } else {
                    qrBox.innerHTML = '<span style="color:#64748b; font-size:0.8rem;">QR-Code Generator lädt...</span>';
                }
            } catch (err) {
                console.error(err);
                qrBox.innerHTML = '<span style="color:#ef4444; font-size:0.8rem;">Fehler beim Erzeugen des QR-Codes</span>';
            }
        }

        function onShareHostChange() {
            renderShareQr();
        }

        function resetShareHost() {
            document.getElementById('shareHostOverrideInput').value = `http://${detectedLocalIp}:${serverPort}`;
            renderShareQr();
        }

        async function copyShareLink() {
            const input = document.getElementById('shareUrlInput');
            input.select();
            input.setSelectionRange(0, 99999);
            try {
                await navigator.clipboard.writeText(input.value);
                const btn = document.getElementById('btnCopyLink');
                btn.textContent = 'Kopiert! ✓';
                btn.style.background = '#10b981';
                btn.style.color = '#fff';
                setTimeout(() => {
                    btn.textContent = 'Kopieren';
                    btn.style.background = '';
                    btn.style.color = '';
                }, 2000);
            } catch (e) {
                alert('Link im Feld kopieren: ' + input.value);
            }
        }

        function updateVal(id, text) {
            document.getElementById(id).textContent = text;
        }

        function applyPreset(name, btn) {
            document.querySelectorAll('.form-group .preset-buttons .chip-btn').forEach(b => b.classList.remove('active'));
            if (btn) btn.classList.add('active');

            const p = PRESETS[name];
            if (!p) return;

            userSettings.vehicle_preset = name;
            userSettings.battery_cap = p.cap;
            userSettings.charge_power = p.power;
            userSettings.soc_ist = p.ist;
            userSettings.soc_soll = p.soll;

            document.getElementById('batteryCap').value = p.cap;
            updateVal('capVal', p.cap + ' kWh');

            document.getElementById('chargePower').value = p.power;
            updateVal('powerVal', p.power.toFixed(1) + ' kW');

            document.getElementById('socIst').value = p.ist;
            updateVal('socIstVal', p.ist + '%');

            document.getElementById('socSoll').value = p.soll;
            updateVal('socSollVal', p.soll + '%');

            persistAndRecalc();
        }

        function onIstChange(val) {
            updateVal('socIstVal', val + '%');
            userSettings.soc_ist = parseFloat(val);
            const sollEl = document.getElementById('socSoll');
            if (parseInt(val) >= parseInt(sollEl.value)) {
                sollEl.value = Math.min(100, parseInt(val) + 5);
                updateVal('socSollVal', sollEl.value + '%');
                userSettings.soc_soll = parseFloat(sollEl.value);
            }
            persistAndRecalc();
        }

        function onSollChange(val) {
            updateVal('socSollVal', val + '%');
            userSettings.soc_soll = parseFloat(val);
            const istEl = document.getElementById('socIst');
            if (parseInt(val) <= parseInt(istEl.value)) {
                istEl.value = Math.max(0, parseInt(val) - 5);
                updateVal('socIstVal', istEl.value + '%');
                userSettings.soc_ist = parseFloat(istEl.value);
            }
            persistAndRecalc();
        }

        function setStrategy(strat) {
            currentStrategy = strat;
            document.getElementById('stratCont').classList.toggle('active', strat === 'continuous');
            document.getElementById('stratSplit').classList.toggle('active', strat === 'split');
            triggerRecalc();
        }

        function setChartView(mode) {
            chartViewMode = mode;
            document.getElementById('btnViewStacked').classList.toggle('active', mode === 'stacked');
            document.getElementById('btnViewTotal').classList.toggle('active', mode === 'total');
            document.getElementById('btnViewSpot').classList.toggle('active', mode === 'spot');
            if (cachedPlan && cachedPlan.timeline) {
                renderBars(cachedPlan.timeline);
            }
        }

        function formatDateTimeLocal(dt) {
            const year = dt.getFullYear();
            const month = String(dt.getMonth() + 1).padStart(2, '0');
            const day = String(dt.getDate()).padStart(2, '0');
            const hours = String(dt.getHours()).padStart(2, '0');
            const minutes = String(dt.getMinutes()).padStart(2, '0');
            return `${year}-${month}-${day}T${hours}:${minutes}`;
        }

        function setDeadlinePreset(preset, btn) {
            const container = btn.parentElement;
            container.querySelectorAll('.chip-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            const now = new Date();
            let targetDt = null;
            let statusText = '';

            if (preset === 'morgen7') {
                targetDt = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 7, 0, 0);
                statusText = 'Morgen um 07:00 Uhr';
            } else if (preset === 'morgen8') {
                targetDt = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0, 0);
                statusText = 'Morgen um 08:00 Uhr';
            } else if (preset === 'in8h') {
                targetDt = new Date(now.getTime() + 8 * 3600 * 1000);
                statusText = `In 8 Stunden (${String(targetDt.getHours()).padStart(2,'0')}:${String(targetDt.getMinutes()).padStart(2,'0')} Uhr)`;
            } else if (preset === 'in12h') {
                targetDt = new Date(now.getTime() + 12 * 3600 * 1000);
                statusText = `In 12 Stunden (${String(targetDt.getHours()).padStart(2,'0')}:${String(targetDt.getMinutes()).padStart(2,'0')} Uhr)`;
            } else if (preset === 'heute22') {
                targetDt = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 22, 0, 0);
                if (targetDt <= now) {
                    targetDt.setDate(targetDt.getDate() + 1);
                    statusText = 'Morgen um 22:00 Uhr';
                } else {
                    statusText = 'Heute um 22:00 Uhr';
                }
            } else if (preset === 'none') {
                targetDt = null;
                statusText = 'Keine Begrenzung';
            }

            const input = document.getElementById('deadlineInput');
            if (targetDt) {
                input.value = formatDateTimeLocal(targetDt);
                currentDeadlineIso = targetDt.toISOString();
            } else {
                input.value = '';
                currentDeadlineIso = null;
            }
            document.getElementById('deadlineStatusText').textContent = statusText;
            triggerRecalc();
        }

        function onDeadlineInputChange() {
            const input = document.getElementById('deadlineInput');
            if (input.value) {
                const dt = new Date(input.value);
                currentDeadlineIso = dt.toISOString();
                document.getElementById('deadlineStatusText').textContent =
                    `${dt.toLocaleDateString('de-DE', {weekday:'short', day:'2-digit', month:'2-digit'})} ${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')} Uhr`;
            } else {
                currentDeadlineIso = null;
                document.getElementById('deadlineStatusText').textContent = 'Keine Begrenzung';
            }
            document.querySelectorAll('.form-group .preset-buttons .chip-btn').forEach(b => b.classList.remove('active'));
            triggerRecalc();
        }

        // Configuration Modal
        function openConfigModal() {
            document.getElementById('modalVatInput').value = parseFloat(userSettings.vat_percent).toFixed(1);

            const fees = userSettings.hourly_fees || Array(24).fill(17.0);
            for (let h = 0; h < 24; h++) {
                const el = document.getElementById(`fee_${h}`);
                if (el) el.value = parseFloat(fees[h]).toFixed(1);
            }
            document.getElementById('bulkFeeInput').value = parseFloat(fees[0] || 15.0).toFixed(1);

            document.getElementById('adminEosSection').style.display = isReadOnly ? 'none' : 'block';
            document.getElementById('configModal').classList.add('active');
        }

        function closeConfigModal() {
            document.getElementById('configModal').classList.remove('active');
        }

        function onModalOverlayClick(e) {
            if (e.target.id === 'configModal') {
                closeConfigModal();
            }
        }

        function setVatPreset(rate) {
            document.getElementById('modalVatInput').value = parseFloat(rate).toFixed(1);
        }

        function applyBulkFee() {
            const val = parseFloat(document.getElementById('bulkFeeInput').value) || 0;
            for (let h = 0; h < 24; h++) {
                const el = document.getElementById(`fee_${h}`);
                if (el) el.value = val.toFixed(1);
            }
        }

        function applyFeePreset(preset) {
            if (preset === 'ostrom') {
                document.getElementById('bulkFeeInput').value = '17.0';
                applyBulkFee();
                setVatPreset(19.0);
            } else if (preset === 'enwg14a') {
                for (let h = 0; h < 24; h++) {
                    const el = document.getElementById(`fee_${h}`);
                    if (el) el.value = (h < 6) ? '10.0' : '16.0';
                }
                document.getElementById('bulkFeeInput').value = '16.0';
                setVatPreset(19.0);
            } else if (preset === 'htnt') {
                for (let h = 0; h < 24; h++) {
                    const el = document.getElementById(`fee_${h}`);
                    if (el) el.value = (h >= 22 || h < 6) ? '11.0' : '17.0';
                }
                document.getElementById('bulkFeeInput').value = '17.0';
                setVatPreset(19.0);
            } else if (preset === 'zero') {
                document.getElementById('bulkFeeInput').value = '0.0';
                applyBulkFee();
            }
        }

        function savePersonalConfig() {
            userSettings.vat_percent = parseFloat(document.getElementById('modalVatInput').value) || 0.0;
            const fees = [];
            for (let h = 0; h < 24; h++) {
                const el = document.getElementById(`fee_${h}`);
                fees.push(parseFloat(el ? el.value : 15.0) || 0.0);
            }
            userSettings.hourly_fees = fees;

            saveUserStorage();
            updateHeaderPill();
            closeConfigModal();
            triggerRecalc();
        }

        function updateHeaderPill() {
            const avgFeeNet = (userSettings.hourly_fees.reduce((a,b)=>a+b, 0) / 24).toFixed(1);
            document.getElementById('headerProviderPill').textContent = `Ø ${avgFeeNet} ct + ${userSettings.vat_percent}% MwSt`;
        }

        function persistAndRecalc() {
            userSettings.battery_cap = parseFloat(document.getElementById('batteryCap').value);
            userSettings.charge_power = parseFloat(document.getElementById('chargePower').value);
            saveUserStorage();
            triggerRecalc();
        }

        // Calculation & Planning
        let debounceTimer = null;
        function triggerRecalc() {
            if (debounceTimer) clearTimeout(debounceTimer);
            debounceTimer = setTimeout(doRecalc, 120);
        }

        async function doRecalc() {
            const payload = {
                battery_capacity_kwh: parseFloat(document.getElementById('batteryCap').value),
                current_soc: parseFloat(document.getElementById('socIst').value),
                target_soc: parseFloat(document.getElementById('socSoll').value),
                charge_power_kw: parseFloat(document.getElementById('chargePower').value),
                efficiency_percent: 90.0,
                deadline_iso: currentDeadlineIso,
                strategy: currentStrategy,
                vat_percent: userSettings.vat_percent,
                hourly_fees: userSettings.hourly_fees
            };

            try {
                const res = await fetch('/api/plan', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                if (!res.ok) {
                    const err = await res.json();
                    showError(err.error || 'Fehler bei der Berechnung');
                    setLiveStatus(false);
                    return;
                }

                const data = await res.json();
                cachedPlan = data;
                setLiveStatus(true);
                renderResult(data);
            } catch (err) {
                console.error(err);
                showError('Verbindung zum Server fehlgeschlagen.');
                setLiveStatus(false);
            }
        }

        function showError(msg) {
            const warningEl = document.getElementById('warningBanner');
            warningEl.style.display = 'block';
            warningEl.textContent = '❌ ' + msg;
        }

        function setLiveStatus(isOnline) {
            const dot = document.getElementById('liveDot');
            const txt = document.getElementById('liveText');
            if (isOnline) {
                dot.className = 'pulse-dot';
                txt.textContent = 'EOS Börse Aktiv';
                txt.style.color = 'var(--brand-green)';
            } else {
                dot.className = 'pulse-dot offline';
                txt.textContent = 'Börsendaten Offline';
                txt.style.color = 'var(--brand-red)';
            }
        }

        function renderResult(data) {
            const warningEl = document.getElementById('warningBanner');
            if (data.warning) {
                warningEl.style.display = 'block';
                warningEl.textContent = data.warning;
            } else {
                warningEl.style.display = 'none';
            }

            const startDt = new Date(data.start_time);
            const endDt = new Date(data.end_time);

            const formatTime = (d) => {
                const isToday = (new Date()).toDateString() === d.toDateString();
                const prefix = isToday ? 'Heute' : 'Morgen';
                const timeStr = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
                return `${prefix}, ${timeStr} Uhr`;
            };

            document.getElementById('recStartTime').textContent = formatTime(startDt);
            document.getElementById('recEndTime').textContent = formatTime(endDt);
            document.getElementById('recDuration').textContent = `Effektive Ladedauer: ${data.duration_formatted} (${data.slots_count * 15} Minuten)`;

            const cdBadge = document.getElementById('countdownBadge');
            cdBadge.textContent = data.countdown_text;
            cdBadge.classList.toggle('urgent', data.start_soon);

            document.getElementById('addedRangeBadge').textContent = `+${data.range_km} km Reichweite`;

            document.getElementById('resEnergy').textContent = `${data.energy_grid_kwh.toFixed(1)} kWh`;
            document.getElementById('resBatteryEnergy').textContent = `${data.energy_battery_kwh.toFixed(1)} kWh netto`;

            document.getElementById('resCost').textContent = `${data.total_cost_euro.toFixed(2)} €`;
            document.getElementById('resCostSub').textContent = `(Netto ${data.total_net_cost_euro.toFixed(2)}€ + ${data.vat_cost_euro.toFixed(2)}€ MwSt [${data.vat_percent}%])`;

            document.getElementById('resAvgPrice').textContent = `${data.avg_price_cent.toFixed(1)} ct/kWh`;
            document.getElementById('resSpotPriceSub').textContent = `davon Ø ${data.avg_spot_price_cent.toFixed(1)} ct Börse (brutto)`;

            document.getElementById('resSavings').textContent = `${data.savings_vs_now_euro.toFixed(2)} €`;
            document.getElementById('resSavingsSub').textContent = `Ersparnis vs. Sofortladen (${data.immediate_cost_euro.toFixed(2)} € brutto)`;

            renderBars(data.timeline);
        }

        function renderBars(timeline) {
            const container = document.getElementById('chartBars');
            container.innerHTML = '';

            if (!timeline || timeline.length === 0) return;

            const maxTotal = Math.max(...timeline.map(t => Math.max(t.total_price, 1.0)), 25.0);
            const maxSpot = Math.max(...timeline.map(t => Math.max(t.spot_price, 1.0)), 15.0);

            const effectiveMax = (chartViewMode === 'spot') ? maxSpot : maxTotal;
            const chartH = 125;

            timeline.forEach((item, idx) => {
                const col = document.createElement('div');
                col.className = 'bar-col';
                col.dataset.idx = idx;

                if (item.is_charging) col.classList.add('charging');
                if (item.is_deadline) col.classList.add('deadline');
                if (item.is_day_start) col.classList.add('day-start');

                const d = new Date(item.dt);
                const timeStr = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
                const dateStr = d.toLocaleDateString('de-DE', { weekday: 'short', day: '2-digit', month: 'short' });

                if (item.is_day_start) {
                    const tag = document.createElement('div');
                    tag.className = 'day-label-tag';
                    tag.textContent = dateStr;
                    col.appendChild(tag);
                }

                const stack = document.createElement('div');
                stack.className = 'bar-stack';

                const spotH = Math.max(2, Math.round((Math.max(0, item.spot_price) / effectiveMax) * chartH));
                const feeH = Math.max(2, Math.round((Math.max(0, item.fixed_fee) / effectiveMax) * chartH));
                const totalH = Math.max(2, Math.round((Math.max(0, item.total_price) / effectiveMax) * chartH));

                if (chartViewMode === 'stacked') {
                    const feeSeg = document.createElement('div');
                    feeSeg.className = 'bar-seg-fee';
                    feeSeg.style.height = `${feeH}px`;
                    stack.appendChild(feeSeg);

                    const spotSeg = document.createElement('div');
                    spotSeg.className = 'bar-seg-spot';
                    spotSeg.style.height = `${spotH}px`;
                    stack.appendChild(spotSeg);
                } else if (chartViewMode === 'total') {
                    const totalSeg = document.createElement('div');
                    totalSeg.className = 'bar-seg-spot standalone';
                    totalSeg.style.height = `${totalH}px`;
                    if (item.is_charging) {
                        totalSeg.style.background = 'linear-gradient(180deg, #10b981, #059669)';
                    } else {
                        totalSeg.style.background = 'linear-gradient(180deg, #38bdf8, #0369a1)';
                    }
                    stack.appendChild(totalSeg);
                } else if (chartViewMode === 'spot') {
                    const spotOnly = document.createElement('div');
                    spotOnly.className = 'bar-seg-spot standalone';
                    spotOnly.style.height = `${spotH}px`;
                    stack.appendChild(spotOnly);
                }

                col.appendChild(stack);

                col.addEventListener('mouseenter', (e) => {
                    let tagHtml = '';
                    if (item.is_charging) tagHtml = '<span class="tt-tag charge">⚡️ Ladefenster</span>';
                    else if (item.is_deadline) tagHtml = '<span class="tt-tag dl">🎯 Abfahrt</span>';

                    tooltipEl.innerHTML = `
                        <div class="tt-header">
                            <span>${dateStr} · ${timeStr}</span>
                            ${tagHtml}
                        </div>
                        <div class="tt-row">
                            <span>⚡️ Spotmarkt:</span>
                            <strong>${item.spot_price_net.toFixed(2)} ct <span style="font-size:0.7rem; color:#94a3b8;">(${item.spot_price.toFixed(2)} brutto)</span></strong>
                        </div>
                        <div class="tt-row">
                            <span>🔧 Fixe Gebühr (${d.getHours()}h):</span>
                            <strong>+ ${item.fixed_fee_net.toFixed(2)} ct <span style="font-size:0.7rem; color:#94a3b8;">(${item.fixed_fee.toFixed(2)} brutto)</span></strong>
                        </div>
                        <div class="tt-row" style="color: #cbd5e1;">
                            <span>💶 MwSt. (${item.vat_percent}%):</span>
                            <strong>+ ${item.vat_amount.toFixed(2)} ct/kWh</strong>
                        </div>
                        <div class="tt-row total">
                            <span>👉 Brutto-Arbeitspreis:</span>
                            <span style="color:#38bdf8;">${item.total_price.toFixed(2)} ct/kWh</span>
                        </div>
                    `;
                    tooltipEl.style.display = 'block';
                    positionTooltip(e);
                });

                col.addEventListener('mousemove', (e) => {
                    positionTooltip(e);
                });

                col.addEventListener('mouseleave', () => {
                    tooltipEl.style.display = 'none';
                });

                if (idx % 4 === 0) {
                    const label = document.createElement('div');
                    label.className = 'bar-time-label';
                    label.textContent = d.getHours() + 'h';
                    col.appendChild(label);
                }

                container.appendChild(col);
            });
        }

        function positionTooltip(e) {
            const x = e.clientX;
            const y = e.clientY;
            tooltipEl.style.left = x + 'px';
            tooltipEl.style.top = y + 'px';
        }

        function scrollToNow() {
            const container = document.getElementById('chartBars');
            container.scrollTo({ left: 0, behavior: 'smooth' });
        }

        function scrollToWindow() {
            const container = document.getElementById('chartBars');
            const chargingCol = container.querySelector('.bar-col.charging');
            if (chargingCol) {
                const targetLeft = chargingCol.offsetLeft - (container.clientWidth / 2) + 20;
                container.scrollTo({ left: Math.max(0, targetLeft), behavior: 'smooth' });
            }
        }

        function scrollToTomorrow() {
            const container = document.getElementById('chartBars');
            const dayStart = container.querySelector('.bar-col.day-start');
            if (dayStart) {
                const targetLeft = dayStart.offsetLeft - 16;
                container.scrollTo({ left: Math.max(0, targetLeft), behavior: 'smooth' });
            }
        }

        const chartContainer = document.getElementById('chartBars');
        chartContainer.addEventListener('wheel', (e) => {
            if (e.deltaY !== 0) {
                e.preventDefault();
                chartContainer.scrollLeft += e.deltaY;
            }
        }, { passive: false });

        // Progressive Web App (PWA) Support
        let deferredPrompt = null;
        window.addEventListener('beforeinstallprompt', (e) => {
            e.preventDefault();
            deferredPrompt = e;
            const btn = document.getElementById('btnInstallPwa');
            if (btn) btn.style.display = 'inline-flex';
        });

        window.addEventListener('appinstalled', () => {
            deferredPrompt = null;
            const btn = document.getElementById('btnInstallPwa');
            if (btn) btn.style.display = 'none';
            showToast('Stromtief wurde erfolgreich als App installiert!');
        });

        function installPwa() {
            if (deferredPrompt) {
                deferredPrompt.prompt();
                deferredPrompt.userChoice.then((choiceResult) => {
                    if (choiceResult.outcome === 'accepted') {
                        console.log('User accepted the PWA install prompt');
                    }
                    deferredPrompt = null;
                });
            } else {
                alert('App auf dem Smartphone installieren:\n\n• iOS / Safari: Tippe unten auf das Teilen-Symbol ⎋ und wähle "Zum Home-Bildschirm" 📲\n• Android / Chrome: Tippe oben rechts auf das Menü ⋮ und wähle "App installieren" bzw. "Zum Startbildschirm hinzufügen" 📲');
            }
        }

        // Register Service Worker for offline support & fast caching
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/sw.js', { scope: '/' })
                    .then((reg) => console.log('ServiceWorker registered with scope:', reg.scope))
                    .catch((err) => console.log('ServiceWorker registration failed:', err));
            });
        }

        // Initialize on page load with LocalStorage restoration & sync check
        window.addEventListener('DOMContentLoaded', async () => {
            // First check if user arrived via a share link (#cfg=...)
            const synced = checkIncomingSync();
            if (!synced) {
                loadUserStorage();
            }

            // Restore vehicle inputs
            document.getElementById('batteryCap').value = userSettings.battery_cap;
            updateVal('capVal', userSettings.battery_cap + ' kWh');

            document.getElementById('chargePower').value = userSettings.charge_power;
            updateVal('powerVal', parseFloat(userSettings.charge_power).toFixed(1) + ' kW');

            document.getElementById('socIst').value = userSettings.soc_ist;
            updateVal('socIstVal', userSettings.soc_ist + '%');

            document.getElementById('socSoll').value = userSettings.soc_soll;
            updateVal('socSollVal', userSettings.soc_soll + '%');

            if (userSettings.vehicle_preset) {
                document.querySelectorAll('.form-group .preset-buttons .chip-btn').forEach(b => b.classList.remove('active'));
                if (userSettings.vehicle_preset === 'ev3_53') document.getElementById('btnPresetEV3_53')?.classList.add('active');
                else if (userSettings.vehicle_preset === 'ev3_81') document.getElementById('btnPresetEV3_81')?.classList.add('active');
                else if (userSettings.vehicle_preset === 'model3') document.getElementById('btnPresetModel3')?.classList.add('active');
            }

            updateHeaderPill();

            // Fetch system info to detect read-only status and local Wi-Fi IP
            try {
                const sysRes = await fetch('/api/system');
                if (sysRes.ok) {
                    const sys = await sysRes.json();
                    isReadOnly = sys.read_only;
                    detectedLocalIp = sys.local_ip || '127.0.0.1';
                    serverPort = sys.port || 8505;
                    setLiveStatus(sys.eos_online);
                }
            } catch (e) {
                console.warn('Systemstatus konnte nicht abgerufen werden', e);
            }

            const firstPresetBtn = document.querySelector('.form-group .preset-buttons .chip-btn');
            setDeadlinePreset('morgen7', firstPresetBtn);
        });
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_CONTENT


if __name__ == "__main__":
    print(f"Starting EOS Smart Charge Planner on http://127.0.0.1:{PORT}")
    print(f"Read-Only Mode: {'AKTIV (Sicher für Multi-User / Public SaaS)' if READ_ONLY_MODE else 'DEAKTIVIERT (Lokaler Admin-Modus)'}")
    print(f"Target EOS Server: {get_active_eos_url()}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
