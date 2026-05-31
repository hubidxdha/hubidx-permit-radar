#!/usr/bin/env python3
"""
Hubidx Permit Radar Scraper — Fort Worth (v3)
- Filtros inteligentes: solo permits "vendibles"
- Guardado incremental fila por fila (a prueba de cortes)
- Logs en tiempo real
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
CITY_KEY = "fort_worth"

# Estatus que EXCLUIMOS siempre (ya no hay nada que vender)
EXCLUDE_STATUSES = {
    "finaled", "final", "completed", "closed", "void", "voided",
    "expired-final", "permit final", "co issued",  # variantes comunes
}

# Estatus que SIEMPRE incluimos sin importar la fecha
ALWAYS_INCLUDE_STATUSES = {
    "in review", "plan review", "pending", "pending review",
    "submitted", "intake", "routing", "under review",
    "cancelled", "canceled", "withdrawn", "expired", "rejected",
    "denied", "on hold", "hold",
}

# Estatus que SOLO incluimos si File_Date < ISSUED_MAX_AGE_DAYS
RECENT_ONLY_STATUSES = {
    "issued", "active", "approved", "ready to issue",
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
# 1. ARCGIS — con info de estatus para filtrar
# ============================================
def fetch_permits_with_metadata(days):
    """Pide a ArcGIS los permisos con su estatus y file_date."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    offset, results = 0, []
    print(f"[ArcGIS] Listando permisos de los últimos {days} días...", flush=True)
    while True:
        r = requests.get(ARCGIS_BASE, params={
            "where": f"File_Date >= timestamp '{cutoff}'",
            "outFields": "Permit_No,File_Date,Record_Status,Record_Type",
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
                    "file_date_ms": attrs.get("File_Date"),
                    "record_status": (attrs.get("Record_Status") or "").strip(),
                    "record_type": (attrs.get("Record_Type") or "").strip(),
                })
        if not feats or not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    # Dedup por permit_number
    seen = {}
    for p in results:
        seen[p["permit_number"]] = p
    print(f"[ArcGIS] {len(seen)} permisos únicos", flush=True)
    return list(seen.values())


def filter_sellable_permits(permits):
    """Aplica los filtros para quedarnos solo con permits 'vendibles'."""
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    max_age_ms = ISSUED_MAX_AGE_DAYS * 24 * 60 * 60 * 1000

    # Resumen por estatus (para debugging)
    status_counts = {}
    for p in permits:
        s = p["record_status"].lower() or "(empty)"
        status_counts[s] = status_counts.get(s, 0) + 1

    print(f"\n[Filter] Resumen por estatus en ArcGIS:", flush=True)
    for s, c in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  {c:6d}  {s}", flush=True)

    kept = []
    excluded_counts = {"obvious": 0, "issued_old": 0, "empty": 0, "unknown": 0}

    for p in permits:
        s = p["record_status"].lower().strip()

        if not s:
            excluded_counts["empty"] += 1
            continue

        if s in EXCLUDE_STATUSES:
            excluded_counts["obvious"] += 1
            continue

        if s in ALWAYS_INCLUDE_STATUSES:
            kept.append(p)
            continue

        if s in RECENT_ONLY_STATUSES:
            file_date_ms = p.get("file_date_ms")
            if file_date_ms and (now_ms - file_date_ms) <= max_age_ms:
                kept.append(p)
            else:
                excluded_counts["issued_old"] += 1
            continue

        # Estatus desconocido — por default lo incluimos para no perder oportunidades
        excluded_counts["unknown"] += 1
        kept.append(p)

    print(f"\n[Filter] Resultados:", flush=True)
    print(f"  Excluidos (finaled/closed/etc): {excluded_counts['obvious']}", flush=True)
    print(f"  Excluidos (Issued >7 días): {excluded_counts['issued_old']}", flush=True)
    print(f"  Excluidos (estatus vacío): {excluded_counts['empty']}", flush=True)
    print(f"  Estatus desconocido (incluidos): {excluded_counts['unknown']}", flush=True)
    print(f"  → TOTAL A SCRAPEAR: {len(kept)}\n", flush=True)
    return kept


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
def build_row(permit_no, metadata, accela_info):
    applicant = accela_info.get("applicant") or {}
    contractor = accela_info.get("contractor") or {}
    owner = accela_info.get("owner") or {}

    address = accela_info.get("address")
    lat, lng = None, None
    if address:
        lat, lng = geocode_address(address)
        time.sleep(DELAY_BETWEEN_GEOCODE)

    category = classify_permit(applicant, contractor, owner)

    file_date_ms = metadata.get("file_date_ms")
    file_date = (
        datetime.utcfromtimestamp(file_date_ms / 1000).isoformat() + "Z"
        if file_date_ms else None
    )

    # Tipo y estatus: prefiero los de Accela si están, si no los de ArcGIS
    permit_type = accela_info.get("record_type") or metadata.get("record_type")
    status = accela_info.get("record_status") or metadata.get("record_status")

    return {
        "city": CITY_KEY,
        "permit_number": permit_no,
        "permit_type": permit_type,
        "status": status,
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
        "raw_data": {"accela": accela_info, "arcgis": metadata},
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
# 6. SCRAPING LOOP — CON GUARDADO INCREMENTAL
# ============================================
def scrape_and_save(permits_metadata):
    """Scrapea cada permiso Y LO GUARDA INMEDIATAMENTE."""
    if not permits_metadata:
        return 0, 0

    total = len(permits_metadata)
    estimated_min = total * 5 // 60
    print(f"[Accela] Scrapeando {total} permisos (toma ~{estimated_min} min)...", flush=True)
    print(f"[Accela] Cada permiso se GUARDA en DB inmediatamente (a prueba de cortes)\n", flush=True)

    saved = 0
    failed = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()

        for idx, meta in enumerate(permits_metadata):
            permit_no = meta["permit_number"]
            pct = f"[{idx+1}/{total}]"

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
                accela_info = None

                if "Record Details" in text or "Permit Address" in text:
                    accela_info = extract_detail_from_page(page)
                elif "no results" in text.lower():
                    print(f"  {pct} -- {permit_no}: no encontrado en Accela", flush=True)
                    failed += 1
                    time.sleep(DELAY_BETWEEN_SCRAPES)
                    continue
                else:
                    links = page.query_selector_all('a[href*="CapDetail"]')
                    if links:
                        links[0].click()
                        time.sleep(4)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        accela_info = extract_detail_from_page(page)
                    else:
                        print(f"  {pct} ?? {permit_no}: estado desconocido", flush=True)
                        failed += 1
                        time.sleep(DELAY_BETWEEN_SCRAPES)
                        continue

                # GUARDAR INMEDIATAMENTE
                if accela_info:
                    row = build_row(permit_no, meta, accela_info)
                    if upsert_to_supabase(row):
                        saved += 1
                        addr = row.get("address") or "(sin dir)"
                        cat = row.get("category")
                        print(f"  {pct} OK [{cat}] {permit_no}: {addr[:50]}", flush=True)
                    else:
                        failed += 1

            except Exception as e:
                print(f"  {pct} ERR {permit_no}: {str(e)[:100]}", flush=True)
                failed += 1

            time.sleep(DELAY_BETWEEN_SCRAPES)

        browser.close()

    return saved, failed


# ============================================
# MAIN
# ============================================
def main():
    print(f"\n{'='*60}", flush=True)
    print(f"HUBIDX PERMIT RADAR — Fort Worth", flush=True)
    print(f"Días hacia atrás: {DAYS_BACK}", flush=True)
    print(f"Skip existentes: {SKIP_EXISTING}", flush=True)
    print(f"Issued max age: {ISSUED_MAX_AGE_DAYS} días", flush=True)
    print(f"{'='*60}\n", flush=True)

    # 1. ArcGIS con metadata
    permits = fetch_permits_with_metadata(DAYS_BACK)
    if not permits:
        print("Sin permisos. Salida.", flush=True)
        return

    # 2. Filtrar por estatus
    sellable = filter_sellable_permits(permits)
    if not sellable:
        print("Ningún permiso pasa el filtro. Salida.", flush=True)
        return

    # 3. Quitar los que ya están completos en DB
    permit_numbers = [p["permit_number"] for p in sellable]

    if SKIP_EXISTING:
        already_complete = set()
        CHUNK_SIZE = 100
        print(f"[DB] Verificando {len(permit_numbers)} permisos en chunks de {CHUNK_SIZE}...", flush=True)
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
                print(f"  [DB] Error en chunk: {e}", flush=True)

        before = len(sellable)
        sellable = [p for p in sellable if p["permit_number"] not in already_complete]
        print(f"[DB] {len(already_complete)} ya completos en DB → quedan {len(sellable)} a scrapear\n", flush=True)

    if not sellable:
        print("Nada nuevo que scrapear. Fin.", flush=True)
        return

    # 4. Scrapear + guardar incremental
    saved, failed = scrape_and_save(sellable)

    print(f"\n{'='*60}", flush=True)
    print(f"DONE. Guardados: {saved}  Errores: {failed}", flush=True)
    print(f"{'='*60}\n", flush=True)


if __name__ == "__main__":
    main()
