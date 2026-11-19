# ⚡️ EOS Smart Charge Planner

> **Smarte Ladeplanung für E-Autos mit dynamischen Stromtarifen (Ostrom, Tibber, EPEX Spot) auf Basis von [Akkudoktor EOS](https://github.com/Akkudoktor-EOS/EOS).**

Der **EOS Smart Charge Planner** bietet eine aufgeräumte, moderne und reaktionsschnelle Weboberfläche, die E-Auto-Besitzern auf einen Blick anzeigt:
1. **Wann muss das Auto angesteckt werden**, um zur gewünschten Abfahrtszeit zum günstigsten Börsenstrompreis voll zu sein?
2. **Wie hoch sind die Gesamtkosten**, transparent aufgeschlüsselt nach Börsenpreis, Netzentgelten und Mehrwertsteuer?
3. **Wie viel Geld spart man** im Vergleich zum sofortigen Laden?

---

## ✨ Features

* **🔌 Konfigurierbarer EOS-Endpunkt:**
  * Verbindung zu lokalem EOS (`http://127.0.0.1:8503`) oder Netzwerk-Installationen (z. B. Raspberry Pi, Home Assistant unter `http://192.168.178.50:8503`).
  * Integrierter Verbindungstest mit Status-Badge.
* **⚡️ Spotmarkt-Quellenauswahl:**
  * Direkte Umschaltung der Börsenpreisquelle in EOS (Energy-Charts DE-LU/AT/CH, SMARD.de Bundesnetzagentur, Akkudoktor Community Server).
* **💶 Flexible Netzentgelte & 24h-Stundengebühren (0–23 Uhr):**
  * Erfassung von fixen oder zeitvariablen Netzentgelten (§ 14a EnWG Modul 3, Hoch-/Niedertarif).
  * 1-Klick-Presets (*Ostrom Standard 15 ct*, *§ 14a EnWG Dynamisch 10/16 ct*, *Tag/Nacht HT/NT*, *Reiner Börsenpreis 0 ct*).
  * Pauschale Zuweisung auf alle 24 Stunden.
* **🧾 Mehrwertsteuer auf Gesamtpreis:**
  * Einstellbarer Steuersatz (z. B. 19% DE, 20% AT, 0% Netto), der vorschriftsmäßig auf die Summe aus Spotmarkt + Fixgebühr aufgeschlagen wird.
* **📊 Interaktive Preisgrafik über mehrere Tage:**
  * **Flüssiges horizontales Scrollen** über 48 Stunden (auch mit dem Mausrad).
  * **Blickwinkel-Umschalter**:
    * `📊 Gestapelt`: Zeigt Börsenanteil (blau/grün) und fixe Gebühren (lila) gestapelt als Gesamtarbeitspreis.
    * `💰 Nur Arbeitspreis`: Gesamtpreis je kWh.
    * `⚡️ Nur Spotmarkt`: Reiner Börsenpreis je kWh.
  * **Interaktiver Hover-Tooltip**: Exakte Aufschlüsselung von Börsenpreis, Fixgebühr, MwSt. und Gesamtarbeitspreis für jeden 15-Minuten-Slot.
  * **Tageswechsel-Markierungen** & Schnellnavigation (`📍 Jetzt`, `⚡️ Zum Ladefenster`, `➡️ Morgen`).
* **🚗 Fahrzeug-Presets & Lade-Strategien:**
  * Presets z. B. für Kia EV3 (53 kWh / 81 kWh Long Range), Tesla Model 3.
  * Schieberegler für Ist-SoC, Soll-SoC, Ladeleistung (3,7 – 22 kW).
  * Abfahrtszeit / Deadline-Presets (*Morgen 07:00*, *Morgen 08:00*, *in 8h*, *in 12h*, *Beliebig*).
  * Strategie-Auswahl: *Am Stück* (zusammenhängendes Ladefenster) vs. *Smart Einzelstunden* (die absolut günstigsten Einzelslots).

---

## 🚀 Schnellstart

### Option 1: Lokal mit Python

```bash
git clone https://github.com/DEIN-BENUTZER/eos-smart-charge-ui.git
cd eos-smart-charge-ui

# Abhängigkeiten installieren
pip install -r requirements.txt

# Starten (Standard: Port 8505, EOS unter http://127.0.0.1:8503)
python app.py

# Oder mit dem Start-Skript:
./start.sh
```

Öffnen Sie anschließend **[http://localhost:8505](http://localhost:8505)** im Browser.

### Option 2: Mit Docker / Docker Compose

```bash
# Starten per Docker Compose
docker compose up -d
```

In `docker-compose.yml` können Sie die Umgebungsvariable `EOS_API_URL` anpassen, z. B.:
```yaml
environment:
  - SMART_CHARGE_PORT=8505
  - EOS_API_URL=http://192.168.178.50:8503
```

---

## ⚙️ Konfiguration

Die Einstellungen werden automatisch in `data/settings.json` gespeichert und können jederzeit über das Zahnrad-Symbol (**`⚙️ EOS & Tarif-Einstellungen`**) in der UI angepasst werden:

```json
{
  "eos_url": "http://127.0.0.1:8503",
  "provider": "ElecPriceEnergyCharts",
  "bidding_zone": "DE-LU",
  "vat_percent": 19.0,
  "hourly_fees": [
    15.0, 15.0, 15.0, 15.0, 15.0, 15.0,
    15.0, 15.0, 15.0, 15.0, 15.0, 15.0,
    15.0, 15.0, 15.0, 15.0, 15.0, 15.0,
    15.0, 15.0, 15.0, 15.0, 15.0, 15.0
  ]
}
```

---

## 🏡 Home Assistant Integration

Sie können die UI als Dashboard-Karte in Home Assistant einbinden:

```yaml
type: iframe
url: http://192.168.178.XX:8505/
aspect_ratio: 100%
```

---

## 🛠 REST-API Endpunkte

| Methode | Endpunkt | Beschreibung |
|---|---|---|
| `GET` | `/` | Web-Oberfläche |
| `GET` | `/api/prices` | 15-Minuten-Preisreihe mit Netto-/Brutto-Aufschlüsselung |
| `POST` | `/api/plan` | Berechnet optimales Ladefenster basierend auf Fahrzeugdaten & Deadline |
| `GET` | `/api/config/provider` | Ruft aktuelle Tarif-, MwSt.- und EOS-Einstellungen ab |
| `POST` | `/api/config/provider` | Speichert Einstellungen und aktualisiert EOS |
| `POST` | `/api/eos/test` | Prüft die Verbindung zu einer EOS Server-URL |

---

## 📄 Lizenz

MIT License — Frei verwendbar und anpassbar.
Basierend auf der Open-Source-Software [Akkudoktor-EOS](https://github.com/Akkudoktor-EOS/EOS).
