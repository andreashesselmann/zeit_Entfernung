
import io
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st


try:
    import googlemaps
    GMAPS_AVAILABLE = True
except Exception:
    GMAPS_AVAILABLE = False
import requests

st.set_page_config(page_title="Vereins-Entfernungen (Google Distance Matrix)", layout="wide")

st.title("Vereins-Entfernungen & Fahrzeiten — Google Distance Matrix")
st.markdown(
    '''
    Diese App erzeugt Entfernungs- (km) und Fahrzeit- (min) Matrizen zwischen allen hochgeladenen Vereinen.
    Datenquelle: Google Maps Platform (Geocoding + Distance Matrix)
    '''
)

with st.expander("Google API-Einstellungen", expanded=True):
    api_key = st.text_input("Google Maps API Key", type="password", help="Key in der Google Cloud Console anlegen & beschränken")
    travel_mode = st.selectbox("Verkehrsmittel", ["driving", "walking", "bicycling", "transit"], index=0)
    units = st.selectbox("Einheiten", ["metric", "imperial"], index=0)
    use_traffic = st.checkbox("Realtime-Traffic (nur driving, benötigt departure_time=now)", value=True if travel_mode=="driving" else False)
    rate_limit_delay = st.number_input("Wartezeit zwischen API-Batches (Sekunden)", min_value=0.0, value=0.1, step=0.1)

st.markdown("---")
uploaded = st.file_uploader("Excel hochladen (z. B. mit Spalten: Verein, Adresse, PLZ, Ort)", type=["xlsx"])

if "geocode_cache" not in st.session_state:
    st.session_state["geocode_cache"] = {}  # key: address string, value: (lat, lng)

def google_geocode(address: str, key: str) -> Optional[Tuple[float, float]]:
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
        else:
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {"address": address, "key": key, "language": "de"}
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json()
            if data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                st.session_state["geocode_cache"][address] = (loc["lat"], loc["lng"])
                return (loc["lat"], loc["lng"])
    except Exception as e:
        st.warning(f"Geocoding-Fehler für '{address}': {e}")
    st.session_state["geocode_cache"][address] = None
    return None

def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i : i + size]

def distance_matrix_batch(origins: List[str], destinations: List[str], key: str, mode="driving", units="metric", use_traffic=False, rate_limit_delay=0.1) -> Dict[Tuple[str, str], Dict]:
    results: Dict[Tuple[str, str], Dict] = {}
    max_origins = 25
    max_destinations = 25
    for o_chunk in chunk_list(origins, max_origins):
        for d_chunk in chunk_list(destinations, max_destinations):
            params = {
                "key": key,
                "mode": mode,
                "units": units,
                "origins": "|".join(o_chunk),
                "destinations": "|".join(d_chunk),
            }
            if use_traffic and mode == "driving":
                params["departure_time"] = "now"
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            try:
                r = requests.get(url, params=params, timeout=60)
                r.raise_for_status()
                data = r.json()
                rows = data.get("rows", [])
                for i, row in enumerate(rows):
                    for j, elem in enumerate(row.get("elements", [])):
                        origin = o_chunk[i]
                        dest = d_chunk[j]
                        status = elem.get("status")
                        if status == "OK":
                            dist_m = elem["distance"]["value"]
                            dur_s = elem["duration"]["value"]
                            results[(origin, dest)] = {"distance_m": dist_m, "duration_s": dur_s, "status": status}
                        else:
                            results[(origin, dest)] = {"distance_m": None, "duration_s": None, "status": status}
                time.sleep(rate_limit_delay)
            except Exception as e:
                for origin in o_chunk:
                    for dest in d_chunk:
                        results[(origin, dest)] = {"distance_m": None, "duration_s": None, "status": f"ERROR: {e}"}
                time.sleep(rate_limit_delay)
    return results

def build_full_address(df: pd.DataFrame, col_map):
    def col_or_empty(name):
        return df[name].astype(str).str.strip() if name and name in df.columns else pd.Series([""] * len(df), index=df.index)

    street_s = col_or_empty(col_map.get("street"))
    zip_s    = col_or_empty(col_map.get("zip"))
    city_s   = col_or_empty(col_map.get("city"))
    # country: falls Spalte fehlt oder leer -> "Deutschland"
    if col_map.get("country") and col_map.get("country") in df.columns:
        country_s = df[col_map["country"]].astype(str).str.strip().fillna("")
    else:
        country_s = pd.Series([""] * len(df), index=df.index)

    full = []
    for i in range(len(df)):
        street  = street_s.iat[i] if i < len(street_s) else ""
        zipc    = zip_s.iat[i]    if i < len(zip_s)    else ""
        city    = city_s.iat[i]   if i < len(city_s)   else ""
        country = country_s.iat[i] if i < len(country_s) else ""
        if not country:
            country = "Deutschland"

        segs = []
        if street:
            segs.append(street)
        place_parts = [p for p in [zipc, city] if p]
        if place_parts:
            segs.append(" ".join(place_parts))
        if country:
            segs.append(country)

        full.append(", ".join(segs))

    return pd.Series(full, index=df.index, name="full_address")

def to_latlng_string(lat: float, lng: float) -> str:
    return f"{lat},{lng}"

if uploaded is not None:
    df = pd.read_excel(uploaded)
    st.success(f"Datei geladen: {uploaded.name} — {len(df)} Zeilen")

    with st.expander("Spalten zuordnen / Adresse bauen", expanded=True):
        st.write("Wähle die Spalten aus deiner Tabelle:")
        name_col = st.selectbox("Vereinsname", options=df.columns.tolist(), index=0 if "Verein" not in df.columns else df.columns.get_loc("Verein"))
        street_col = st.selectbox("Straße/Hausnr.", options=[None] + df.columns.tolist(), index=0 if "Adresse" not in df.columns else df.columns.get_loc("Adresse")+1)
        zip_col = st.selectbox("PLZ", options=[None] + df.columns.tolist(), index=0 if "PLZ" not in df.columns else df.columns.get_loc("PLZ")+1)
        city_col = st.selectbox("Ort", options=[None] + df.columns.tolist(), index=0 if "Ort" not in df.columns else df.columns.get_loc("Ort")+1)
        country_col = st.selectbox("Land (optional, default DE)", options=[None] + df.columns.tolist(), index=0 if "Land" not in df.columns else df.columns.get_loc("Land")+1)

        if "full_address" in df.columns:
            prebuilt = st.checkbox("Vorhandene Spalte 'full_address' verwenden", value=False)
        else:
            prebuilt = False

        if prebuilt:
            addresses = df["full_address"].astype(str).tolist()
        else:
            addresses = build_full_address(df, {"street": street_col, "zip": zip_col, "city": city_col, "country": country_col}).tolist()

    if not api_key:
        st.warning("Bitte oben deinen Google Maps API Key eingeben, um fortzufahren.")
        st.stop()

    st.subheader("Geocoding")
    left, right = st.columns([3,2])

    with left:
        st.write("Schritt 1: Koordinaten ermitteln (lat/lng). Die Ergebnisse werden zwischengespeichert.")
        if st.button("Geocoding starten"):
            coords = []
            progress = st.progress(0, text="Geocoding läuft...")
            for idx, addr in enumerate(addresses):
                coords.append(google_geocode(addr, api_key))
                if len(addresses) > 0:
                    progress.progress((idx+1)/len(addresses), text=f"Geocoding {idx+1}/{len(addresses)}")
            progress.empty()
            df["latlng"] = [to_latlng_string(c[0], c[1]) if c else None for c in coords]
            st.success("Geocoding abgeschlossen.")
            st.dataframe(df[[name_col, "latlng"]])

    with right:
        st.write("Cache-Optionen")
        if st.session_state["geocode_cache"]:
            cache_df = pd.DataFrame([(k, v[0], v[1]) for k, v in st.session_state["geocode_cache"].items() if v is not None], columns=["address", "lat", "lng"])
            buf = io.BytesIO()
            cache_df.to_csv(buf, index=False)
            st.download_button("Geocode-Cache als CSV herunterladen", data=buf.getvalue(), file_name="geocode_cache.csv", mime="text/csv")
        cache_file = st.file_uploader("Geocode-Cache CSV laden (address,lat,lng)", type=["csv"], key="cacheu")
        if cache_file is not None:
            imp = pd.read_csv(cache_file)
            for _, row in imp.iterrows():
                st.session_state["geocode_cache"][row["address"]] = (float(row["lat"]), float(row["lng"]))
            st.success(f"{len(imp)} gecachte Koordinaten importiert.")

    st.markdown("---")
    st.subheader("Distance Matrix (alle gegen alle)")

    sample_n = st.number_input("Optional: nur die ersten N Vereine berechnen (zum Testen)", min_value=0, value=0, step=1, help="0 = alle")
    names = df[name_col].astype(str).tolist()

    if "latlng" in df.columns and df["latlng"].notna().any():
        origin_tokens = [df["latlng"].iloc[i] if pd.notna(df["latlng"].iloc[i]) else addresses[i] for i in range(len(df))]
    else:
        origin_tokens = addresses

    if sample_n and sample_n > 0:
        names = names[:sample_n]
        origin_tokens = origin_tokens[:sample_n]

    if st.button("Entfernungen & Zeiten berechnen"):
        origins = origin_tokens
        destinations = origin_tokens

        st.info("Sende Anfragen an Google Distance Matrix API...")
        res = distance_matrix_batch(
            origins=origins,
            destinations=destinations,
            key=api_key,
            mode=travel_mode,
            units=units,
            use_traffic=use_traffic,
            rate_limit_delay=rate_limit_delay
        )

        dist_km = pd.DataFrame(index=names, columns=names, dtype=float)
        time_min = pd.DataFrame(index=names, columns=names, dtype=float)
        status_tbl = pd.DataFrame(index=names, columns=names, dtype=object)

        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                r = res.get((o, d), {"distance_m": None, "duration_s": None, "status": "N/A"})
                if r["distance_m"] is not None:
                    km = r["distance_m"] / 1000.0 if units == "metric" else r["distance_m"] / 1609.344
                    dist_km.iat[i, j] = round(km, 2)
                else:
                    dist_km.iat[i, j] = None
                if r["duration_s"] is not None:
                    minutes = r["duration_s"] / 60.0
                    time_min.iat[i, j] = round(minutes, 1)
                else:
                    time_min.iat[i, j] = None
                status_tbl.iat[i, j] = r["status"]

        for i in range(len(names)):
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

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            meta = pd.DataFrame({"Einstellungen": ["mode", "units", "use_traffic"], "Wert": [travel_mode, units, str(use_traffic)]})
            meta.to_excel(writer, sheet_name="Einstellungen", index=False)
            pd.DataFrame({name_col: names, "Token": origins}).to_excel(writer, sheet_name="Vereine", index=False)
            dist_km.to_excel(writer, sheet_name="Distanz_km")
            time_min.to_excel(writer, sheet_name="Fahrzeit_min")
            status_tbl.to_excel(writer, sheet_name="Status")
        st.download_button("Excel herunterladen", data=buf.getvalue(), file_name="Entfernungen_und_Fahrzeiten.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.markdown("---")
with st.expander("Hinweise & Limits"):
    st.markdown(
        '''
        - Kontingente & Kosten: Google berechnet pro Anfrage. Grosse Matrizen bedeuten viele API-Elemente.
        - Limits pro Request: bis 25x25 Elemente (625) pro Request. Die App chunked automatisch.
        - Traffic-Zeiten: Nur fuer driving und mit departure_time=now sinnvoll.
        - Caching: Import/Export des Geocode-Caches vermeidet doppelte Geocoding-Kosten.
        - Datensparsamkeit: API-Key bleibt lokal in der Session.
        '''
    )
