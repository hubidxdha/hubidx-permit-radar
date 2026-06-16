#!/usr/bin/env python3
"""
Hubidx Permit Radar — RE-VERIFICADOR (refresh.py)
====================================================
Corre como segundo cron en Railway, SEPARADO del scraper principal.

Qué hace:
1. Toma los permisos que ya están en la DB (los no-stale)
2. Los re-consulta en ArcGIS por su Permit_No (en lotes)
3. Si el estatus cambió a "muerto" (finaled, closed, etc.) → marca is_stale=true
4. Si sigue vivo → actualiza el estatus y last_verified_at

Esto mantiene la base "fresca": un permiso que se finalizó deja de aparecer
en el mapa porque is_stale=true.

Variables de entorno (Railway):
- SUPABASE_URL
- SUPABASE_SERVICE_ROLE_KEY
- REFRESH_BATCH_SIZE (default 200) — cuántos permisos re-verificar por corrida
- REFRESH_MAX_AGE_DAYS (default 90) — no re-verificar permisos más viejos que esto
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

try:
    from supabase import create_client, Client
except ImportError:
    sys.exit("pip install supabase")

# ============================================
# CONFIG
# ============================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
BATCH_SIZE = int(os.environ.get("REFRESH_BATCH_SIZE", "200"))
MAX_AGE_DAYS = int(os.environ.get("REFRESH_MAX_AGE_DAYS", "90"))
CITY_KEY = "fort_worth"

# Estados que significan "ya no sirve como lead"
DEAD_STATUSES = {
    "finaled", "final", "completed", "closed", "void", "voided",
    "expired-final", "permit final", "co issued", "co finaled",
    "permit final - certificate of occupancy", "c of o finaled",
    "certificate of occupancy issued", "expired", "cancelled", "canceled",
    "withdrawn", "denied", "rejected",
}

if not SUPABASE_URL or not SUPABASE_KEY:
    sys.exit("ERROR: faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")

ARCGIS_BASE = (
    "https://services5.arcgis.com/3ddLCBXe1bRt7mzj/arcgis/rest/services/"
    "CFW_Open_Data_Development_Permits_View/FeatureServer/0/query"
)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ============================================
# 1. Obtener permisos a re-verificar
# ============================================
def get_permits_to_verify():
    """
    Trae los permisos NO-stale, priorizando los que:
    - Nunca se verificaron (last_verified_at NULL)
    - Se verificaron hace más tiempo
    Solo permisos con file_date dentro de MAX_AGE_DAYS.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)).isoformat()

    print(f"[Refresh] Buscando hasta {BATCH_SIZE} permisos a re-verificar...", flush=True)
    print(f"[Refresh] Solo permisos con file_date >= {cutoff[:10]}", flush=True)

    resp = (
        supabase.table("permit_leads")
        .select("permit_number, status, file_date, last_verified_at")
        .eq("city", CITY_KEY)
        .or_("is_stale.is.null,is_stale.eq.false")
        .gte("file_date", cutoff)
        .order("last_verified_at", desc=False, nullsfirst=True)
        .limit(BATCH_SIZE)
        .execute()
    )

    permits = resp.data or []
    print(f"[Refresh] {len(permits)} permisos para re-verificar", flush=True)
    return permits


# ============================================
# 2. Consultar estatus actual en ArcGIS
# ============================================
def fetch_current_statuses(permit_numbers):
    """
    Consulta ArcGIS por una lista de Permit_No.
    Devuelve dict {permit_number: current_status}
    """
    if not permit_numbers:
        return {}

    statuses = {}
    CHUNK = 50  # ArcGIS tiene límite de longitud de URL

    for i in range(0, len(permit_numbers), CHUNK):
        chunk = permit_numbers[i:i + CHUNK]
        # Construir cláusula WHERE con IN
        quoted = ",".join([f"'{pn}'" for pn in chunk])
        where = f"Permit_No IN ({quoted})"

        try:
            r = requests.get(ARCGIS_BASE, params={
                "where": where,
                "outFields": "Permit_No,Current_Status,Status_Date",
                "f": "json",
                "resultRecordCount": CHUNK,
            }, timeout=30)
            data = r.json()
            for f in data.get("features", []):
                attrs = f.get("attributes", {})
                pn = attrs.get("Permit_No", "")
                status = (attrs.get("Current_Status") or "").strip()
                if pn:
                    statuses[pn] = status
        except Exception as e:
            print(f"  [ArcGIS] Error en chunk {i}: {str(e)[:100]}", flush=True)

        time.sleep(0.5)  # ser amable con ArcGIS

    return statuses


# ============================================
# 3. Actualizar la DB
# ============================================
def update_permit(permit_number, new_status, is_dead):
    now = datetime.now(timezone.utc).isoformat()
    update_data = {
        "status": new_status,
        "last_verified_at": now,
    }
    if is_dead:
        update_data["is_stale"] = True
        update_data["stale_reason"] = f"Status changed to '{new_status}'"

    try:
        supabase.table("permit_leads") \
            .update(update_data) \
            .eq("city", CITY_KEY) \
            .eq("permit_number", permit_number) \
            .execute()
        return True
    except Exception as e:
        print(f"  [DB] Error actualizando {permit_number}: {str(e)[:80]}", flush=True)
        return False


def mark_not_found_stale(permit_number):
    """Si un permiso ya no aparece en ArcGIS, lo marcamos stale también."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table("permit_leads") \
            .update({
                "is_stale": True,
                "stale_reason": "No longer in ArcGIS",
                "last_verified_at": now,
            }) \
            .eq("city", CITY_KEY) \
            .eq("permit_number", permit_number) \
            .execute()
        return True
    except Exception as e:
        print(f"  [DB] Error: {str(e)[:80]}", flush=True)
        return False


# ============================================
# MAIN
# ============================================
def main():
    print(f"\n{'='*60}", flush=True)
    print(f"HUBIDX PERMIT REFRESH — Re-verificador", flush=True)
    print(f"Batch: {BATCH_SIZE} | Max age: {MAX_AGE_DAYS}d", flush=True)
    print(f"{'='*60}\n", flush=True)

    permits = get_permits_to_verify()
    if not permits:
        print("Nada que re-verificar. Fin.", flush=True)
        return

    permit_numbers = [p["permit_number"] for p in permits]
    old_statuses = {p["permit_number"]: (p.get("status") or "").lower().strip() for p in permits}

    print(f"\n[ArcGIS] Consultando estatus actual de {len(permit_numbers)} permisos...", flush=True)
    current_statuses = fetch_current_statuses(permit_numbers)
    print(f"[ArcGIS] {len(current_statuses)} encontrados en ArcGIS\n", flush=True)

    stats = {
        "marked_stale": 0,
        "status_updated": 0,
        "unchanged": 0,
        "not_found": 0,
    }

    for pn in permit_numbers:
        new_status = current_statuses.get(pn)

        # Caso 1: ya no existe en ArcGIS → marcar stale
        if new_status is None:
            if mark_not_found_stale(pn):
                stats["not_found"] += 1
                print(f"  GONE  {pn} → ya no está en ArcGIS, stale", flush=True)
            continue

        new_lower = new_status.lower().strip()
        old_lower = old_statuses.get(pn, "")
        is_dead = new_lower in DEAD_STATUSES

        # Caso 2: estatus muerto → marcar stale
        if is_dead:
            if update_permit(pn, new_status, is_dead=True):
                stats["marked_stale"] += 1
                print(f"  DEAD  {pn} → '{new_status}' (stale)", flush=True)
            continue

        # Caso 3: estatus cambió pero sigue vivo → actualizar
        if new_lower != old_lower:
            if update_permit(pn, new_status, is_dead=False):
                stats["status_updated"] += 1
                print(f"  UPD   {pn} → '{old_lower}' a '{new_status}'", flush=True)
            continue

        # Caso 4: sin cambios → solo actualizar last_verified_at
        if update_permit(pn, new_status, is_dead=False):
            stats["unchanged"] += 1

    print(f"\n{'='*60}", flush=True)
    print(f"REFRESH DONE:", flush=True)
    print(f"  Marcados stale (muertos):     {stats['marked_stale']}", flush=True)
    print(f"  Marcados stale (no en ArcGIS): {stats['not_found']}", flush=True)
    print(f"  Estatus actualizado:           {stats['status_updated']}", flush=True)
    print(f"  Sin cambios:                   {stats['unchanged']}", flush=True)
    print(f"{'='*60}\n", flush=True)


if __name__ == "__main__":
    main()