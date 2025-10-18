# bhv_distance_app.py
# Vereins-Entfernungen & Fahrzeiten — Google Distance Matrix (robust bis N x N via Chunking)

import io
import math
import re
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

# Optional: googlemaps, sonst fallback auf requests
try:
    import googlemaps
    GMAPS_AVAILABLE = True
except Exception:
    GMAPS_AVAILABLE = False

import requests

# ----------------------------
# Streamlit Grundkonfiguration
# ----------------------------
st.set_page_config(page_title="Vereins-Entfernungen (Google Distance Matrix)", layout="wide")

st.title("Vereins-Entfernungen & Fahrzeiten — Google Distance Matrix")
st.markdown(
    """
Diese App erzeugt Entfernungs- (km) und Fahrzeit- (min) Matrizen zwischen allen hochgeladenen Vereinen.
Datenquelle: **Google Maps Platform** (Geocoding + Distance Matrix)
"""
)

# ----------------------------
# Einstellungen
# ----------------------------
with st.expander("Google API-Einstellungen", expanded=True):
    api_key = st.text_input(
        "Google Maps API Key",
        type="password",
        help="Key in der Google Cloud Console anlegen & beschränken (APIs: Geocoding + Distance Matrix).",
    )
    travel_mode = st.selectbox("Verkehrsmittel", ["driving", "walking", "bicycling", "transit"], index=0)
    units = st.selectbox("Einheiten", ["metric", "imperial"], index=0)
    use_traffic = st.checkbox(
        "Realtime-Traffic (nur driving, benötigt departure_time=now)",
        value=True if travel_mode == "driving" else False,
    )
    rate_limit_delay = st.number_input(
        "Wartezeit zwischen API-Batches (Sekunden)",
        min_value=0.0,
        value=0.15,
        step=0.05,
        help="Sanftes Throttling zwischen Chunk-Requests.",
    )

st.markdown("---")
uploaded = st.file_uploader(
    "Excel hochladen (z. B. mit Spalten: Verein, Adresse, PLZ, Ort)",
    type=["xlsx"],
)

# ---------------------------------
# Session-Cache für Geocodingdaten
# ---------------------------------
if "geocode_cache" not in st.session_state:
    # key: address string, value: (lat, lng) oder None
    st.session_state["geocode_cache"] = {}

# ---------------------------------
# Hilfsfunktionen
# ---------------------------------
def normalize_zip(val: object) -> str:
    """PLZ in 5-stelligen String normalisieren (keine .0, keine Leerzeichen)."""
    if pd.isna(val):
        return ""
    s = str(val).strip()
    s = re.sub(r"\.0$", "", s)  # 34225.0 -> 34225
    s = re.sub(r"[^\d]", "", s)  # nur Ziffern
    if len(s) == 4:
        s = "0" + s
    if len(s) == 5:
        return s
    # ungültig -> leer
    return s

def make_unique_labels(labels: List[str]) -> List[str]:
    """Duplikate in Index/Spaltennamen eindeutig machen, z. B. 'Verein', 'Verein (2)', ..."""
    seen = {}
    out = []
    for lab in labels:
        key = lab
        if key not in seen:
            seen[key] = 1
            out.append(key)
        else:
            seen[key] += 1
            out.append(f"{key} ({seen[key]})")
    return out

def google_geocode(address: str, key: str) -> Optional[Tuple[float, float]]:
    """Geocode Adresse -> (lat, lng), mit Session-Cache."""
    if not address:
        return None
    if address in st.session_state["geocode_cache"]:
        return st.session_state["geocode_cache"][address]

    try:
        if GMAPS_AVAILABLE:
            client = googlemaps.Client(key=key)
            res = client.geocode(address)
            if res:
                loc = res[0]["geometry"]["location"]
                st.session_state["geocode_cache"][address] = (loc["lat"], loc["lng"])
                return (loc["lat"], loc["lng"])
            # ZERO_RESULTS etc.
            st.session_state["geocode_cache"][address] = None
            return None
        else:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {"address": address, "key": key, "language": "de"}
            r = requests.get(url, params=params, timeout=20)
            data = r.json()
            status = data.get("status", "UNKNOWN")
            if status == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                st.session_state["geocode_cache"][address] = (loc["lat"], loc["lng"])
                return (loc["lat"], loc["lng"])
            elif status in {"REQUEST_DENIED", "INVALID_REQUEST"}:
                # Key/Request-Problem -> sofort anzeigen
                msg = data.get("error_message", "(keine Fehlermeldung von Google)")
                st.error(f"Geocoding abgelehnt: {status} – {msg}")
            else:
                # ZERO_RESULTS, OVER_QUERY_LIMIT, etc. -> warnen
                st.warning(f"Geocoding-Fehler für '{address}': {status} – {data.get('error_message')}")
    except Exception as e:
        st.warning(f"Geocoding-Fehler für '{address}': {e}")

    st.session_state["geocode_cache"][address] = None
    return None

# Distance Matrix – robuste Chunking-Parameter
MAX_ELEMENTS_PER_REQUEST = 100   # sicheres Limit, unabhängig von 25x25 theoretisch
MAX_DIM_PER_REQUEST = 25         # Google-API-Hardcap pro Dimension
MAX_RETRIES = 6
BASE_SLEEP = 0.6  # Sekunden

def _compute_chunk_dims(n_orig: int, n_dest: int) -> Tuple[int, int]:
    """Wähle Chunk-Dimensionen so, dass o*d ≤ MAX_ELEMENTS_PER_REQUEST und jeweils ≤ MAX_DIM_PER_REQUEST."""
    o = min(n_orig, MAX_DIM_PER_REQUEST)
    d = min(n_dest, MAX_DIM_PER_REQUEST)
    while o * d > MAX_ELEMENTS_PER_REQUEST:
        if o >= d and o > 1:
            o -= 1
        elif d > 1:
            d -= 1
        else:
            break
    return max(o, 1), max(d, 1)

def _dm_request_with_backoff(url: str, params: dict) -> dict:
    """GET mit exponentiellem Backoff; wertet DistanceMatrix-Status korrekt aus."""
    for attempt in range(MAX_RETRIES):
        r = requests.get(url, params=params, timeout=60)
        # Rate/Server-Fehler -> Backoff
        if r.status_code in (429, 500, 503):
            time.sleep(BASE_SLEEP * (2 ** attempt))
            continue
        try:
            data = r.json()
        except Exception:
            data = {"status": "UNKNOWN_ERROR", "error_message": "Invalid JSON"}

        status = data.get("status", "OK")
        if status == "OK":
            return data
        if status in {"OVER_QUERY_LIMIT", "RESOURCE_EXHAUSTED"}:
            time.sleep(BASE_SLEEP * (2 ** attempt))
            continue
        if status in {"REQUEST_DENIED", "INVALID_REQUEST"}:
            raise RuntimeError(f"DistanceMatrix abgelehnt: {status} – {data.get('error_message')}")
        # unbekannt -> erneut versuchen
        time.sleep(BASE_SLEEP * (2 ** attempt))

    # letzter Versuch
    r = requests.get(url, params=params, timeout=60)
    try:
        data = r.json()
    except Exception:
        data = {"status": "UNKNOWN_ERROR", "error_message": "Invalid JSON (final)"}
    if data.get("status", "OK") != "OK":
        raise RuntimeError(f"DistanceMatrix dauerhafter Fehler: {data.get('status')} – {data.get('error_message')}")
    return data

def distance_matrix_batch(
    origins: List[str],
    destinations: List[str],
    key: str,
    mode: str = "driving",
    units: str = "metric",
    use_traffic: bool = False,
    rate_limit_delay: float = 0.1,
) -> Dict[Tuple[str, str], Dict]:
    """Baut die Matrix in Chunks (≤100 Elements/Request), Backoff bei Limits."""
    results: Dict[Tuple[str, str], Dict] = {}
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"

    # Chunkgrößen dynamisch errechnen
    o_chunk_size, d_chunk_size = _compute_chunk_dims(len(origins), len(destinations))

    def chunk(seq, n):
        for i in range(0, len(seq), n):
            yield seq[i : i + n]

    # Optional: Progress
    total_requests = math.ceil(len(origins) / o_chunk_size) * math.ceil(len(destinations) / d_chunk_size)
    step = 0
    progress = st.progress(0.0, text="Berechne Distanzmatrix…")

    for o_chunk in chunk(origins, o_chunk_size):
        origins_param = "|".join(o_chunk)
        for d_chunk in chunk(destinations, d_chunk_size):
            params = {
                "key": key,
                "mode": mode,
                "units": units,
                "origins": origins_param,
                "destinations": "|".join(d_chunk),
                "language": "de",
            }
            if use_traffic and mode == "driving":
                params["departure_time"] = "now"

            try:
                data = _dm_request_with_backoff(url, params)
                rows = data.get("rows", [])
                for i, row in enumerate(rows):
                    elements = row.get("elements", [])
                    for j, elem in enumerate(elements):
                        origin = o_chunk[i]
                        dest = d_chunk[j]
                        status = elem.get("status")
                        if status == "OK":
                            dist_m = elem["distance"]["value"]
                            # Traffic bevorzugen, falls vorhanden
                            dur_s = elem.get("duration_in_traffic", elem.get("duration", {})).get("value")
                            if dur_s is None and "duration" in elem:
                                dur_s = elem["duration"]["value"]
                            results[(origin, dest)] = {
                                "distance_m": dist_m,
                                "duration_s": dur_s,
                                "status": status,
                            }
                        else:
                            results[(origin, dest)] = {
                                "distance_m": None,
                                "duration_s": None,
                                "status": status,
                            }
                time.sleep(rate_limit_delay)
            except Exception as e:
                for origin in o_chunk:
                    for dest in d_chunk:
                        results[(origin, dest)] = {
                            "distance_m": None,
                            "duration_s": None,
                            "status": f"ERROR: {e}",
                        }
                time.sleep(rate_limit_delay)

            step += 1
            progress.progress(step / total_requests, text=f"Berechne Distanzmatrix… ({step}/{total_requests})")

    progress.empty()
    return results

def build_full_address(df: pd.DataFrame, col_map: Dict[str, Optional[str]]) -> pd.Series:
    """Baut 'full_address' im Format 'Straße Hausnr, PLZ Ort, Land' (Land default Deutschland)."""
    def col_or_empty(name: Optional[str]) -> pd.Series:
        if name and name in df.columns:
            return df[name].astype(str).fillna("").str.strip()
        return pd.Series([""] * len(df), index=df.index)

    street_s = col_or_empty(col_map.get("street"))
    zip_s = col_or_empty(col_map.get("zip")).apply(normalize_zip)
    city_s = col_or_empty(col_map.get("city"))

    # Land: optional, default "Deutschland"
    if col_map.get("country") and col_map["country"] in df.columns:
        country_s = df[col_map["country"]].astype(str).fillna("").str.strip().replace("", "Deutschland")
    else:
        country_s = pd.Series(["Deutschland"] * len(df), index=df.index)

    out = []
    for i in range(len(df)):
        street = street_s.iat[i] if i < len(street_s) else ""
        zipc = zip_s.iat[i] if i < len(zip_s) else ""
        city = city_s.iat[i] if i < len(city_s) else ""
        country = country_s.iat[i] if i < len(country_s) else "Deutschland"
        segs = []
        if street:
            segs.append(street)
        place = " ".join([p for p in [zipc, city] if p]).strip()
        if place:
            segs.append(place)
        if country:
            segs.append(country)
        out.append(", ".join(segs))
    return pd.Series(out, index=df.index, name="full_address")

def to_latlng_string(lat: float, lng: float) -> str:
    return f"{lat},{lng}"

# ----------------------------
# Haupt-UI: Datei laden
# ----------------------------
if uploaded is not None:
    df = pd.read_excel(uploaded)
    st.success(f"Datei geladen: {uploaded.name} — {len(df)} Zeilen")

    # Spaltenzuordnung
    with st.expander("Spalten zuordnen / Adresse bauen", expanded=True):
        st.write("Wähle die Spalten aus deiner Tabelle:")
        name_col = st.selectbox(
            "Vereinsname",
            options=df.columns.tolist(),
            index=0 if "Verein" not in df.columns else df.columns.get_loc("Verein"),
        )
        street_col = st.selectbox(
            "Straße/Hausnr.",
            options=[None] + df.columns.tolist(),
            index=0 if "Adresse" not in df.columns else df.columns.get_loc("Adresse") + 1,
        )
        zip_col = st.selectbox(
            "PLZ",
            options=[None] + df.columns.tolist(),
            index=0 if "PLZ" not in df.columns else df.columns.get_loc("PLZ") + 1,
        )
        city_col = st.selectbox(
            "Ort",
            options=[None] + df.columns.tolist(),
            index=0 if "Ort" not in df.columns else df.columns.get_loc("Ort") + 1,
        )
        country_col = st.selectbox(
            "Land (optional, default DE)",
            options=[None] + df.columns.tolist(),
            index=0 if "Land" not in df.columns else df.columns.get_loc("Land") + 1,
        )

        if "full_address" in df.columns:
            prebuilt = st.checkbox("Vorhandene Spalte 'full_address' verwenden", value=False)
        else:
            prebuilt = False

        if prebuilt:
            addresses = df["full_address"].astype(str).tolist()
        else:
            addresses = build_full_address(
                df, {"street": street_col, "zip": zip_col, "city": city_col, "country": country_col}
            ).tolist()

    if not api_key:
        st.warning("Bitte oben deinen Google Maps API Key eingeben, um fortzufahren.")
        st.stop()

    # ---------------------------------
    # Geocoding
    # ---------------------------------
    st.subheader("Geocoding")
    left, right = st.columns([3, 2])

    with left:
        st.write("Schritt 1: Koordinaten ermitteln (lat/lng). Die Ergebnisse werden zwischengespeichert.")
        if st.button("Geocoding starten"):
            coords = []
            progress = st.progress(0, text="Geocoding läuft...")
            for idx, addr in enumerate(addresses):
                coords.append(google_geocode(addr, api_key))
                if len(addresses) > 0:
                    progress.progress((idx + 1) / len(addresses), text=f"Geocoding {idx+1}/{len(addresses)}")
            progress.empty()
            df["latlng"] = [to_latlng_string(c[0], c[1]) if c else None for c in coords]
            st.success("Geocoding abgeschlossen.")
            st.dataframe(df[[name_col, "latlng"]])

    with right:
        st.write("Cache-Optionen")
        if st.session_state["geocode_cache"]:
            cache_df = pd.DataFrame(
                [(k, v[0], v[1]) for k, v in st.session_state["geocode_cache"].items() if v is not None],
                columns=["address", "lat", "lng"],
            )
            buf = io.BytesIO()
            cache_df.to_csv(buf, index=False)
            st.download_button(
                "Geocode-Cache als CSV herunterladen",
                data=buf.getvalue(),
                file_name="geocode_cache.csv",
                mime="text/csv",
            )

        cache_file = st.file_uploader("Geocode-Cache CSV laden (address,lat,lng)", type=["csv"], key="cacheu")
        if cache_file is not None:
            imp = pd.read_csv(cache_file)
            for _, row in imp.iterrows():
                st.session_state["geocode_cache"][row["address"]] = (float(row["lat"]), float(row["lng"]))
            st.success(f"{len(imp)} gecachte Koordinaten importiert.")

    st.markdown("---")
    st.subheader("Distance Matrix (alle gegen alle)")

    sample_n = st.number_input(
        "Optional: nur die ersten N Vereine berechnen (zum Testen)",
        min_value=0,
        value=0,
        step=1,
        help="0 = alle",
    )
    names = df[name_col].astype(str).fillna("").str.strip().tolist()

    # Token (lat,lng) bevorzugen, sonst Rohadresse
    if "latlng" in df.columns and df["latlng"].notna().any():
        origin_tokens = [df["latlng"].iloc[i] if pd.notna(df["latlng"].iloc[i]) else addresses[i] for i in range(len(df))]
    else:
        origin_tokens = addresses

    if sample_n and sample_n > 0:
        names = names[:sample_n]
        origin_tokens = origin_tokens[:sample_n]

    # Duplikate in Namen vermeiden (sonst PyArrow/Anzeige-Probleme)
    names_unique = make_unique_labels(names)

    if st.button("Entfernungen & Zeiten berechnen"):
        origins = origin_tokens
        destinations = origin_tokens

        st.info("Sende Anfragen an Google Distance Matrix API (Chunking ≤ 100 Elemente/Request)…")
        res = distance_matrix_batch(
            origins=origins,
            destinations=destinations,
            key=api_key,
            mode=travel_mode,
            units=units,
            use_traffic=use_traffic,
            rate_limit_delay=rate_limit_delay,
        )

        # Leere DataFrames mit eindeutigen Labels
        dist_km = pd.DataFrame(index=names_unique, columns=names_unique, dtype=float)
        time_min = pd.DataFrame(index=names_unique, columns=names_unique, dtype=float)
        status_tbl = pd.DataFrame(index=names_unique, columns=names_unique, dtype=object)

        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                r = res.get((o, d), {"distance_m": None, "duration_s": None, "status": "N/A"})
                if r["distance_m"] is not None:
                    if units == "metric":
                        km = r["distance_m"] / 1000.0
                    else:
                        km = r["distance_m"] / 1609.344
                    dist_km.iat[i, j] = round(km, 2)
                else:
                    dist_km.iat[i, j] = None

                if r["duration_s"] is not None:
                    minutes = r["duration_s"] / 60.0
                    time_min.iat[i, j] = round(minutes, 1)
                else:
                    time_min.iat[i, j] = None

                status_tbl.iat[i, j] = r["status"]

        for i in range(len(names_unique)):
            dist_km.iat[i, i] = 0.0
            time_min.iat[i, i] = 0.0
            status_tbl.iat[i, i] = "OK"

        st.success("Berechnung abgeschlossen.")
        st.write("Distanzmatrix (km)")
        st.dataframe(dist_km)
        st.write("Zeitmatrix (min)")
        st.dataframe(time_min)
        st.write("Status (Debug)")
        st.dataframe(status_tbl)

        # Export
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            meta = pd.DataFrame(
                {"Einstellungen": ["mode", "units", "use_traffic"], "Wert": [travel_mode, units, str(use_traffic)]}
            )
            meta.to_excel(writer, sheet_name="Einstellungen", index=False)
            pd.DataFrame({"Name": names_unique, "Token": origins}).to_excel(writer, sheet_name="Vereine", index=False)
            dist_km.to_excel(writer, sheet_name="Distanz_km")
            time_min.to_excel(writer, sheet_name="Fahrzeit_min")
            status_tbl.to_excel(writer, sheet_name="Status")
        st.download_button(
            "Excel herunterladen",
            data=buf.getvalue(),
            file_name="Entfernungen_und_Fahrzeiten.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

st.markdown("---")
with st.expander("Hinweise & Limits"):
    st.markdown(
        """
- **Kontingente & Kosten**: Google berechnet pro Anfrage/Element. Große Matrizen erzeugen viele Elemente.
- **Automatisches Chunking**: Die App hält **≤ 100 Elemente pro Request** (z. B. 10×10), unabhängig von der Gesamtgröße.
- **Hardcap pro Dimension**: max. **25 Origins** bzw. **25 Destinations** je Einzelanfrage (Google Limit) wird eingehalten.
- **Traffic-Zeiten**: Nur bei *driving* und mit `departure_time=now` sinnvoll (Schwankungen möglich).
- **Geocode-Cache**: Import/Export vermeidet doppelte Geocoding-Kosten.
- **Saubere Adressen**: PLZ wird als 5-stelliger String normalisiert; Land default = Deutschland.
"""
    )
