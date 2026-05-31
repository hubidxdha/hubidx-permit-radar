#!/usr/bin/env python3
"""
Hubidx Permit Radar Scraper — Fort Worth (v4)
- Usa los nombres REALES de campos de ArcGIS
- ArcGIS da casi todo (address, owner, value, etc.)
- Accela solo se usa para sacar contactos (email, phones)
- Filtros inteligentes por Current_Status
- Guardado incremental
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

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
ISSUED_MAX_AGE_DAYS = int(os.environ.get("ISSUED_MAX_AGE_DAYS", "7"))
SCRAPE_ACCELA = os.environ.get("SCRAPE_ACCELA", "true").lower() == "true"
CITY_KEY = "fort_worth"

# Estatus que EXCLUIMOS (ya no hay nada que vender)
EXCLUDE_STATUSES = {
    "finaled", "final", "completed", "closed", "void", "voided",
    "expired-final", "permit final", "co issued", "co finaled",
    "permit final - certificate of occupancy", "c of o finaled",
}

# Estatus que SIEMPRE incluimos sin importar la fecha
ALWAYS_INCLUDE_STATUSES = {
    "in review", "plan review", "pending", "pending review",
    "submitted", "intake", "routing", "under review", "in plan review",
    "cancelled", "canceled", "withdrawn", "expired", "rejected",
    "denied", "on hold", "hold", "incomplete", "resubmittal required",
    "ready for review", "reviewer assigned",
}

# Estatus que SOLO incluimos si File_Date es reciente
RECENT_ONLY_STATUSES = {
    "issued", "active", "approved", "ready to issue",
    "permit issued", "approved for permit", "ready to print",
}

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
# 1. ARCGIS — con TODOS los campos relevantes
# ============================================
def fetch_permits_with_metadata(days):
    """Pide a ArcGIS los permisos con TODA la info que ya trae."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    offset, results = 0, []
    print(f"[ArcGIS] Listando permisos de los últimos {days} días...", flush=True)
    while True:
        r = requests.get(ARCGIS_BASE, params={
            "where": f"File_Date >= timestamp '{cutoff}'",
            "outFields": (
                "Permit_No,Permit_Type,Permit_SubType,Permit_Category,"
                "B1_WORK_DESC,B1_SPECIAL_TEXT,"
                "Full_Street_Address,Zip_Code,"
                "Owner_Full_Name,"
                "File_Date,Current_Status,Status_Date,"
                "JobValue,Use_Type,Specific_Use,SqFt,Units,Location_1"
            ),
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": 1000,
            "orderByFields": "File_Date DESC",
        }, timeout=30)
        data = r.json()
        feats = data.get("features", [])
        for f in feats:
            attrs = f.get("attributes", {})
            pn = attrs.get("Permit_No", "")
            if pn:
                results.append({
                    "permit_number": pn,
                    "permit_type": (attrs.get("Permit_Type") or "").strip(),
                    "permit_subtype": (attrs.get("Permit_SubType") or "").strip(),
                    "permit_category": (attrs.get("Permit_Category") or "").strip(),
                    "work_description": (attrs.get("B1_WORK_DESC") or attrs.get("B1_SPECIAL_TEXT") or "").strip(),
                    "address": (attrs.get("Full_Street_Address") or "").strip(),
                    "zip_code": str(attrs.get("Zip_Code") or "").strip() if attrs.get("Zip_Code") else None,
                    "owner_name": (attrs.get("Owner_Full_Name") or "").strip(),
                    "file_date_ms": attrs.get("File_Date"),
                    "current_status": (attrs.get("Current_Status") or "").strip(),
                    "status_date_ms": attrs.get("Status_Date"),
                    "job_value": attrs.get("JobValue"),
                    "use_type": (attrs.get("Use_Type") or "").strip(),
                    "specific_use": (attrs.get("Specific_Use") or "").strip(),
                    "sqft": (attrs.get("SqFt") or "").strip() if attrs.get("SqFt") else None,
                    "units": (attrs.get("Units") or "").strip() if attrs.get("Units") else None,
                    "location_1": (attrs.get("Location_1") or "").strip(),
                })
        if not feats or not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    seen = {}
    for p in results:
        seen[p["permit_number"]] = p
    print(f"[ArcGIS] {len(seen)} permisos únicos", flush=True)
    return list(seen.values())


def filter_sellable_permits(permits):
    """Filtros para quedarnos con permits 'vendibles'."""
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    max_age_ms = ISSUED_MAX_AGE_DAYS * 24 * 60 * 60 * 1000

    # Resumen por estatus
    status_counts = {}
    for p in permits:
        s = p["current_status"].lower() or "(empty)"
        status_counts[s] = status_counts.get(s, 0) + 1

    print(f"\n[Filter] Resumen por estatus en ArcGIS:", flush=True)
    for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {c:6d}  {s}", flush=True)

    kept = []
    excluded = {"obvious": 0, "issued_old": 0, "empty": 0}
    unknown_statuses = set()

    for p in permits:
        s = p["current_status"].lower().strip()

        if not s:
            excluded["empty"] += 1
            continue

        if s in EXCLUDE_STATUSES:
            excluded["obvious"] += 1
            continue

        if s in ALWAYS_INCLUDE_STATUSES:
            kept.append(p)
            continue

        if s in RECENT_ONLY_STATUSES:
            file_date_ms = p.get("file_date_ms")
            if file_date_ms and (now_ms - file_date_ms) <= max_age_ms:
                kept.append(p)
            else:
                excluded["issued_old"] += 1
            continue

        # Estatus desconocido — incluir para no perder oportunidades
        unknown_statuses.add(s)
        kept.append(p)

    print(f"\n[Filter] Resultados:", flush=True)
    print(f"  Excluidos (finaled/closed): {excluded['obvious']}", flush=True)
    print(f"  Excluidos (Issued > {ISSUED_MAX_AGE_DAYS}d): {excluded['issued_old']}", flush=True)
    print(f"  Excluidos (estatus vacío): {excluded['empty']}", flush=True)
    if unknown_statuses:
        print(f"  Estatus desconocidos incluidos: {sorted(unknown_statuses)}", flush=True)
    print(f"  → TOTAL A PROCESAR: {len(kept)}\n", flush=True)
    return kept


# ============================================
# 2. ACCELA — solo para contactos (email/phones)
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


def extract_contacts_from_accela(page):
    text = page.inner_text("body")
    info = {}

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

    return info


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
        print(f"  [Geocode] Error: {e}", flush=True)
    return None, None


# ============================================
# 4. CLASIFICAR
# ============================================
def classify_permit(applicant, contractor, owner_name):
    has_contractor = bool(
        contractor
        and contractor.get("name")
        and contractor.get("name").strip()
    )
    if has_contractor:
        return "contractor"

    applicant_name = (applicant or {}).get("name", "").lower().strip()
    owner_lower = (owner_name or "").lower().strip()

    if applicant_name and owner_lower and applicant_name == owner_lower:
        return "homeowner"
    if applicant_name and not has_contractor:
        return "homeowner"
    if owner_lower and not has_contractor:
        return "homeowner"

    return "unknown"


# ============================================
# 5. BUILD ROW + UPSERT
# ============================================
def parse_job_value_cents(raw):
    """JobValue viene como string, convertir a centavos."""
    if not raw:
        return None
    try:
        # Quitar todo lo no numérico excepto punto
        cleaned = re.sub(r"[^\d.]", "", str(raw))
        if not cleaned:
            return None
        return int(float(cleaned) * 100)
    except Exception:
        return None


def build_row(meta, accela_info=None):
    accela_info = accela_info or {}
    applicant = accela_info.get("applicant") or {}
    contractor = accela_info.get("contractor") or {}

    address = meta.get("address")
    lat, lng = None, None

    # Si ArcGIS dio Location_1 con coords, usar eso. Si no, geocodificar.
    loc1 = meta.get("location_1") or ""
    coord_match = re.search(r"\(([-\d.]+),\s*([-\d.]+)\)", loc1)
    if coord_match:
        try:
            lat = float(coord_match.group(1))
            lng = float(coord_match.group(2))
        except Exception:
            pass

    if (lat is None or lng is None) and address:
        lat, lng = geocode_address(address)
        time.sleep(DELAY_BETWEEN_GEOCODE)

    category = classify_permit(applicant, contractor, meta.get("owner_name"))

    file_date_ms = meta.get("file_date_ms")
    file_date = (
        datetime.utcfromtimestamp(file_date_ms / 1000).isoformat() + "Z"
        if file_date_ms else None
    )

    # Combinar permit_type con subtype para más detalle
    permit_type_full = meta.get("permit_type") or ""
    if meta.get("permit_subtype"):
        permit_type_full = f"{permit_type_full} - {meta['permit_subtype']}".strip(" -")

    return {
        "city": CITY_KEY,
        "permit_number": meta["permit_number"],
        "permit_type": permit_type_full or None,
        "status": meta.get("current_status"),
        "work_description": meta.get("work_description") or None,
        "address": address or None,
        "city_name": "Fort Worth",
        "state_code": "TX",
        "zip_code": meta.get("zip_code"),
        "latitude": lat,
        "longitude": lng,
        "applicant_name": applicant.get("name") or None,
        "applicant_email": applicant.get("email") or None,
        "applicant_phones": applicant.get("phones") or [],
        "owner_name": meta.get("owner_name") or None,
        "contractor_name": contractor.get("name") or None,
        "contractor_license": contractor.get("license") or None,
        "category": category,
        "permit_value_cents": parse_job_value_cents(meta.get("job_value")),
        "file_date": file_date,
        "raw_data": {"arcgis": meta, "accela": accela_info},
    }


def upsert_to_supabase(row):
    try:
        supabase.table("permit_leads").upsert(
            row,
            on_conflict="city,permit_number"
        ).execute()
        return True
    except Exception as e:
        print(f"  [Supabase] Error con {row.get('permit_number')}: {e}", flush=True)
        return False


# ============================================
# 6. PROCESS LOOP — GUARDADO INCREMENTAL
# ============================================
def process_permits(sellable):
    if not sellable:
        return 0, 0

    total = len(sellable)
    if SCRAPE_ACCELA:
        estimated_min = total * 5 // 60
        print(f"[Process] {total} permisos. Accela ON → ~{estimated_min} min estimados", flush=True)
    else:
        print(f"[Process] {total} permisos. Accela OFF → solo ArcGIS data", flush=True)
    print(f"[Process] Guardado INCREMENTAL (fila por fila)\n", flush=True)

    saved = 0
    failed = 0
    page = None
    browser = None
    pw_context = None

    if SCRAPE_ACCELA:
        pw_context = sync_playwright().start()
        browser = pw_context.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()

    try:
        for idx, meta in enumerate(sellable):
            permit_no = meta["permit_number"]
            pct = f"[{idx+1}/{total}]"
            accela_info = {}

            # SCRAPE ACCELA (solo para contactos)
            if SCRAPE_ACCELA and page:
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
                        accela_info = extract_contacts_from_accela(page)
                    elif "no results" not in text.lower():
                        links = page.query_selector_all('a[href*="CapDetail"]')
                        if links:
                            links[0].click()
                            time.sleep(4)
                            page.wait_for_load_state("networkidle", timeout=15000)
                            accela_info = extract_contacts_from_accela(page)
                except Exception as e:
                    print(f"  {pct} ACCELA-ERR {permit_no}: {str(e)[:100]}", flush=True)
                    # Continuamos sin contactos pero igual guardamos lo de ArcGIS

            # GUARDAR
            row = build_row(meta, accela_info)
            if upsert_to_supabase(row):
                saved += 1
                addr = row.get("address") or "(sin dir)"
                cat = row.get("category")
                has_email = "📧" if row.get("applicant_email") else ""
                has_phone = "📞" if row.get("applicant_phones") else ""
                print(f"  {pct} OK [{cat}] {permit_no} {has_email}{has_phone} {addr[:45]}", flush=True)
            else:
                failed += 1

            if SCRAPE_ACCELA:
                time.sleep(DELAY_BETWEEN_SCRAPES)

    finally:
        if browser:
            browser.close()
        if pw_context:
            pw_context.stop()

    return saved, failed


# ============================================
# MAIN
# ============================================
def main():
    print(f"\n{'='*60}", flush=True)
    print(f"HUBIDX PERMIT RADAR — Fort Worth (v4)", flush=True)
    print(f"Días hacia atrás: {DAYS_BACK}", flush=True)
    print(f"Skip existentes: {SKIP_EXISTING}", flush=True)
    print(f"Issued max age: {ISSUED_MAX_AGE_DAYS} días", flush=True)
    print(f"Scrape Accela: {SCRAPE_ACCELA}", flush=True)
    print(f"{'='*60}\n", flush=True)

    permits = fetch_permits_with_metadata(DAYS_BACK)
    if not permits:
        print("Sin permisos. Salida.", flush=True)
        return

    sellable = filter_sellable_permits(permits)
    if not sellable:
        print("Ningún permiso pasa el filtro. Salida.", flush=True)
        return

    # Skip existentes (chunks)
    if SKIP_EXISTING:
        permit_numbers = [p["permit_number"] for p in sellable]
        already_complete = set()
        CHUNK_SIZE = 100
        print(f"[DB] Verificando {len(permit_numbers)} permisos en chunks de {CHUNK_SIZE}...", flush=True)
        for i in range(0, len(permit_numbers), CHUNK_SIZE):
            chunk = permit_numbers[i:i + CHUNK_SIZE]
            try:
                resp = supabase.table("permit_leads") \
                    .select("permit_number, applicant_email, applicant_phones") \
                    .eq("city", CITY_KEY) \
                    .in_("permit_number", chunk) \
                    .execute()
                for r in (resp.data or []):
                    # Solo skip si YA tiene contactos (email o phones)
                    if r.get("applicant_email") or (r.get("applicant_phones") and len(r["applicant_phones"]) > 0):
                        already_complete.add(r["permit_number"])
            except Exception as e:
                print(f"  [DB] Error: {e}", flush=True)

        sellable = [p for p in sellable if p["permit_number"] not in already_complete]
        print(f"[DB] {len(already_complete)} ya con contactos → quedan {len(sellable)} a procesar\n", flush=True)

    if not sellable:
        print("Nada nuevo. Fin.", flush=True)
        return

    saved, failed = process_permits(sellable)

    print(f"\n{'='*60}", flush=True)
    print(f"DONE. Guardados: {saved}  Errores: {failed}", flush=True)
    print(f"{'='*60}\n", flush=True)


if __name__ == "__main__":
    main()
