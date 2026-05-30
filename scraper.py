#!/usr/bin/env python3
"""
Hubidx Permit Radar Scraper — Fort Worth
Corre en Railway como cron job. Escribe a Supabase.

Variables de entorno requeridas:
  SUPABASE_URL                  — URL de tu proyecto Supabase
  SUPABASE_SERVICE_ROLE_KEY     — Service role key (mantener privado)
  PERMIT_DAYS_BACK              — Días hacia atrás (default: 7)

Uso:
  python3 scraper.py              # corrida normal (últimos 7 días)
  PERMIT_DAYS_BACK=90 python3 scraper.py   # primera carga (90 días)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Instala: pip install playwright && python -m playwright install chromium")

try:
    import requests
except ImportError:
    sys.exit("Instala: pip install requests")

try:
    from supabase import create_client, Client
except ImportError:
    sys.exit("Instala: pip install supabase")

# ============================================
# CONFIG
# ============================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
DAYS_BACK = int(os.environ.get("PERMIT_DAYS_BACK", "7"))
CITY_KEY = "fort_worth"

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY son requeridas")

ARCGIS_BASE = (
    "https://services5.arcgis.com/3ddLCBXe1bRt7mzj/arcgis/rest/services/"
    "CFW_Open_Data_Development_Permits_View/FeatureServer/0/query"
)
SEARCH_URL = "https://aca-prod.accela.com/cfw/Cap/CapHome.aspx?module=Development"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

DELAY_BETWEEN_SCRAPES = 3  # segundos
DELAY_BETWEEN_GEOCODE = 1  # OSM rate limit

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================
# 1. TRAER PERMISOS DE ARCGIS
# ============================================
def fetch_permits_from_arcgis(days):
    """Trae permisos con todos sus campos en una sola request."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    offset = 0
    permits = []

    print(f"[ArcGIS] Buscando permisos de los últimos {days} días...")

    while True:
        r = requests.get(ARCGIS_BASE, params={
            "where": f"File_Date >= timestamp '{cutoff}'",
            "outFields": "*",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": 1000,
            "orderByFields": "File_Date DESC",
        }, timeout=30)
        data = r.json()
        feats = data.get("features", [])

        for f in feats:
            attrs = f.get("attributes", {})
            geom = f.get("geometry") or {}
            permits.append({
                "permit_number": attrs.get("Permit_No", "").strip(),
                "permit_type": attrs.get("Record_Type"),
                "status": attrs.get("Record_Status"),
                "work_description": attrs.get("Work_Description"),
                "address": attrs.get("Permit_Address"),
                "zip_code": attrs.get("Zip_Code"),
                "permit_value_cents": int((attrs.get("Project_Value") or 0) * 100),
                "file_date_ms": attrs.get("File_Date"),
                "longitude": geom.get("x"),
                "latitude": geom.get("y"),
                "raw_arcgis": attrs,
            })

        if not feats or not data.get("exceededTransferLimit"):
            break
        offset += len(feats)

    # Deduplicar por permit_number (manteniendo el más reciente)
    seen = {}
    for p in permits:
        if p["permit_number"]:
            seen[p["permit_number"]] = p

    result = list(seen.values())
    print(f"[ArcGIS] {len(result)} permisos únicos")
    return result


# ============================================
# 2. SCRAPE DE ACCELA (extrae contactos)
# ============================================
def parse_contact_block(raw):
    """Extrae email, teléfonos y licencia de un bloque de texto."""
    result = {"raw": " | ".join([l.strip() for l in raw.strip().split("\n") if l.strip()])}

    emails = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", raw)
    if emails:
        result["email"] = emails[0]

    phones = re.findall(r"\d{3}[-.]?\d{3}[-.]?\d{4}", raw)
    if phones:
        result["phones"] = list(dict.fromkeys(phones))

    m = re.search(r"([A-Z]{2,4}\s*-\s*[^\n]+?[A-Z]\d{4,})", raw)
    if m:
        result["license"] = m.group(1).strip()

    # Primera línea no vacía = nombre
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    if lines:
        result["name"] = lines[0]

    return result


def extract_detail(page):
    """Extrae datos de la página de detalle de Accela."""
    text = page.inner_text("body")
    info = {}

    m = re.search(r"Applicant:\s*\n(.*?)(?=Licensed Professional:|Owner:|More Details|$)", text, re.S)
    if m:
        info["applicant"] = parse_contact_block(m.group(1))

    m = re.search(r"Licensed Professional:\s*\n(.*?)(?=Owner:|Applicant:|More Details|$)", text, re.S)
    if m:
        info["contractor"] = parse_contact_block(m.group(1))

    m = re.search(r"Owner:\s*\n(.*?)(?=More Details|Applicant:|Licensed Professional:|$)", text, re.S)
    if m:
        info["owner"] = parse_contact_block(m.group(1))

    return info


def scrape_accela_for_permits(permit_numbers):
    """Abre Accela y busca cada permiso. Retorna dict {permit_no: info}."""
    if not permit_numbers:
        return {}

    print(f"[Accela] Scrapeando {len(permit_numbers)} permisos...")
    results = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page()

        for idx, permit_no in enumerate(permit_numbers):
            try:
                page.goto(SEARCH_URL, timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(1.5)

                page.fill(
                    "#ctl00_PlaceHolderMain_generalSearchForm_txtGSPermitNumber",
                    permit_no,
                )
                page.click("#ctl00_PlaceHolderMain_btnNewSearch")
                time.sleep(4)
                page.wait_for_load_state("networkidle", timeout=15000)

                text = page.inner_text("body")

                if "Record Details" in text or "Permit Address" in text:
                    results[permit_no] = extract_detail(page)
                    print(f"  [{idx+1}/{len(permit_numbers)}] OK {permit_no}")
                elif "no results" in text.lower():
                    results[permit_no] = {"error": "not_found"}
                    print(f"  [{idx+1}/{len(permit_numbers)}] -- {permit_no}: not found")
                else:
                    links = page.query_selector_all('a[href*="CapDetail"]')
                    if links:
                        links[0].click()
                        time.sleep(4)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        results[permit_no] = extract_detail(page)
                        print(f"  [{idx+1}/{len(permit_numbers)}] OK {permit_no} (multi)")
                    else:
                        results[permit_no] = {"error": "unknown_state"}

            except Exception as e:
                results[permit_no] = {"error": str(e)[:200]}
                print(f"  [{idx+1}/{len(permit_numbers)}] ERR {permit_no}: {e}")

            time.sleep(DELAY_BETWEEN_SCRAPES)

        browser.close()

    return results


# ============================================
# 3. GEOCODING (solo para los que ArcGIS no trajo coords)
# ============================================
def geocode_address(address, city="Fort Worth", state="TX"):
    """Geocodifica usando Nominatim (OSM, gratis)."""
    if not address:
        return None, None
    try:
        r = requests.get(NOMINATIM_URL, params={
            "q": f"{address}, {city}, {state}",
            "format": "json",
            "limit": 1,
        }, headers={"User-Agent": "Hubidx-PermitRadar/1.0"}, timeout=10)
        results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        print(f"  [Geocode] Error: {e}")
    return None, None


# ============================================
# 4. CLASIFICAR (homeowner vs contractor)
# ============================================
def classify_permit(applicant, contractor, owner):
    """
    Si el aplicante == dueño y NO hay contractor licensed → homeowner (lead caliente!)
    Si hay contractor → contractor (ya tienen quien lo haga)
    """
    has_contractor = bool(contractor and contractor.get("name") and not contractor.get("error"))
    if has_contractor:
        return "contractor"

    applicant_name = (applicant or {}).get("name", "").lower().strip()
    owner_name = (owner or {}).get("name", "").lower().strip()

    if applicant_name and owner_name and applicant_name == owner_name:
        return "homeowner"
    if applicant_name and not has_contractor:
        return "homeowner"

    return "unknown"


# ============================================
# 5. UPSERT A SUPABASE
# ============================================
def upsert_to_supabase(lead_row):
    """Inserta o actualiza un lead. Usa el índice único (city, permit_number)."""
    try:
        supabase.table("permit_leads").upsert(
            lead_row,
            on_conflict="city,permit_number"
        ).execute()
        return True
    except Exception as e:
        print(f"  [Supabase] Error con {lead_row.get('permit_number')}: {e}")
        return False


# ============================================
# MAIN
# ============================================
def main():
    print(f"\n{'='*60}")
    print(f"HUBIDX PERMIT RADAR — Fort Worth")
    print(f"Días hacia atrás: {DAYS_BACK}")
    print(f"{'='*60}\n")

    # 1. Obtener permisos
    permits = fetch_permits_from_arcgis(DAYS_BACK)
    if not permits:
        print("Sin permisos nuevos. Salida.")
        return

    # 2. Filtrar a los que ya tenemos (no re-scrape)
    permit_numbers = [p["permit_number"] for p in permits]
    existing_rows = supabase.table("permit_leads") \
        .select("permit_number") \
        .eq("city", CITY_KEY) \
        .in_("permit_number", permit_numbers) \
        .execute()
    existing_set = {r["permit_number"] for r in (existing_rows.data or [])}

    to_scrape = [p["permit_number"] for p in permits if p["permit_number"] not in existing_set]
    print(f"[Plan] {len(existing_set)} ya en DB, {len(to_scrape)} nuevos a scrapear\n")

    # 3. Scrape Accela solo para los nuevos
    accela_data = scrape_accela_for_permits(to_scrape) if to_scrape else {}

    # 4. Combinar todo y guardar
    print(f"\n[Save] Guardando {len(permits)} leads en Supabase...")
    saved = 0
    failed = 0

    for p in permits:
        permit_no = p["permit_number"]
        accela = accela_data.get(permit_no, {})

        applicant = accela.get("applicant") or {}
        contractor = accela.get("contractor") or {}
        owner = accela.get("owner") or {}

        # Geocoding si ArcGIS no trajo coords y tenemos dirección
        lat = p.get("latitude")
        lng = p.get("longitude")
        if (not lat or not lng) and p.get("address") and permit_no not in existing_set:
            lat, lng = geocode_address(p["address"])
            time.sleep(DELAY_BETWEEN_GEOCODE)

        category = classify_permit(applicant, contractor, owner)

        file_date_ms = p.get("file_date_ms")
        file_date = (
            datetime.utcfromtimestamp(file_date_ms / 1000).isoformat() + "Z"
            if file_date_ms else None
        )

        row = {
            "city": CITY_KEY,
            "permit_number": permit_no,
            "permit_type": p.get("permit_type"),
            "status": p.get("status"),
            "work_description": p.get("work_description"),
            "address": p.get("address"),
            "city_name": "Fort Worth",
            "state_code": "TX",
            "zip_code": p.get("zip_code"),
            "latitude": lat,
            "longitude": lng,
            "applicant_name": applicant.get("name"),
            "applicant_email": applicant.get("email"),
            "applicant_phones": applicant.get("phones") or [],
            "owner_name": owner.get("name"),
            "contractor_name": contractor.get("name"),
            "contractor_license": contractor.get("license"),
            "category": category,
            "permit_value_cents": p.get("permit_value_cents") or None,
            "file_date": file_date,
            "raw_data": {
                "arcgis": p.get("raw_arcgis"),
                "accela": accela,
            },
        }

        if upsert_to_supabase(row):
            saved += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"DONE. Guardados: {saved}  Errores: {failed}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
