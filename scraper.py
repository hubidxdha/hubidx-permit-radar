#!/usr/bin/env python3
"""
Hubidx Permit Radar Scraper — Fort Worth
TODOS los permisos pasan por Accela para obtener detalles completos.

Variables de entorno requeridas:
  SUPABASE_URL                  — URL de tu proyecto Supabase
  SUPABASE_SERVICE_ROLE_KEY     — Service role key
  PERMIT_DAYS_BACK              — Días hacia atrás (default: 7)
  SKIP_EXISTING                 — Si "true", no re-scrapea permisos ya completos (default: true)
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
SKIP_EXISTING = os.environ.get("SKIP_EXISTING", "true").lower() == "true"
CITY_KEY = "fort_worth"

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY son requeridas")

ARCGIS_BASE = (
    "https://services5.arcgis.com/3ddLCBXe1bRt7mzj/arcgis/rest/services/"
    "CFW_Open_Data_Development_Permits_View/FeatureServer/0/query"
)
SEARCH_URL = "https://aca-prod.accela.com/cfw/Cap/CapHome.aspx?module=Development"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

DELAY_BETWEEN_SCRAPES = 3
DELAY_BETWEEN_GEOCODE = 1.1

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================
# 1. ARCGIS
# ============================================
def fetch_permit_numbers(days):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    offset, results = 0, []
    print(f"[ArcGIS] Listando permisos de los últimos {days} días...")
    while True:
        r = requests.get(ARCGIS_BASE, params={
            "where": f"File_Date >= timestamp '{cutoff}'",
            "outFields": "Permit_No,File_Date",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": 1000,
            "orderByFields": "File_Date DESC",
        }, timeout=30)
        data = r.json()
        feats = data.get("features", [])
        for f in feats:
            pn = f["attributes"].get("Permit_No", "")
            if pn:
                results.append({
                    "permit_number": pn,
                    "file_date_ms": f["attributes"].get("File_Date"),
                })
        if not feats or not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    seen = {}
    for p in results:
        seen[p["permit_number"]] = p
    print(f"[ArcGIS] {len(seen)} permisos únicos")
    return list(seen.values())


# ============================================
# 2. ACCELA
# ============================================
def parse_contact_block(raw):
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

    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    if lines:
        result["name"] = lines[0]

    return result


def extract_detail_from_page(page):
    text = page.inner_text("body")
    info = {}

    m = re.search(r"Record\s+(\S+):\s*\n\s*(.+?)\n\s*Record Status:\s*(.+)", text)
    if m:
        info["record_number"] = m.group(1).strip()
        info["record_type"] = m.group(2).strip()
        info["record_status"] = m.group(3).strip()

    m = re.search(r"Permit Address\s*\n\s*(.+?)(?:\n|$)", text)
    if m:
        info["address"] = m.group(1).strip().rstrip(" *,")

    m = re.search(r"Description:\s*\n\s*(.+?)(?:\n\s*\n|\nMore Details|$)", text, re.S)
    if m:
        info["work_description"] = m.group(1).strip()

    m = re.search(
        r"Applicant:\s*\n(.*?)(?=Licensed Professional:|Owner:|More Details|$)",
        text, re.S,
    )
    if m:
        info["applicant"] = parse_contact_block(m.group(1))

    m = re.search(
        r"Licensed Professional:\s*\n(.*?)(?=Owner:|Applicant:|More Details|$)",
        text, re.S,
    )
    if m:
        info["contractor"] = parse_contact_block(m.group(1))

    m = re.search(
        r"Owner:\s*\n(.*?)(?=More Details|Applicant:|Licensed Professional:|$)",
        text, re.S,
    )
    if m:
        info["owner"] = parse_contact_block(m.group(1))

    return info


def scrape_accela_for_permits(permit_numbers):
    if not permit_numbers:
        return {}

    estimated_min = len(permit_numbers) * 5 // 60
    print(f"[Accela] Scrapeando {len(permit_numbers)} permisos (toma ~{estimated_min} min)...")
    results = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()

        for idx, permit_no in enumerate(permit_numbers):
            pct = f"[{idx+1}/{len(permit_numbers)}]"
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
                    info = extract_detail_from_page(page)
                    results[permit_no] = info
                    print(f"  {pct} OK  {permit_no}: {info.get('address','(sin direccion)')}")
                elif "no results" in text.lower():
                    results[permit_no] = {"error": "not_found"}
                    print(f"  {pct} --  {permit_no}: no encontrado")
                else:
                    links = page.query_selector_all('a[href*="CapDetail"]')
                    if links:
                        links[0].click()
                        time.sleep(4)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        info = extract_detail_from_page(page)
                        results[permit_no] = info
                        print(f"  {pct} OK  {permit_no} (multi)")
                    else:
                        results[permit_no] = {"error": "unknown_state"}

            except Exception as e:
                results[permit_no] = {"error": str(e)[:200]}
                print(f"  {pct} ERR {permit_no}: {e}")

            time.sleep(DELAY_BETWEEN_SCRAPES)

        browser.close()

    return results


# ============================================
# 3. GEOCODING
# ============================================
def geocode_address(address, city="Fort Worth", state="TX"):
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
# 4. CLASIFICAR
# ============================================
def classify_permit(applicant, contractor, owner):
    has_contractor = bool(
        contractor
        and contractor.get("name")
        and not contractor.get("error")
        and contractor.get("name").strip()
    )
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
def build_row(permit_no, file_date_ms, accela_info):
    applicant = accela_info.get("applicant") or {}
    contractor = accela_info.get("contractor") or {}
    owner = accela_info.get("owner") or {}

    address = accela_info.get("address")
    lat, lng = None, None
    if address:
        lat, lng = geocode_address(address)
        time.sleep(DELAY_BETWEEN_GEOCODE)

    category = classify_permit(applicant, contractor, owner)

    file_date = (
        datetime.utcfromtimestamp(file_date_ms / 1000).isoformat() + "Z"
        if file_date_ms else None
    )

    return {
        "city": CITY_KEY,
        "permit_number": permit_no,
        "permit_type": accela_info.get("record_type"),
        "status": accela_info.get("record_status"),
        "work_description": accela_info.get("work_description"),
        "address": address,
        "city_name": "Fort Worth",
        "state_code": "TX",
        "latitude": lat,
        "longitude": lng,
        "applicant_name": applicant.get("name"),
        "applicant_email": applicant.get("email"),
        "applicant_phones": applicant.get("phones") or [],
        "owner_name": owner.get("name"),
        "contractor_name": contractor.get("name"),
        "contractor_license": contractor.get("license"),
        "category": category,
        "file_date": file_date,
        "raw_data": {"accela": accela_info},
    }


def upsert_to_supabase(row):
    try:
        supabase.table("permit_leads").upsert(
            row,
            on_conflict="city,permit_number"
        ).execute()
        return True
    except Exception as e:
        print(f"  [Supabase] Error con {row.get('permit_number')}: {e}")
        return False


# ============================================
# MAIN
# ============================================
def main():
    print(f"\n{'='*60}")
    print(f"HUBIDX PERMIT RADAR — Fort Worth")
    print(f"Días hacia atrás: {DAYS_BACK}")
    print(f"Skip existentes: {SKIP_EXISTING}")
    print(f"{'='*60}\n")

    permits = fetch_permit_numbers(DAYS_BACK)
    if not permits:
        print("Sin permisos. Salida.")
        return

    permit_numbers = [p["permit_number"] for p in permits]
    file_date_by_permit = {p["permit_number"]: p["file_date_ms"] for p in permits}

    to_scrape = permit_numbers
    if SKIP_EXISTING:
        already_complete = set()
        CHUNK_SIZE = 100
        print(f"[Plan] Verificando {len(permit_numbers)} permisos en DB (en chunks de {CHUNK_SIZE})...")
        for i in range(0, len(permit_numbers), CHUNK_SIZE):
            chunk = permit_numbers[i:i + CHUNK_SIZE]
            try:
                resp = supabase.table("permit_leads") \
                    .select("permit_number, address") \
                    .eq("city", CITY_KEY) \
                    .in_("permit_number", chunk) \
                    .execute()
                for r in (resp.data or []):
                    if r.get("address"):
                        already_complete.add(r["permit_number"])
            except Exception as e:
                print(f"  [DB] Error en chunk {i}-{i+CHUNK_SIZE}: {e}")

        to_scrape = [p for p in permit_numbers if p not in already_complete]
        print(f"[Plan] {len(already_complete)} ya completos en DB")
        print(f"[Plan] {len(to_scrape)} a scrapear\n")

    if not to_scrape:
        print("Nada nuevo que scrapear. Fin.")
        return

    accela_data = scrape_accela_for_permits(to_scrape)

    print(f"\n[Save] Procesando {len(accela_data)} resultados...")
    saved, failed = 0, 0
    for permit_no, accela in accela_data.items():
        if accela.get("error"):
            failed += 1
            continue
        row = build_row(permit_no, file_date_by_permit.get(permit_no), accela)
        if upsert_to_supabase(row):
            saved += 1
        else:
            failed += 1

    print(f"\n{'='*60}")
    print(f"DONE. Guardados: {saved}  Errores: {failed}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
