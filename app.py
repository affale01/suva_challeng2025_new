import os
import gradio as gr
import folium
import requests
import xml.etree.ElementTree as ET

SEARCH_URL = "https://api3.geo.admin.ch/rest/services/api/SearchServer"
IDENTIFY_URL = "https://api3.geo.admin.ch/rest/services/api/MapServer/identify"
GWR_PUBLIC_URL = "https://madd.bfs.admin.ch/eCH-0206"

# ---------- Helpers ----------

def geocode_address(address: str):
    """Adresse/Ort -> (lat, lon, label) via GeoAdmin SearchServer."""
    params = {"searchText": address, "type": "locations", "sr": 4326, "limit": 1}
    r = requests.get(SEARCH_URL, params=params, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        return None
    attrs = data["results"][0]["attrs"]
    return float(attrs["lat"]), float(attrs["lon"]), attrs.get("label", address)

def identify_egid(lat: float, lon: float, sr: int, tolerance: int):
    """Einmaliger Identify-Call; gibt EGID oder None zurück."""
    params = {
        "geometryType": "esriGeometryPoint",
        "geometry": f"{lon},{lat}",  # x=lon, y=lat
        "sr": sr,
        "layers": "all:ch.bfs.gebaeude_wohnungs_register",
        "tolerance": tolerance,
        "mapExtent": "0,0,100,100",  # Geändert von 0,0,0,0
        "imageDisplay": "100,100,96",  # Geändert von 1,1,96
        "returnGeometry": "false",
        "lang": "de",
    }
    r = requests.get(IDENTIFY_URL, params=params, timeout=10)
    r.raise_for_status()
    js = r.json()
    
    # DEBUG: Print response
    print(f"[DEBUG] Identify response für sr={sr}, tolerance={tolerance}:")
    print(f"  Results count: {len(js.get('results', []))}")
    
    if not js.get("results"):
        return None
    
    egid = js["results"][0]["attributes"].get("egid")
    print(f"  EGID found: {egid}")
    return egid

def wgs84_to_lv95(lat, lon):
    """
    Grobe, dependency-freie Umrechnung WGS84 -> LV95 (CH).
    Für Identify-Fallback ausreichend.
    """
    lat = lat * 3600
    lon = lon * 3600
    lat_aux = (lat - 169028.66) / 10000
    lon_aux = (lon - 26782.5) / 10000
    e = 2600072.37 + 211455.93 * lon_aux - 10938.51 * lon_aux * lat_aux - 0.36 * lon_aux * lat_aux**2 - 44.54 * lon_aux**3
    n = 1200147.07 + 308807.95 * lat_aux + 3745.25 * lon_aux**2 + 76.63 * lat_aux**2 - 194.56 * lon_aux**2 * lat_aux + 119.79 * lat_aux**3
    return n, e

def get_egid_from_point(lat: float, lon: float):
    """
    Koordinate -> EGID. Versucht zuerst sr=4326 (WGS84) mit größerer Toleranz,
    fällt dann auf sr=2056 (LV95) zurück.
    """
    print(f"\n[DEBUG] Suche EGID für Koordinaten: lat={lat}, lon={lon}")
    
    # Versuch 1: WGS84 mit höherer Toleranz
    print("[DEBUG] Versuch 1: WGS84 (sr=4326), tolerance=50")
    egid = identify_egid(lat, lon, sr=4326, tolerance=50)  # Erhöht von 15
    if egid:
        print(f"[DEBUG] ✅ EGID gefunden (WGS84): {egid}")
        return egid

    # Versuch 2: LV95
    print("[DEBUG] Versuch 2: LV95 (sr=2056)")
    n, e = wgs84_to_lv95(lat, lon)
    print(f"[DEBUG] Umgerechnete LV95-Koordinaten: N={n:.2f}, E={e:.2f}")
    egid = identify_egid(lat=n, lon=e, sr=2056, tolerance=50)  # Erhöht von 15
    
    if egid:
        print(f"[DEBUG] ✅ EGID gefunden (LV95): {egid}")
    else:
        print("[DEBUG] ❌ Keine EGID gefunden")
    
    return egid

def find_first_text(root: ET.Element, localnames):
    """Namespace-agnostisch: suche erstes Element, dessen lokaler Tagname in localnames ist."""
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag in localnames and (elem.text and elem.text.strip()):
            return elem.text.strip()
    return None

def get_gwr_public_info(egid: str | int) -> dict:
    """EGID -> öffentliche GWR-Infos via eCH-0206 (XML)."""
    if not egid:
        return {}
    
    print(f"\n[DEBUG] GWR-Abfrage für EGID: {egid}")
    
    try:
        r = requests.get(GWR_PUBLIC_URL, params={"egid": str(egid)}, timeout=10)
        print(f"[DEBUG] GWR Response Status: {r.status_code}")
        r.raise_for_status()
        
        # Debug: Print raw response
        print(f"[DEBUG] GWR Response (erste 500 Zeichen):\n{r.text[:500]}")
        
        root = ET.fromstring(r.content)
        
        info = {"egid": str(egid)}
        
        # Erweiterte Suche mit mehr möglichen Feldnamen
        baujahr_options = [
            "yearOfConstruction", "baujahr", "GBAUJ", 
            "constructionYear", "dateOfConstruction"
        ]
        gebkat_options = [
            "buildingCategory", "gebaeudekategorie", "GKAT",
            "category"
        ]
        wohnung_options = [
            "numberOfDwellings", "anzahlWohnungen", "GAZZI",
            "dwellings"
        ]
        
        info["baujahr"] = find_first_text(root, baujahr_options)
        info["gebaeudekategorie"] = find_first_text(root, gebkat_options)
        info["anzahl_wohnungen"] = find_first_text(root, wohnung_options)
        
        print(f"[DEBUG] Gefundene GWR-Infos: {info}")
        
        return {k: v for k, v in info.items() if v}
        
    except Exception as e:
        print(f"[DEBUG] ❌ GWR-Fehler: {e}")
        return {}

# ---------- UI-Callback ----------

def show_address_on_map(address, basemap="swisstopo_grey"):
    if not address:
        return "<p>Bitte eine Adresse eingeben.</p>"

    try:
        print(f"\n{'='*60}")
        print(f"[INFO] Suche Adresse: {address}")
        print(f"{'='*60}")
        
        geo = geocode_address(address)
        if not geo:
            return f"<p>❌ Keine Treffer für <b>{address}</b>.</p>"
        
        lat, lon, label = geo
        print(f"[INFO] Gefunden: {label}")
        print(f"[INFO] Koordinaten: lat={lat}, lon={lon}")

        # EGID suchen
        egid = get_egid_from_point(lat, lon)
        
        # GWR-Daten holen
        gwr = get_gwr_public_info(egid) if egid else {}
        
        print(f"\n[INFO] Finale Daten:")
        print(f"  - EGID: {egid or '❌ nicht gefunden'}")
        print(f"  - GWR-Daten: {gwr if gwr else '❌ keine Daten'}")

        # Karte erstellen
        m = folium.Map(
            location=[lat, lon],
            zoom_start=18,  # Näher ran für bessere Sicht
            control_scale=True,
            prefer_canvas=True
        )

        # Basemap
        if basemap == "swisstopo_grey":
            folium.TileLayer(
                tiles="https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-grau/default/current/3857/{z}/{x}/{y}.jpeg",
                attr="© swisstopo",
                name="swisstopo (grau)",
                overlay=False,
                control=True,
                max_zoom=20,
                detect_retina=True
            ).add_to(m)
        else:
            folium.TileLayer(
                tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
                attr="© OpenStreetMap contributors © CARTO",
                name="OSM Light",
                overlay=False,
                control=True,
                max_zoom=20,
                detect_retina=True
            ).add_to(m)

        # Popup mit Debug-Infos
        details = [
            f"<div style='font-family: Arial; min-width: 250px;'>",
            f"<h4 style='margin: 0 0 10px 0; color: #2c3e50;'>{label}</h4>",
            f"<hr style='margin: 10px 0;'>",
            f"<p style='margin: 5px 0;'><b>📍 Koordinaten:</b><br>{lat:.6f}, {lon:.6f}</p>",
        ]
        
        if egid:
            details.append(f"<p style='margin: 5px 0;'><b>🏢 EGID:</b> {egid}</p>")
            
            if gwr.get("baujahr"):
                baujahr = int(gwr["baujahr"])
                if baujahr < 1990:
                    farbe = "red"
                    risiko = "HOCH ⚠️"
                else:
                    farbe = "green"
                    risiko = "GERING ✅"
                
                details.append(f"<p style='margin: 5px 0;'><b>📅 Baujahr:</b> {baujahr}</p>")
                details.append(f"<p style='margin: 5px 0; padding: 8px; background-color: #{farbe}22; border-left: 3px solid {farbe};'><b>Asbestrisiko:</b> {risiko}</p>")
            else:
                details.append(f"<p style='margin: 5px 0; color: #888;'><i>⚠️ Baujahr nicht verfügbar</i></p>")
            
            if gwr.get("gebaeudekategorie"):
                details.append(f"<p style='margin: 5px 0;'><b>🏗️ Kategorie:</b> {gwr['gebaeudekategorie']}</p>")
            
            if gwr.get("anzahl_wohnungen"):
                details.append(f"<p style='margin: 5px 0;'><b>🏠 Wohnungen:</b> {gwr['anzahl_wohnungen']}</p>")
        else:
            details.append(f"<p style='margin: 10px 0; padding: 10px; background-color: #fff3cd; border-left: 3px solid #ffc107;'>")
            details.append(f"<b>⚠️ EGID nicht gefunden</b><br>")
            details.append(f"<small>Gebäude möglicherweise nicht im GWR erfasst oder Adresse zu ungenau.</small>")
            details.append(f"</p>")
        
        details.append("</div>")
        popup_html = "".join(details)

        # Marker mit Farbe je nach Baujahr
        if egid and gwr.get("baujahr"):
            baujahr = int(gwr["baujahr"])
            marker_color = "red" if baujahr < 1990 else "green"
            marker_icon = "exclamation-triangle" if baujahr < 1990 else "check-circle"
        else:
            marker_color = "gray"
            marker_icon = "question"

        folium.Marker(
            [lat, lon],
            tooltip=f"{label} (Klicken für Details)",
            popup=folium.Popup(popup_html, max_width=350),
            icon=folium.Icon(
                color=marker_color,
                icon=marker_icon,
                prefix='fa'
            )
        ).add_to(m)

        return m._repr_html_()

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"\n[ERROR] {error_details}")
        return f"<p>❌ <b>Fehler:</b> {e}</p><pre style='font-size: 10px;'>{error_details}</pre>"

# ---------- Gradio App ----------

with gr.Blocks(title="Smart Safety Map - Prototype", theme=gr.themes.Soft()) as demo:
    gr.Markdown("""
    # 🗺️ Smart Safety Map - Prototype
    ### Swisstopo + GWR Integration mit Asbestrisiko-Bewertung
    
    Gib eine Schweizer Adresse ein und erhalte:
    - 📍 Genaue Lokalisierung auf der Karte
    - 🏢 EGID (Eidgenössischer Gebäudeidentifikator)
    - 📅 Baujahr aus dem GWR
    - ⚠️ Asbestrisiko-Bewertung (vor/nach 1990)
    """)
    
    with gr.Row():
        with gr.Column(scale=3):
            address = gr.Textbox(
                label="🔍 Adresse/Ort eingeben",
                value="Marktgasse 19, Bern",
                placeholder="z.B. Bundesplatz 3, Bern"
            )
        with gr.Column(scale=1):
            basemap = gr.Radio(
                ["swisstopo_grey", "osm_light"],
                value="swisstopo_grey",
                label="Kartenstil"
            )
    
    out = gr.HTML()
    
    # Beispiele
    gr.Examples(
        examples=[
            ["Marktgasse 19, Bern"],
            ["Bahnhofstrasse 1, Zürich"],
            ["Bundesplatz 3, Bern"],
            ["Seftigenstrasse 264, Wabern"],
        ],
        inputs=address
    )
    
    # Event handlers
    demo.load(show_address_on_map, inputs=[address, basemap], outputs=out)
    address.submit(show_address_on_map, inputs=[address, basemap], outputs=out)
    basemap.change(show_address_on_map, inputs=[address, basemap], outputs=out)
    
    gr.Markdown("""
    ---
    **Debug-Modus aktiviert:** Logs werden in der Konsole ausgegeben.
    
    **Farbcode:**
    - 🔴 Rot: Baujahr < 1990 (Asbestrisiko)
    - 🟢 Grün: Baujahr ≥ 1990 (kein Asbestrisiko)
    - ⚪ Grau: Baujahr unbekannt
    """)

if __name__ == "__main__":
    port = int(os.getenv("GRADIO_SERVER_PORT", os.getenv("PORT", "7860")))
    demo.launch(server_name="0.0.0.0", server_port=port)