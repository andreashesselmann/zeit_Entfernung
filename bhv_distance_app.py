# bhv_distance_app.py
# Vereins-Entfernungen & Fahrzeiten — Google Distance Matrix API v1 (JSON, POST)
# - Garantiert <= 100 Elemente pro Request (Block-Schnitt)
# - Arbeitet alle Blocks ab, bis die komplette Matrix gefüllt ist
# - Robustes Geocoding mit Cache
# - Optional: Quota-Fenster (z. B. 100 Elemente / 60 s)
# - Excel-Export

import io
import math
import re
import time
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

# ------------------------------ Streamlit UI ---------------------------------
st.set_page_config(page_title="Vereins-Entfernungen (v1, max 100/Request)", layout="wide")
st.title("Vereins-Entfernungen & Fahrzeiten — Distance Matrix API v1 (POST, ≤100/Request)")
st.caption("Jede Anfrage enthält maximal 100 Elemente. Die App schneidet die Matrix in Blöcke und sendet automatisch mehrere Anfragen, bis alles berechnet ist.")

with st.expander("Google API-Einstellungen", expanded=True):
    api_key = st.text_input(
        "Google API Key", type="password",
        help="In der Google Cloud Console anlegen. APIs aktiv: Geocoding API & Distance Matrix API."
    )
    travel_mode = st.selectbox("Verkehrsmittel", ["driving", "walking", "bicycling", "transit"], index=0)
    use_departure_now = st.checkbox("Realtime (departure_time=now; nur driving/transit sinnvoll)", value=False)
    units_display = st.selectbox("Einheiten (Anzeige)", ["metric", "imperial"], index=0)

with st.expander("Erweitert (Drosselung & Stabilität)", expanded=False):
    base_sleep = st.number_input("Basis-Wartezeit zwischen Requests (s)", 0.0, 5.0, 0.25, 0.05)
    per_element_delay = st.number_input(
        "Zusatzwartezeit pro Element (s)", 0.0, 0.02, 0.0015, 0.0005,
        help="Elemente = Origins_in_Block × Destinations_in_Block"
    )
    max_retries = st.number_input("Max. Retries je Block (429/5xx/UNKNOWN_ERROR)", 0, 10, 5, 1)
    debug_headers = st.checkbox("Debug: Header/Antwortlängen & Top-Status loggen", value=False)

with st.expander("Quota-Fenster (optional)", expanded=False):
    use_quota_window = st.checkbox("Quota-Fenster aktivieren (Elemente/Zeitraum)", value=False)
    quota_elements = st.number_input("Max. Elemente pro Fenster", min_value=1, value=100, step=10)
    quota_window_s = st.number_input("Fensterlänge (Sekunden)", min_value=10, value=60, step=5)

st.markdown("---")
uploaded = st.file_uploader("Excel hochladen (z. B. Spalten: Verein, Straße/Hausnr., PLZ, Ort)", type=["xlsx"])

# ------------------------------ Geocoding Cache -------------------------------
if "geocode_cache" not in st.session_state:
    st.session_state["geocode_cache"] = {}  # address -> (lat,lng) (nur positive Ergebnisse)

LATLNG_RE = re.compile(r"^\s*-?\d{1,2}\.\d+,\s*-?\d{1,3}\.\d+\s*$")

def normalize_zip(val) -> str:
    s = str(val).strip()
    s = re.sub(r"\.0$", "", s)
    s = re.sub(r"[^\d]", "", s)
    if len(s) == 4: 
        s = "0" + s
    return s

def build_full_address(df: pd.DataFrame, col_map) -> pd.Series:
    def col_or_empty(name: Optional[str]) -> pd.Series:
        if name and name in df.columns:
            return df[name].astype(str).fillna("").str.strip()
        return pd.Series([""] * len(df), index=df.index)
    street = col_or_empty(col_map.get("street"))
    zipc   = col_or_empty(col_map.get("zip")).apply(normalize_zip)
    city   = col_or_empty(col_map.get("city"))
    if col_map.get("country") and col_map["country"] in df.columns:
        country = df[col_map["country"]].astype(str).fillna("").str.strip().replace("", "Deutschland")
    else:
        country = pd.Series(["Deutschland"] * len(df), index=df.index)
    out = []
    for i in range(len(df)):
        segs=[]
        if street.iat[i]: segs.append(street.iat[i])
        place = " ".join([p for p in [zipc.iat[i], city.iat[i]] if p]).strip()
        if place: segs.append(place)
        if country.iat[i]: segs.append(country.iat[i])
        out.append(", ".join(segs))
    return pd.Series(out, index=df.index, name="full_address")

def google_geocode(address: str, key: str) -> Optional[Tuple[float, float]]:
    """Robustes Geocoding: cached OK-Ergebnisse, Retries bei temporären Fehlern."""
    if not address:
        return None
    if address in st.session_state["geocode_cache"]:
        return st.session_state["geocode_cache"][address]
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": address, "key": key, "language": "de"}
    for attempt in range(int(max_retries) + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
        except Exception as e:
            if attempt < max_retries:
                time.sleep(0.5 * (2 ** attempt)); continue
            st.error(f"Geocoding-Netzwerkfehler '{address}': {e}")
            return None
        status = data.get("status", "UNKNOWN")
        if status == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            res = (loc["lat"], loc["lng"])
            st.session_state["geocode_cache"][address] = res
            return res
        if status in {"UNKNOWN_ERROR","OVER_QUERY_LIMIT"} and attempt < max_retries:
            time.sleep(0.7 * (2 ** attempt)); continue
        if status in {"REQUEST_DENIED","INVALID_REQUEST"}:
            st.error(f"Geocoding abgelehnt '{address}': {status} – {data.get('error_message','')}")
            return None
        if status == "ZERO_RESULTS":
            st.warning(f"Geocoding ZERO_RESULTS '{address}'. Bitte prüfen.")
            return None
        if attempt < max_retries:
            time.sleep(0.5 * (2 ** attempt)); continue
        st.warning(f"Geocoding-Fehler '{address}': {status} – {data.get('error_message','')}")
        return None

def to_latlng_string(lat: float, lng: float) -> str:
    return f"{lat},{lng}"

def assert_latlng_tokens(tokens: List[str]):
    bad = [(i, t) for i, t in enumerate(tokens) if not (isinstance(t, str) and LATLNG_RE.match(t or ""))]
    if bad:
        idxs = ", ".join(str(i) for i, _ in bad[:10])
        raise ValueError(f"Ungültige lat,lng-Tokens in Zeilen: {idxs} … (Format: '48.123,11.567')")

def make_unique_labels(names: List[str]) -> List[str]:
    seen = {}
    out = []
    for n in names:
        base = n.strip() or "Verein"
        if base not in seen:
            seen[base] = 1
            out.append(base)
        else:
            seen[base] += 1
            out.append(f"{base} ({seen[base]})")
    return out

def _block_plan(n: int, max_side: int = 25, max_elements: int = 100):
    """Erzeugt Block-Indizes (o_idx, d_idx) für eine quadratische n×n-Matrix, sodass rows*cols ≤ max_elements."""
    # Wähle Blockseiten so groß wie möglich, aber ≤25 und ≤100 Elemente.
    # Für n>10 wird standardmäßig 10×10 gewählt, Restblöcke werden kleiner.
    side = min(max_side, int(math.floor(math.sqrt(max_elements))))
    # Sicherheitsnetz: falls side*side > max_elements -> side reduzieren
    while side * side > max_elements:
        side -= 1
    if side < 1:
        side = 1
    for o0 in range(0, n, side):
        o_idx = list(range(o0, min(o0 + side, n)))
        for d0 in range(0, n, side):
            d_idx = list(range(d0, min(d0 + side, n)))
            yield o_idx, d_idx

# ----------------------- Distance Matrix API v1 (JSON, POST) -----------------
DM_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
DM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
    "Accept-Encoding": "identity",  # manche Proxies zicken mit gzip/deflate
}

def _quota_ensure(elements_needed: int, use_window: bool, qstate: dict, quota_elements: int, quota_window_s: int):
    if not use_window:
        return
    now = time.time()
    elapsed = now - qstate["window_start"]
    if elapsed >= quota_window_s:
        qstate["window_start"] = now
        qstate["used"] = 0
    if qstate["used"] + elements_needed > quota_elements:
        sleep_for = quota_window_s - elapsed
        if sleep_for > 0:
            st.info(f"Quota: {qstate['used']}/{quota_elements} Elemente verbraucht. Warte {sleep_for:.1f}s …")
            time.sleep(sleep_for)
        qstate["window_start"] = time.time()
        qstate["used"] = 0
    qstate["used"] += elements_needed

def _top_status_info(resp: requests.Response) -> Tuple[str, str]:
    try:
        data = resp.json()
        status = data.get("status", "UNKNOWN")
        return status, (str(data)[:800])
    except Exception:
        return f"HTTP_{resp.status_code}", resp.text[:800] if resp.text else ""

def distance_matrix_v1_post_max100(
    tokens: List[str],
    api_key: str,
    mode: str = "driving",
    use_departure_now: bool = False,
    base_sleep: float = 0.25,
    per_element_delay: float = 0.0015,
    max_retries: int = 5,
    max_elements_per_req: int = 100,
    max_side: int = 25,
    debug_headers: bool = False,
    use_quota_window: bool = False,
    quota_elements: int = 100,
    quota_window_s: int = 60,
) -> Dict[Tuple[str, str], Dict[str, Optional[float]]]:
    """
    tokens: Liste 'lat,lng' je Verein (gleiche Reihenfolge für Origins/Destinations)
    Rückgabe: Dict[(origin_token, dest_token)] -> {distance_m, duration_s, status}
    """
    n = len(tokens)
    assert_latlng_tokens(tokens)

    results: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}
    plan = list(_block_plan(n, max_side=max_side, max_elements=max_elements_per_req))
    total_calls = len(plan)
    pb = st.progress(0.0, text="Distance Matrix v1 (≤100/Request) …")
    qstate = {"window_start": time.time(), "used": 0}

    step = 0
    for o_idx, d_idx in plan:
        origins = [tokens[i] for i in o_idx]
        destinations = [tokens[j] for j in d_idx]
        elements = len(origins) * len(destinations)

        # Quota-Fenster beachten (optional)
        _quota_ensure(elements, use_quota_window, qstate, quota_elements, quota_window_s)

        # Mehrfach versuchen (429/5xx/UNKNOWN_ERROR)
        ok_block = False
        for attempt in range(int(max_retries) + 1):
            form = {
                "origins": "|".join(origins),
                "destinations": "|".join(destinations),
                "mode": mode,
                "units": "metric",
                "key": api_key,
            }
            if use_departure_now and mode in {"driving", "transit"}:
                form["departure_time"] = "now"

            try:
                r = requests.post(DM_URL, headers=DM_HEADERS, data=form, timeout=120)
                if debug_headers:
                    top, snippet = _top_status_info(r)
                    st.caption(f"Block O{len(origins)}×D{len(destinations)}: HTTP={r.status_code} status={top} len={len(r.text or '')}")
                    if top != "OK":
                        st.code(snippet, language="json")
                data = r.json()
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(min(6.0, 0.6 * (2 ** attempt)))
                    continue
                # ganzen Block als Fehler markieren
                for i in o_idx:
                    for j in d_idx:
                        results[(tokens[i], tokens[j])] = {"distance_m": None, "duration_s": None, "status": f"NET_ERR:{e}"}
                break

            status = data.get("status", "UNKNOWN")
            if status == "OK":
                rows = data.get("rows", [])
                if len(rows) != len(origins):
                    status = f"BLOCK_ERR:rows_mismatch {len(rows)}!={len(origins)}"
                else:
                    # Elemente auslesen
                    for ii, row in enumerate(rows):
                        els = row.get("elements", [])
                        if len(els) != len(destinations):
                            # Zeile markieren
                            for jj in range(len(destinations)):
                                results[(origins[ii], destinations[jj])] = {"distance_m": None, "duration_s": None, "status": "ROW_LEN_MISMATCH"}
                            continue
                        for jj, el in enumerate(els):
                            st_el = el.get("status", "UNKNOWN")
                            if st_el == "OK":
                                dist_m = el.get("distance", {}).get("value")
                                dur_s  = el.get("duration", {}).get("value")
                                results[(origins[ii], destinations[jj])] = {
                                    "distance_m": float(dist_m) if dist_m is not None else None,
                                    "duration_s": float(dur_s) if dur_s is not None else None,
                                    "status": "OK",
                                }
                            else:
                                results[(origins[ii], destinations[jj])] = {
                                    "distance_m": None,
                                    "duration_s": None,
                                    "status": st_el,
                                }
                    ok_block = True
            if ok_block:
                break
            # Retry bei temporären Problemen
            if attempt < max_retries:
                time.sleep(min(6.0, 0.6 * (2 ** attempt)))

        # Falls Block gar nicht ok, markiere fehlende Pärchen, die noch nicht gesetzt sind
        if not ok_block:
            for i in o_idx:
                for j in d_idx:
                    results.setdefault((tokens[i], tokens[j]), {"distance_m": None, "duration_s": None, "status": "BLOCK_FAILED"})

        # sanfte Drosselung
        time.sleep(base_sleep + per_element_delay * elements)
        step += 1
        pb.progress(step / total_calls, text=f"Distance Matrix v1 (≤100/Request) … ({step}/{total_calls})")

    pb.empty()
    return results

# ------------------------------ App-Workflow ---------------------------------
if uploaded is not None:
    df = pd.read_excel(uploaded)
    st.success(f"Datei geladen: {uploaded.name} — {len(df)} Zeilen")

    with st.expander("Spalten zuordnen / Adresse bauen", expanded=True):
        name_col   = st.selectbox("Vereinsname", options=df.columns.tolist(),
                                  index=0 if "Verein" not in df.columns else df.columns.get_loc("Verein"))
        street_col = st.selectbox("Straße/Hausnr.", options=[None]+df.columns.tolist(),
                                  index=0 if "Adresse" not in df.columns else df.columns.get_loc("Adresse")+1)
        zip_col    = st.selectbox("PLZ", options=[None]+df.columns.tolist(),
                                  index=0 if "PLZ" not in df.columns else df.columns.get_loc("PLZ")+1)
        city_col   = st.selectbox("Ort", options=[None]+df.columns.tolist(),
                                  index=0 if "Ort" not in df.columns else df.columns.get_loc("Ort")+1)
        country_col= st.selectbox("Land (optional, default DE)", options=[None]+df.columns.tolist(),
                                  index=0 if "Land" not in df.columns else df.columns.get_loc("Land")+1)

        use_existing_full = False
        if "full_address" in df.columns:
            use_existing_full = st.checkbox("Vorhandene Spalte 'full_address' verwenden", value=False)

    # Adressen/Koordinaten vorbereiten
    if "latlng" in df.columns and df["latlng"].notna().any():
        tokens = [df["latlng"].iloc[i] if pd.notna(df["latlng"].iloc[i]) else None for i in range(len(df))]
    else:
        tokens = [None]*len(df)

    if not use_existing_full:
        full_address = build_full_address(df, {
            "street": street_col or None,
            "zip": zip_col or None,
            "city": city_col or None,
            "country": country_col or None,
        })
    else:
        full_address = df["full_address"].astype(str).fillna("").str.strip()

    st.subheader("Distance Matrix (alle gegen alle) – v1 JSON (POST, ≤100/Request)")
    sample_n = st.number_input("Optional: nur die ersten N Vereine berechnen (Test)", 0, value=0, step=1, help="0 = alle")

    names = df[name_col].astype(str).fillna("").str.strip().tolist()
    if sample_n and sample_n > 0:
        names = names[:sample_n]
        full_address = full_address.iloc[:sample_n]
        tokens = tokens[:sample_n]

    # fehlende Koordinaten per Geocoding holen
    if st.checkbox("Fehlende Koordinaten per Geocoding ergänzen", value=True):
        if not api_key:
            st.warning("Bitte zuerst API-Key eintragen (oben).")
        else:
            geocode_progress = st.progress(0.0, text="Geocoding …")
            for i in range(len(tokens)):
                if tokens[i] and LATLNG_RE.match(tokens[i]):  # schon gesetzt
                    pass
                else:
                    addr = full_address.iloc[i]
                    loc = google_geocode(addr, api_key=api_key)
                    if loc:
                        tokens[i] = to_latlng_string(*loc)
                    else:
                        tokens[i] = None
                geocode_progress.progress((i+1)/len(tokens), text=f"Geocoding … ({i+1}/{len(tokens)})")
            geocode_progress.empty()

    # Koordinaten prüfen
    try:
        assert_latlng_tokens(tokens)
    except Exception as e:
        st.error(str(e))
        st.stop()

    names_unique = make_unique_labels(names)

    if st.button("Entfernungen & Zeiten berechnen (≤100/Request)"):
        if not api_key:
            st.error("API-Key fehlt.")
            st.stop()

        st.info("Sende Anfragen an Distance Matrix API v1 (POST) … (≤100 Elemente pro Request)")
        res = distance_matrix_v1_post_max100(
            tokens=tokens,
            api_key=api_key,
            mode=travel_mode,
            use_departure_now=use_departure_now,
            base_sleep=base_sleep,
            per_element_delay=per_element_delay,
            max_retries=int(max_retries),
            max_elements_per_req=100,   # harte Grenze
            max_side=25,
            debug_headers=debug_headers,
            use_quota_window=use_quota_window,
            quota_elements=int(quota_elements),
            quota_window_s=int(quota_window_s),
        )

        # Ergebnisse in DataFrames
        n = len(tokens)
        dist_km = pd.DataFrame(index=names_unique, columns=names_unique, dtype="float64")
        time_min = pd.DataFrame(index=names_unique, columns=names_unique, dtype="float64")
        status_tbl = pd.DataFrame(index=names_unique, columns=names_unique, dtype="string")

        for i in range(n):
            for j in range(n):
                r = res.get((tokens[i], tokens[j]), {})
                dm = r.get("distance_m")
                ds = r.get("duration_s")
                stt = r.get("status")
                dist_km.iat[i, j] = (dm / 1000.0) if dm is not None else None
                time_min.iat[i, j] = (ds / 60.0) if ds is not None else None
                status_tbl.iat[i, j] = stt

        # Anzeigeeinheit
        if units_display == "imperial":
            dist_out = dist_km * 0.621371  # km -> miles
            dist_label = "Distance (mi)"
        else:
            dist_out = dist_km
            dist_label = "Distanz (km)"

        st.subheader("Ergebnisse")
        st.markdown(f"**{dist_label}**")
        st.dataframe(dist_out.round(2))

        st.markdown("**Fahrzeit (min)**")
        st.dataframe(time_min.round(1))

        st.markdown("**Status**")
        st.dataframe(status_tbl)

        # Excel exportieren
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            meta = pd.DataFrame({
                "Einstellung": ["Mode", "Departure now", "Units (Display)", "Max/Req", "Max side"],
                "Wert": [travel_mode, use_departure_now, units_display, 100, 25],
            })
            meta.to_excel(writer, sheet_name="Einstellungen", index=False)
            pd.DataFrame({"Name": names_unique, "Token (lat,lng)": tokens}).to_excel(writer, sheet_name="Vereine", index=False)
            dist_out.to_excel(writer, sheet_name=dist_label.replace("/", "_"))
            time_min.to_excel(writer, sheet_name="Fahrzeit_min")
            status_tbl.to_excel(writer, sheet_name="Status")
        st.download_button("Excel herunterladen", data=buf.getvalue(),
                           file_name="Entfernungen_und_Fahrzeiten_v1_max100.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ------------------------------- Hinweise ------------------------------------
st.markdown("---")
with st.expander("Hinweise & Anforderungen", expanded=False):
    st.markdown(
        """
- **Erforderlich:** „Geocoding API“ **und** „Distance Matrix API“ müssen aktiv sein (Projekt + Key).
- **Key-Einschränkung:** Für Server-Apps keine HTTP-Referrer-Restriktion nutzen. Nutze *Application restrictions* passend (z. B. IP-Restriction) oder testweise ohne Restriktion.
- **Limits v1:** Max. **25×25** pro Request **und** max. **100 Elemente**. Dieses Tool schneidet die Matrix so, dass **≤100 pro Request** gesendet wird.
- **Rate-Limits:** Optionales **Quota-Fenster** (z. B. 100 Elemente/60 s) hilft gegen `OVER_QUERY_LIMIT`.
- **Transit:** Außerhalb Ballungsräumen oft `ZERO_RESULTS`. Zum Testen lieber `driving` ohne `departure_time`.
"""
    )
