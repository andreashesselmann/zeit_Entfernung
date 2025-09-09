
# Vereins-Entfernungen & Fahrzeiten — Streamlit App

Diese App berechnet Entfernungs- und Fahrzeit-Matrizen zwischen allen Vereinen in einer Excel-Datei mittels Google Maps Distance Matrix API.

## Features
- Excel-Upload
- Flexible Spaltenzuordnung (Name, Straße, PLZ, Ort)
- Geocoding mit Cache (Import/Export)
- Distance Matrix alle gegen alle (km & min), inkl. Traffic (optional)
- Excel-Export mit mehreren Sheets

## Nutzung
1. Python 3.10+ installieren
2. Abhängigkeiten installieren:
   ```bash
   pip install -r requirements.txt
   ```
3. App starten:
   ```bash
   streamlit run bhv_distance_app.py
   ```
4. Im Browser: Excel hochladen, Google API Key eingeben, Spalten zuordnen, geokodieren, berechnen, exportieren.

## Google API
- APIs aktivieren: Geocoding API & Distance Matrix API
- API-Key: In der Google Cloud Console anlegen und einschränken (HTTP-Referer/Domain oder IP).
- Kosten: Bitte die aktuellen Preise in der Cloud Console prüfen.

## Hinweise
- Google Limits: bis zu 25 Origins x 25 Destinations pro Request (625 Elemente). Die App chunked automatisch.
- Fahrzeiten hängen von Verkehr & Abfahrtszeit ab (für driving mit departure_time=now).
