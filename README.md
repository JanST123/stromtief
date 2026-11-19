# ⚡️ EOS Smart Charge Planner

> **Smarte Ladeplanung für E-Autos mit dynamischen Stromtarifen (Ostrom, Tibber, EPEX Spot) auf Basis von [Akkudoktor EOS](https://github.com/Akkudoktor-EOS/EOS).**
> 
> Konzipiert für den lokalen Eigenbetrieb **oder** als sicherer, mandantenfähiger Online-Dienst für tausende Nutzer.

Der **EOS Smart Charge Planner** bietet eine aufgeräumte, moderne und reaktionsschnelle Weboberfläche, die E-Auto-Besitzern auf einen Blick anzeigt:
1. **Wann muss das Auto angesteckt werden**, um zur gewünschten Abfahrtszeit zum günstigsten Börsenstrompreis voll zu sein?
2. **Wie hoch sind die Gesamtkosten**, transparent aufgeschlüsselt nach Börsenpreis, Netzentgelten und Mehrwertsteuer?
3. **Wie viel Geld spart man** im Vergleich zum sofortigen Laden?

---

## 🛡️ Multi-User & Read-Only Architektur (SaaS-Betrieb)

Akkudoktor EOS ist von Haus aus ein lokales Einzelsystem ohne Benutzerverwaltung. Der **EOS Smart Charge Planner** macht daraus einen sicheren Onlineservice:

* **Zentraler Börsenpreis-Feed:** Eine einzige EOS-Instanz holt die EPEX-Spot-Auktion für die gesamte Preiszone (DE-LU bzw. AT) und cacht diese im Arbeitsspeicher.
* **100 % Privatsphäre via LocalStorage:** Fahrzeugdaten (Kapazität, Ladeleistung, SoC), individuelle Netzentgelte (0–23h) und der MwSt.-Satz werden **ausschließlich im Browser des Nutzers** gespeichert.
* **Keine Benutzerdatenbank nötig:** Völlig DSGVO-konform, keine Registrierung, keine Passwörter, keine Cookies.
* **Sicherer Read-Only-Schutzschild (`READ_ONLY_MODE=true`):**
  * EOS-Schreibbefehle (`PUT /v1/config`, `POST /v1/admin/*`) sind komplett gesperrt.
  * In Docker läuft EOS auf einem internen, isolierten Netzwerk **ohne offene Ports nach außen**.

```mermaid
flowchart LR
    Internet((Internet / Nutzer)) -->|Öffentlich: Port 443 / 8505| UI[Smart Charge UI / Proxy]
    UI -->|Nur intern im Docker-Netzwerk: Port 8503| EOS[(EOS Server - Read Only)]
    style EOS fill:#1e293b,stroke:#ef4444,stroke-width:2px
    style UI fill:#0f172a,stroke:#10b981,stroke-width:2px
```

---

## ✨ Features

* **⚡️ Börsenpreise & 48h-Vorhersage:** Direkte Anbindung an Day-Ahead-Spotmarktpreise via EOS.
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
./start.sh
```

Öffnen Sie anschließend **[http://localhost:8505](http://localhost:8505)** im Browser.

### Option 2: Produktion / Onlineservice mit Docker Compose

Startet EOS isoliert und stellt nur die Smart Charge UI nach außen bereit:

```bash
docker compose up -d
```

---

## ⚙️ Umgebungsvariablen

| Variable | Standard | Beschreibung |
|---|---|---|
| `READ_ONLY_MODE` | `true` | Sperrt EOS-Schreibzugriffe und aktiviert LocalStorage-Betrieb für öffentliche Deployments. |
| `EOS_API_URL` | `http://127.0.0.1:8503` | Adresse des Akkudoktor EOS Servers. |
| `SMART_CHARGE_PORT` | `8505` | Port für die Webanwendung. |
| `ADMIN_TOKEN` | *leer* | Optionaler Admin-Token für Konfigurationsänderungen via API. |

---

## 🛠 REST-API Endpunkte

| Methode | Endpunkt | Beschreibung |
|---|---|---|
| `GET` | `/` | Responsive Web-App |
| `GET` | `/api/system` | Systemstatus, Read-Only-Modus & EOS-Verbindungsstatus |
| `GET` | `/api/prices` | 15-Minuten-Börsenpreisreihe mit Netto-/Brutto-Aufschlüsselung (gecacht) |
| `POST` | `/api/plan` | Berechnet optimales Ladefenster rein im Arbeitsspeicher (Memory-Only) |
| `GET` | `/api/config/provider` | Ruft Standard-Tarifkonfiguration ab |
| `POST` | `/api/config/provider` | *(Gesperrt im Read-Only-Modus)* Server-Konfiguration anpassen |

---

## 📄 Lizenz

MIT License — Frei verwendbar und anpassbar.
Basierend auf der Open-Source-Software [Akkudoktor-EOS](https://github.com/Akkudoktor-EOS/EOS).
