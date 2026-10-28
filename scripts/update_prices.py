#!/usr/bin/env python3
"""AGROX scheduled market data updater.

Pulls public/available sources once per run, updates only validated observations,
keeps provenance and time series, computes comparable annual and real changes,
and preserves last-known values whenever a source fails or lacks a comparable datum.
"""
from __future__ import annotations

import json
import math
import calendar
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "prices.json"
BASE_HEADERS = {"User-Agent": "AGROX-market-data-updater/1.0 (+daily scheduled updates)"}
ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
TODAY = datetime.now(ARG_TZ).date()
NOW_UTC = datetime.now(timezone.utc).isoformat(timespec="seconds")
TIMEOUT = 25

GRAINS_API = "https://granosar.lfcaucino.workers.dev/api/v1"
ARGDATOS_API = "https://api.argentinadatos.com/v1"
DOLARAPI_API = "https://dolarapi.com/v1/dolares"
BCRA_FX_API = "https://api.bcra.gob.ar/estadisticascambiarias/v1.0/Cotizaciones/REF"
CAMPO_ARGENTO = "https://campoargento.com/precios"
CONSIGNATARIAS = "https://www.consignatarias.com.ar/api"
EIA_BRENT = "https://www.eia.gov/dnav/pet/hist/RBRTEd.htm"

SESSION = requests.Session()
SESSION.headers.update(BASE_HEADERS)


def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    r = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_text(url: str) -> str:
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (float, int)):
        return float(value) if math.isfinite(float(value)) else None
    s = str(value).strip().replace("\u00a0", "")
    if not s or s in {"—", "-", "n/a", "null", "None"}:
        return None
    s = re.sub(r"[^0-9,.-]", "", s)
    if "," in s and "." in s:
        # Spanish formatted 1.234,56 vs English 1,234.56.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        f = float(s)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s[:25], fmt).date()
        except ValueError:
            pass
    # Common ISO timestamp with fractional seconds.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def get_date_from_obj(obj: dict) -> date | None:
    for k in ("fecha", "date", "Fecha", "fecha_operacion", "periodo", "period", "timestamp", "observed_at"):
        if k in obj:
            d = parse_date(obj[k])
            if d:
                return d
    return None


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("data", "results", "result"):
            if key in payload and payload[key] is not None:
                return payload[key]
    return payload


def item_by_name(items: list[dict], name: str) -> dict | None:
    return next((x for x in items if x.get("name", "").strip().casefold() == name.casefold()), None)


def update_status(status: dict, key: str, ok: bool, message: str, url: str, records: int = 0, warning: bool = False):
    status[key] = {
        "status": "partial" if ok and warning else ("ok" if ok else "error"),
        "checked_at": NOW_UTC,
        "message": message,
        "url": url,
        "records_updated": int(records),
    }


def merge_obs(item: dict, obs: dict) -> bool:
    """Upsert an observation by date. Returns True when a real observation was added/changed."""
    d = parse_date(obs.get("date"))
    p = num(obs.get("price"))
    if not d or p is None or p <= 0:
        return False
    clean = {
        "date": d.isoformat(),
        "price": p,
        "ars": num(obs.get("ars")),
        "fx": num(obs.get("fx")),
        "source": obs.get("source", item.get("source", "")),
        "quality": obs.get("quality", item.get("quality", "")),
    }
    obs_list = item.setdefault("observations", [])
    for i, old in enumerate(obs_list):
        if old.get("date") == clean["date"]:
            # Prefer a record with a value and refresh metadata on an exact-date update.
            if num(old.get("price")) != clean["price"] or old.get("source") != clean["source"]:
                obs_list[i] = clean
                item["observations"] = sorted(obs_list, key=lambda a: a.get("date", ""))[-2200:]
                return True
            # Fill missing auxiliary fields but do not mark as changed if price stayed stable.
            for k in ("ars", "fx", "quality"):
                if old.get(k) is None and clean.get(k) is not None:
                    old[k] = clean[k]
            return False
    obs_list.append(clean)
    item["observations"] = sorted(obs_list, key=lambda a: a.get("date", ""))[-2200:]
    return True


def as_num_from(d: dict, *keys: str) -> float | None:
    for key in keys:
        if key in d:
            value = num(d[key])
            if value is not None:
                return value
    return None


def nested_key(d: Any, names: tuple[str, ...]) -> Any:
    if not isinstance(d, dict):
        return None
    low = {str(k).casefold(): v for k, v in d.items()}
    for name in names:
        if name.casefold() in low:
            return low[name.casefold()]
    return None


def extract_grain_quotes(record: dict) -> dict[str, dict]:
    """Parse a current or historical Granos.ar record with some schema tolerance."""
    aliases = {"soja": ("soja", "soybeans"), "maiz": ("maiz", "maíz", "maize", "corn"),
               "trigo": ("trigo", "wheat"), "girasol": ("girasol", "sunflower"), "sorgo": ("sorgo", "sorghum")}
    node = record.get("granos") or record.get("precios") or record.get("prices") or record
    result = {}
    for code, variations in aliases.items():
        entry = None
        if isinstance(node, dict):
            for v in variations:
                if v in node:
                    entry = node[v]
                    break
        # Flat schema alternative: soja_usd_tn, soja_ars_tn.
        if entry is None:
            flat = {str(k).casefold().replace("á", "a"): v for k, v in record.items()}
            usd = as_num_from(flat, f"{code}_usd_tn", f"{code}_usd_tn", f"{code}_usd", f"{code}_usd_ton")
            ars = as_num_from(flat, f"{code}_ars_tn", f"{code}_ars_tn", f"{code}_ars", f"{code}_ars_ton")
            if usd is not None or ars is not None:
                result[code] = {"usd_tn": usd, "ars_tn": ars}
            continue
        if isinstance(entry, dict):
            usd = as_num_from(entry, "usd_tn", "usd_t", "usd_ton", "usd", "precio_usd", "usd_tn_cierre")
            ars = as_num_from(entry, "ars_tn", "ars_t", "ars_ton", "ars", "precio_ars", "pesos_tn")
            if usd is not None or ars is not None:
                result[code] = {"usd_tn": usd, "ars_tn": ars}
        elif num(entry) is not None:
            result[code] = {"usd_tn": num(entry), "ars_tn": None}
    return result


def walk_grain_records(node: Any):
    seen = set()
    def walk(obj):
        if isinstance(obj, dict):
            d = get_date_from_obj(obj)
            q = extract_grain_quotes(obj)
            if d and q:
                key = (d.isoformat(), tuple(sorted(q.keys())))
                if key not in seen:
                    seen.add(key)
                    yield d, q, obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)
    yield from walk(node)


def history_value_on_or_before(series: list[dict], target: date, max_back_days: int = 45) -> dict | None:
    eligible = []
    for p in series:
        d = parse_date(p.get("date"))
        if d and d <= target and (target - d).days <= max_back_days:
            eligible.append((d, p))
    return max(eligible, key=lambda z: z[0])[1] if eligible else None


def yearly_comparable(obs: list[dict], current_date: date) -> dict | None:
    target = current_date - timedelta(days=365)
    cand = []
    for o in obs:
        d = parse_date(o.get("date"))
        if d and d < current_date and abs((d - target).days) <= 45 and num(o.get("price")) and num(o.get("price")) > 0:
            cand.append((abs((d - target).days), d, o))
    return min(cand, key=lambda x: (x[0], -x[1].toordinal()))[2] if cand else None


def update_metrics(data: dict, fx_history: list[dict]):
    fx_today_row = max((r for r in fx_history if parse_date(r.get("date")) and parse_date(r.get("date")) <= TODAY and num(r.get("value")) and num(r.get("value")) > 0), key=lambda r: r["date"], default=None)
    fx_today = num(fx_today_row.get("value")) if fx_today_row else None
    for item in data.get("items", []):
        obs = item.get("observations", [])
        current_date = parse_date(item.get("date")) or TODAY
        if not item.get("price") or not obs:
            continue
        current = max((o for o in obs if parse_date(o.get("date")) and parse_date(o.get("date")) <= current_date), key=lambda o: o["date"], default=None)
        prev = yearly_comparable(obs, current_date)
        if not current or not prev:
            # Preserve last known metrics until a genuinely comparable year-back observation exists.
            continue
        if item.get("source_feed") == "maquinac":
            # RNM is a benchmark comparing the previous full-year mean with current Jan-Jun;
            # the adapter already sets those metrics and their non-calendar-year caveat.
            continue
        p_now, p_prev = num(current.get("price")), num(prev.get("price"))
        if not p_now or not p_prev or p_prev <= 0:
            continue
        unit = str(item.get("unit", ""))
        is_usd_physical = unit.strip().upper().startswith("USD/")
        fx_now = num(current.get("fx"))
        if fx_now is None:
            fx_now_date = parse_date(current.get("date")) or current_date
            fx_now_row = history_value_on_or_before(fx_history, fx_now_date)
            fx_now = num(fx_now_row.get("value")) if fx_now_row else None
        if fx_now is None:
            fx_now = fx_today
        fx_prev = num(prev.get("fx"))
        if fx_prev is None:
            fx_prev_row = history_value_on_or_before(fx_history, parse_date(prev["date"]) or current_date)
            fx_prev = num(fx_prev_row.get("value")) if fx_prev_row else None
        if is_usd_physical:
            # Nominal ARS-equivalent change and devaluation from the same FX window;
            # real change then algebraically isolates the USD product price movement.
            if fx_now and fx_prev:
                annual = ((p_now * fx_now) / (p_prev * fx_prev) - 1) * 100
                deval = (fx_now / fx_prev - 1) * 100
                real = ((1 + annual/100) / (1 + deval/100) - 1) * 100 if 1 + deval/100 else None
            else:
                # An annual USD price change can be displayed, but the ARS nominal metric stays untouched without FX.
                annual = (p_now / p_prev - 1) * 100
                deval = None
                real = annual
        else:
            annual = (p_now / p_prev - 1) * 100
            if item.get("name") == "Dólar oficial":
                deval, real = annual, 0.0
            else:
                if fx_now and fx_prev:
                    deval = (fx_now / fx_prev - 1) * 100
                    real = ((1 + annual/100) / (1 + deval/100) - 1) * 100 if 1 + deval/100 else None
                else:
                    deval, real = None, None
        if annual is not None and math.isfinite(annual): item["annual"] = round(annual, 2)
        if deval is not None and math.isfinite(deval): item["deval"] = round(deval, 2)
        if real is not None and math.isfinite(real): item["real"] = round(real, 2)
        item["variationQuality"] = "observed"
        item["variationPeriod"] = f"{prev['date']} → {current.get('date', current_date.isoformat())}"
        item["variationMethod"] = "Calculado automáticamente con dos observaciones comparables de aproximadamente 12 meses. Para precios USD/unidad, la variación nominal es el equivalente ARS; la variación real descuenta la variación del tipo de cambio del mismo período."
        item["variationSourceUrl"] = item.get("source_url")


def add_fx_rows(data: dict, rows: list[dict], source: str, quality: str):
    macro = data.setdefault("macro", {})
    fx_hist = macro.setdefault("fx_history", [])
    count = 0
    for row in rows:
        d = parse_date(row.get("date") or row.get("fecha"))
        value = num(row.get("value") or row.get("venta") or row.get("rate"))
        if not d or not value or value <= 0:
            continue
        rec = {"date": d.isoformat(), "value": value, "source": source, "quality": quality}
        existing = next((i for i, old in enumerate(fx_hist) if old.get("date") == rec["date"]), None)
        if existing is None:
            fx_hist.append(rec); count += 1
        else:
            fx_hist[existing] = rec
    macro["fx_history"] = sorted(fx_hist, key=lambda x: x.get("date", ""))[-5000:]
    return count


def update_bcra_fx(data: dict, status: dict) -> float | None:
    """Try BCRA REF (A3500 reference) first, fallback to official historical series."""
    url = BCRA_FX_API
    start = (TODAY - timedelta(days=800)).isoformat()
    end = TODAY.isoformat()
    rows = []
    method = "BCRA A3500 REF"
    try:
        payload = get_json(url, params={"fechaDesde": start, "fechaHasta": end})
        root = unwrap(payload)
        if isinstance(root, dict):
            root = root.get("results", root.get("cotizaciones", root))
            if isinstance(root, dict) and ("fecha" in root or "date" in root):
                root = [root]
        if isinstance(root, list):
            for r in root:
                if not isinstance(r, dict): continue
                d = get_date_from_obj(r)
                detail = r.get("detalle", r.get("details", []))
                rate = None
                if isinstance(detail, list):
                    for det in detail:
                        if isinstance(det, dict):
                            rate = as_num_from(det, "tipoCotizacion", "tipo_cotizacion", "valor", "cotizacion")
                            if rate and rate > 0: break
                if rate is None:
                    rate = as_num_from(r, "tipoCotizacion", "tipo_cotizacion", "valor", "cotizacion", "value")
                if d and rate and rate > 0:
                    rows.append({"date": d.isoformat(), "value": rate})
        if not rows:
            raise ValueError("La respuesta BCRA no trajo puntos A3500 reconocibles")
        add_fx_rows(data, rows, method, "oficial")
        current = max(rows, key=lambda x: x["date"])
        update_status(status, "BCRA A3500", True, f"Serie oficial cargada ({len(rows)} observaciones)", url, len(rows))
        return float(current["value"])
    except Exception as exc:
        # Fallback: ArgentinaDatos mayorista; explicitly marked as a proxy rather than official A3500.
        fallback_url = f"{ARGDATOS_API}/cotizaciones/dolares/mayorista"
        try:
            payload = get_json(fallback_url)
            arr = payload if isinstance(payload, list) else (unwrap(payload) or [])
            for r in arr if isinstance(arr, list) else []:
                if isinstance(r, dict):
                    d = get_date_from_obj(r); v = as_num_from(r, "venta", "sell", "price")
                    if d and v and v > 0: rows.append({"date": d.isoformat(), "value": v})
            if rows:
                add_fx_rows(data, rows, "ArgentinaDatos/DolarAPI mayorista", "proxy")
                current = max(rows, key=lambda x: x["date"])
                update_status(status, "BCRA A3500", True, f"BCRA no disponible; se usa proxy mayorista ({len(rows)} observaciones). Original: {exc}", fallback_url, len(rows), warning=True)
                return float(current["value"])
        except Exception as fallback_exc:
            update_status(status, "BCRA A3500", False, f"BCRA y proxy mayorista no disponibles: {exc}; {fallback_exc}", url)
        # Last fallback to latest official USD quote history if already loaded.
        macro = data.get("macro", {})
        hist = macro.get("fx_history", [])
        current = max((r for r in hist if num(r.get("value"))), key=lambda x: x.get("date", ""), default=None)
        return num(current.get("value")) if current else None


def update_dollar_series(data: dict, status: dict) -> float | None:
    items = data["items"]
    current_official = None
    for name, endpoint in (("Dólar oficial", "oficial"), ("Dólar MEP", "bolsa")):
        item = item_by_name(items, name)
        url = f"{ARGDATOS_API}/cotizaciones/dolares/{endpoint}"
        try:
            payload = get_json(url)
            arr = payload if isinstance(payload, list) else (unwrap(payload) or [])
            if not isinstance(arr, list): raise ValueError("Formato de histórico no reconocido")
            valid = []
            for r in arr:
                if not isinstance(r, dict): continue
                d = get_date_from_obj(r); val = as_num_from(r, "venta", "sell", "price")
                if d and val and val > 0:
                    valid.append({"date": d.isoformat(), "price": val, "ars": val, "fx": None, "source": "ArgentinaDatos/DolarAPI", "quality": "market"})
            if not valid: raise ValueError("La API no devolvió cotizaciones válidas")
            for o in valid:
                merge_obs(item, o)
            latest = max(valid, key=lambda o: o["date"])
            item.update({"price": latest["price"], "ars": latest["price"], "date": latest["date"],
                         "source": "ArgentinaDatos / DolarAPI", "source_url": url,
                         "quality": "Cotización histórica diaria · fuente secundaria", "currency": "ARS per USD"})
            item.setdefault("variationSourceUrl", url)
            update_status(status, f"{name}", True, f"Cotización diaria; {len(valid)} registros históricos recibidos", url, len(valid))
            if name == "Dólar oficial": current_official = latest["price"]
        except Exception as exc:
            # DolarAPI live endpoint as fallback; no overwrite if invalid.
            fallback = f"{DOLARAPI_API}/{'oficial' if endpoint == 'oficial' else 'bolsa'}"
            try:
                live = get_json(fallback)
                val = as_num_from(live, "venta", "sell", "price")
                d = parse_date(live.get("fechaActualizacion") or live.get("fecha") or TODAY.isoformat())
                if not item or not val or val <= 0 or not d: raise ValueError("Cotización fallback inválida")
                merge_obs(item, {"date": d.isoformat(), "price": val, "ars": val, "source": "DolarAPI", "quality": "market"})
                item.update({"price": val, "ars": val, "date": d.isoformat(), "source": "DolarAPI (último dato)", "source_url": fallback,
                             "quality": "Cotización actual · histórico no disponible"})
                update_status(status, name, True, f"Se actualizó cotización actual; histórico no disponible ({exc})", fallback, 1, warning=True)
                if name == "Dólar oficial": current_official = val
            except Exception as fallback_exc:
                update_status(status, name, False, f"No se pudo actualizar; se conserva el último precio: {exc}; {fallback_exc}", url)
    return current_official


def update_grains(data: dict, status: dict, fx_current: float | None) -> int:
    url = f"{GRAINS_API}/pizarra"
    items = data["items"]
    name_map = {"soja": "Soja", "maiz": "Maíz", "trigo": "Trigo", "girasol": "Girasol", "sorgo": "Sorgo"}
    update_count = 0
    try:
        payload = get_json(url)
        root = unwrap(payload)
        if not isinstance(root, dict): raise ValueError("Formato de pizarra no reconocido")
        d = parse_date(root.get("fecha") or root.get("date")) or TODAY
        fx_bna = as_num_from(root, "tipo_cambio_bna_divisas", "bna_divisas", "tc_bna") or fx_current
        quotes = extract_grain_quotes(root)
        if not quotes:
            raise ValueError("La respuesta no contiene campos USD/t o ARS/t reconocibles")
        for code, name in name_map.items():
            q = quotes.get(code)
            item = item_by_name(items, name)
            if not q or not item: continue
            usd, ars = q.get("usd_tn"), q.get("ars_tn")
            if usd is None and ars is not None and fx_bna:
                usd = ars / fx_bna
            if usd is None or usd <= 0: continue
            item.update({"price": round(usd, 4), "date": d.isoformat(), "source": "Granos.ar · pizarra Rosario",
                         "source_url": f"{GRAINS_API}/pizarra", "quality": "Pizarra observada · fuente secundaria con referencia CAC/BCR"})
            if ars is not None and ars > 0: item["ars"] = ars
            item["source_feed"] = "granos.ar"
            merge_obs(item, {"date": d.isoformat(), "price": usd, "ars": ars, "fx": fx_current or fx_bna, "source": "Granos.ar · pizarra Rosario", "quality": "observed"})
            update_count += 1
        update_status(status, "Granos · pizarra actual", True, f"Cotización para {d.isoformat()} cargada", url, update_count)
    except Exception as exc:
        update_status(status, "Granos · pizarra actual", False, f"Se conservan los últimos datos: {exc}", url)
        return 0

    # Backfill the most recent 365 calendar days, the maximum documented for this endpoint.
    hist_url = f"{GRAINS_API}/pizarra/historico"
    hist_count = 0
    try:
        historic_payload = get_json(hist_url, params={"dias": 365})
        for d, quotes, raw in walk_grain_records(historic_payload):
            bna_rate = as_num_from(raw, "tipo_cambio_bna_divisas", "bna_divisas", "tc_bna")
            date_fx_row = history_value_on_or_before(data.get("macro",{}).get("fx_history",[]), d, max_back_days=45)
            fx_bna = num(date_fx_row.get("value")) if date_fx_row else (fx_current or bna_rate)
            for code, name in name_map.items():
                item = item_by_name(items, name); q = quotes.get(code)
                if not item or not q: continue
                usd, ars = q.get("usd_tn"), q.get("ars_tn")
                if usd is None and ars and fx_bna: usd = ars / fx_bna
                if usd and usd > 0:
                    if merge_obs(item, {"date": d.isoformat(), "price": usd, "ars": ars, "fx": fx_bna, "source": "Granos.ar · pizarra histórica", "quality": "observed"}):
                        hist_count += 1
        for code, name in name_map.items():
            item = item_by_name(items, name)
            if not item: continue
            true = sorted(item.get("observations", []), key=lambda o: o.get("date", ""))
            # Use a compact monthly sample for the chart; keep full daily observations separately.
            monthly = {}
            for o in true:
                dt = parse_date(o.get("date"))
                if dt: monthly[f"{dt.year}-{dt.month:02d}"] = o
            recent = list(monthly.values())[-24:]
            if len(recent) >= 2:
                item["hist"] = [round(float(o["price"]), 4) for o in recent]
                item["histLabels"] = [parse_date(o["date"]).strftime("%b-%y") for o in recent]
        update_status(status, "Granos · histórico", True, f"Serie histórica integrada; {hist_count} nuevas observaciones", hist_url, hist_count)
    except Exception as exc:
        update_status(status, "Granos · histórico", False, f"Se conservan las observaciones existentes: {exc}", hist_url)
    return update_count + hist_count


class VisibleTextParser(HTMLParser):
    BLOCKS = {"br", "p", "div", "li", "tr", "td", "th", "article", "section", "h1", "h2", "h3", "h4", "span"}
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS: self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in self.BLOCKS: self.parts.append("\n")
    def handle_data(self, data): self.parts.append(data)


def html_lines(html: str) -> list[str]:
    parser = VisibleTextParser(); parser.feed(html)
    return [re.sub(r"\s+", " ", x).strip() for x in "".join(parser.parts).splitlines() if x.strip()]


PRICE_PATTERN = re.compile(r"(?:US\$|USD)\s*([0-9][0-9.,]*)\s*(?:/\s*(t(?:n)?|ton(?:elada)?|kg|kilos?|l|lt|litros?|bolsa))?", re.I)


def parse_price_match(text: str) -> tuple[float, str | None] | None:
    m = PRICE_PATTERN.search(text)
    if not m: return None
    val = num(m.group(1)); unit = m.group(2).lower() if m.group(2) else None
    if val is None or val <= 0: return None
    return val, unit


def nearby_prices(lines: list[str], name_pattern: str, allowed_units: set[str], exact_only: bool = False) -> list[tuple[float, str]]:
    regex = re.compile(name_pattern, re.I)
    found = []
    for i, line in enumerate(lines):
        if not regex.search(line): continue
        # Product cards typically render title, package size, and price in the next lines.
        snippet = " ".join(lines[i:min(i+7, len(lines))])
        match = parse_price_match(snippet)
        if not match: continue
        value, unit = match
        normalized = {"tn": "t", "ton": "t", "tonelada": "t", "lt": "l", "litros": "l", "kilo": "kg", "kilos": "kg"}.get(unit or "", unit)
        if normalized not in allowed_units: continue
        if exact_only and i+1 < len(lines) and not regex.search(line): continue
        found.append((value, normalized or ""))
    # Dedupe duplicated matching lines near each card, preserving order.
    out=[]
    for item in found:
        if not any(abs(item[0]-x[0]) < 1e-6 and item[1] == x[1] for x in out): out.append(item)
    return out


def parse_spanish_observed_date(text: str) -> date | None:
    months = {"ene":1,"enero":1,"feb":2,"febrero":2,"mar":3,"marzo":3,"abr":4,"abril":4,
              "may":5,"mayo":5,"jun":6,"junio":6,"jul":7,"julio":7,"ago":8,"agosto":8,
              "sep":9,"sept":9,"septiembre":9,"setiembre":9,"oct":10,"octubre":10,
              "nov":11,"noviembre":11,"dic":12,"diciembre":12}
    m = re.search(r"(?:observado|actualizado)\s+(\d{1,2})\s+de\s+([a-záéíóú]+)\s+de\s+(\d{4})", text, re.I)
    if not m: return None
    month = months.get(m.group(2).casefold())
    if not month: return None
    try: return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError: return None


def nearby_price_records(lines: list[str], name_pattern: str, allowed_units: set[str]) -> list[dict]:
    regex = re.compile(name_pattern, re.I)
    found=[]
    unit_map={"tn":"t","ton":"t","tonelada":"t","lt":"l","litros":"l","kilo":"kg","kilos":"kg"}
    for i,line in enumerate(lines):
        if not regex.search(line): continue
        subset=lines[i:min(i+8,len(lines))]
        joined=" ".join(subset)
        match=parse_price_match(joined)
        if not match: continue
        value,unit=match
        normalized=unit_map.get(unit or "",unit)
        if normalized not in allowed_units: continue
        observed=parse_spanish_observed_date(joined)
        record={"price":value,"unit":normalized,"observed_date":observed.isoformat() if observed else None,"snippet":joined[:280]}
        if not any(x["price"]==record["price"] and x["unit"]==record["unit"] and x["observed_date"]==record["observed_date"] for x in found):
            found.append(record)
    return found


def update_campo_argento(data: dict, status: dict) -> int:
    url = CAMPO_ARGENTO
    try:
        lines = html_lines(get_text(url))
    except Exception as exc:
        update_status(status, "Campo Argento · insumos", False, f"Se conservan los últimos datos: {exc}", url)
        return 0
    specs = [
        ("Urea", r"\bUREA\s+Granel\b", {"t"}, True),
        ("MAP", r"\bMAP\s+Granel\b", {"t"}, True),
        ("DAP", r"\bDAP\s+Granel\b", {"t"}, True),
        ("Glifosato", r"\bGLIFOSATO\b", {"l"}, False),
        ("2,4-D", r"\b2[, ]?4\s*[- ]?D\b", {"l"}, False),
        ("Atrazina", r"\bATRAZINA\b", {"kg"}, False),
        ("Dicamba", r"\bDICAMBA\b", {"l"}, False),
        ("Semilla de trigo", r"\bSemilla trigo (?:fiscalizada|identificada)\b", {"bolsa"}, False),
        ("Semilla de soja", r"\b(?:SEMILLAS? DE SOJA|SEMILLA SOJA|SOJA FISCALIZADA)\b", {"bolsa"}, False),
        ("Semilla de maíz", r"\b(?:SEMILLAS? DE MA[IÍ]Z|MA[IÍ]Z H[IÍ]BRIDO)\b", {"bolsa"}, False),
        ("Semilla de girasol", r"\b(?:SEMILLAS? DE GIRASOL|GIRASOL H[IÍ]BRIDO)\b", {"bolsa"}, False),
    ]
    updated=0
    for name, pat, allowed, exact in specs:
        item = item_by_name(data["items"], name)
        if not item: continue
        values = nearby_price_records(lines, pat, allowed)
        if not values: continue
        # The page can contain multiple formulations; use exact first match for bulk fertilizer
        # and the median for pesticide category proxies. Never invent a fresh observation date
        # when the provider listing has an explicit "observado" timestamp.
        value = values[0]["price"] if exact else statistics.median(v["price"] for v in values)
        observed_dates = [parse_date(v.get("observed_date")) for v in values if v.get("observed_date")]
        observed_date = max(observed_dates) if observed_dates else None
        if not observed_date:
            # A page-check timestamp is not the same thing as the supplier's quote date.
            # Skip the update rather than fabricating a fresh price observation.
            continue
        d = observed_date.isoformat()
        if item.get("price") is None or abs(float(item["price"])-value) > 1e-6 or item.get("date") != d:
            item.update({"price": round(value, 4), "date": d, "source": "Campo Argento / precios de proveedores",
                         "source_url": url, "quality": "Proveedor observado" if exact else "Proxy: mediana de formulaciones listadas por proveedor",
                         "source_feed": "campoargento", "source_observed_date_known": True})
            item["variationQuality"] = "observed" if exact else "proxy"
            merge_obs(item, {"date": d, "price": value, "source": "Campo Argento / precios de proveedores",
                             "quality": "observed" if exact else "proxy"})
            updated += 1
    update_status(status, "Campo Argento · insumos", True,
                  f"Listado inspeccionado; {updated} precios actualizados. Los proxies agregan formulaciones distintas.", url, updated,
                  warning=(updated < len(specs) or any(x.get("variationQuality")=="proxy" and x.get("source_feed")=="campoargento" for x in data["items"])))
    return updated


def find_named_numeric(node: Any, terms: tuple[str, ...]) -> tuple[float, str] | None:
    hits=[]
    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p=f"{path} {k}".casefold()
                if any(t in p for t in terms):
                    n = None
                    if isinstance(v, (int,float,str)): n = num(v)
                    elif isinstance(v, dict): n = as_num_from(v, "ars_l", "ars_por_litro", "precio_ars", "precio", "valor", "price", "value", "precio_promedio")
                    if n and n > 0: hits.append((n,p))
                walk(v,p)
        elif isinstance(obj, list):
            for v in obj: walk(v,path)
    walk(node)
    # prefer keys that explicitly look like price per liter and values plausible in ARS/L.
    hits=[h for h in hits if h[0]>100 and h[0]<10000]
    hits.sort(key=lambda h: (0 if "precio" in h[1] or "price" in h[1] else 1, len(h[1])))
    return hits[0] if hits else None


def find_named_numeric_with_date(node: Any, terms: tuple[str, ...]) -> tuple[float, str, date | None] | None:
    hits=[]
    def walk(obj, path=""):
        if isinstance(obj, dict):
            direct_date = get_date_from_obj(obj)
            for k,v in obj.items():
                p=f"{path} {k}".casefold()
                if any(t in p for t in terms):
                    value = None
                    candidate_date = direct_date
                    if isinstance(v,(int,float,str)):
                        value=num(v)
                    elif isinstance(v,dict):
                        value=as_num_from(v,"ars_l","ars_por_litro","precio_ars","precio","valor","price","value","precio_promedio")
                        candidate_date=get_date_from_obj(v) or direct_date
                    if value and 100 < value < 10000:
                        hits.append((value,p,candidate_date))
                walk(v,p)
        elif isinstance(obj,list):
            for v in obj: walk(v,path)
    walk(node)
    hits.sort(key=lambda h:(0 if "precio" in h[1] or "price" in h[1] else 1,len(h[1])))
    return hits[0] if hits else None


def update_milk_from_granos_indicators(data: dict, status: dict, fx_current: float | None) -> int:
    url=f"{GRAINS_API}/indicadores"
    item=item_by_name(data["items"],"Leche")
    try:
        payload=get_json(url)
        match=find_named_numeric_with_date(payload,("precio_leche", "leche_magyp", "precio de leche", "milk price"))
        if not match:
            raise ValueError("No se encontró un campo de precio de leche en /indicadores")
        ars,path,observed_date=match
        if not observed_date:
            raise ValueError("La respuesta no asocia una fecha de observación al precio de leche; se conserva el dato anterior")
        fxrow=history_value_on_or_before(data.get("macro",{}).get("fx_history",[]),observed_date,max_back_days=45)
        fx_at_date=num(fxrow.get("value")) if fxrow else None
        if not fx_at_date or fx_at_date<=0: raise ValueError("Falta tipo de cambio histórico para convertir ARS/L al período del precio")
        usd=ars/fx_at_date
        d=observed_date.isoformat()
        item.update({"price":round(usd,5),"ars":round(ars,4),"date":d,"source":"Granos.ar / indicador leche MAGyP",
                     "source_url":url,"quality":"Referencia sectorial mensual · fecha de fuente identificada · convertida a USD con A3500 del período", "source_feed":"granosar/indicadores"})
        merge_obs(item,{"date":d,"price":usd,"ars":ars,"fx":fx_at_date,"source":"Granos.ar / indicador leche MAGyP","quality":"benchmark"})
        update_status(status,"Leche · indicador sectorial",True,f"Precio publicado en ARS/L convertido a USD/L (campo {path}, fecha {d})",url,1,warning=True)
        return 1
    except Exception as exc:
        # SSR fallback: Granos.ar publishes the official MAGyP/SIGLeA price in ARS/L and its month.
        page_url = "https://granos.ar/"
        try:
            text = " ".join(html_lines(get_text(page_url)))
            heading = re.search(r"Precio Nacional de la Leche.{0,500}?Precio por litro\s*\$\s*([0-9.,]+).{0,220}?Corresponde a\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\s+(20\d{2})", text, re.I)
            if not heading: raise ValueError("No se pudo reconocer el precio y período mensual de leche en la página")
            ars = num_argentine_thousands(heading.group(1))
            month_names = {"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
            year, month = int(heading.group(3)), month_names[heading.group(2).casefold()]
            period_end = date(year, month, calendar.monthrange(year,month)[1])
            fxrow = history_value_on_or_before(data.get("macro",{}).get("fx_history",[]), period_end, max_back_days=45)
            fx = num(fxrow.get("value")) if fxrow else None
            if not ars or ars<=0 or not fx or fx<=0: raise ValueError("Falta tipo de cambio comparable para convertir el precio mensual a USD/L")
            usd = ars/fx; d = period_end.isoformat()
            item.update({"price":round(usd,6),"ars":ars,"date":d,"source":"Granos.ar · MAGyP/SIGLeA precio mensual de leche",
                         "source_url":page_url,"quality":"Observado mensual oficial en ARS/L; convertido a USD con referencia cambiaria del período",
                         "source_feed":"granosar/indicadores"})
            merge_obs(item,{"date":d,"price":usd,"ars":ars,"fx":fx,"source":"Granos.ar · MAGyP/SIGLeA precio mensual de leche","quality":"official monthly"})
            update_status(status,"Leche · indicador sectorial",True,f"Fallback de página usado porque /indicadores falló ({exc}); SIGLeA {year}-{month:02d} convertido a USD/L",page_url,1,warning=True)
            return 1
        except Exception as fallback_exc:
            update_status(status,"Leche · indicador sectorial",False,f"No se modificó el precio; API: {exc}; página: {fallback_exc}",page_url)
            return 0


def walk_price_rows(node: Any):
    """Yield dict rows that look like category prices from the consignatarias API."""
    if isinstance(node, dict):
        if any(k in node for k in ("price_avg", "precio_promedio", "precioPromedio", "avg", "promedio", "price")):
            yield node
        for v in node.values(): yield from walk_price_rows(v)
    elif isinstance(node, list):
        for v in node: yield from walk_price_rows(v)


def row_category(row: dict) -> str:
    return " ".join(str(row.get(k, "")) for k in ("subcategory", "category", "category_group", "categoria", "categoria_nombre", "name", "nombre", "label")).casefold()


def row_price_ars(row: dict) -> float | None:
    return as_num_from(row, "price_avg", "precio_promedio", "precioPromedio", "avg", "promedio", "price", "precio", "valor")


def update_livestock(data: dict, status: dict, fx_current: float | None) -> int:
    # Public best-effort endpoint; if restricted/rate-limited, it preserves previous values.
    url=f"{CONSIGNATARIAS}/precios?detallado=true"
    try:
        headers={"Accept":"application/json"}
        api_key=os.environ.get("CONSIGNATARIAS_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload=get_json(url, headers=headers)
        root=unwrap(payload)
        d=None
        if isinstance(root,dict): d=parse_date(root.get("fecha") or root.get("date"))
        rows=list(walk_price_rows(root))
        updates=0
        targets=[("Novillo",("novillo",)),("Carne vacuna",("novillo",)),("Ternero",("ternero","terneros","invernada"))]
        for name,terms in targets:
            item=item_by_name(data["items"],name)
            candidates=[]
            for row in rows:
                label=row_category(row)
                price=row_price_ars(row)
                if price and price>500 and any(t in label for t in terms):
                    candidates.append((price,row))
            if not candidates or not item: continue
            # Specific weighted average if present; otherwise median of observed rows in that class.
            vals=sorted(candidates,key=lambda x:x[0])
            ars=statistics.median(v[0] for v in vals)
            rd=parse_date(next((r.get("fecha") for _,r in candidates if r.get("fecha")),None)) or d or TODAY
            frow=history_value_on_or_before(data.get("macro",{}).get("fx_history",[]),rd,max_back_days=45)
            fx_at_date=num(frow.get("value")) if frow else fx_current
            if not fx_at_date or fx_at_date<=0: continue
            price_usd=ars/fx_at_date
            item.update({"price":round(price_usd,5),"ars":round(ars,4),"date":rd.isoformat(),
                         "source":"Consignatarias / MAG · promedio de categoría", "source_url":url,
                         "quality":"Referencia sectorial observada · agregado de categorías", "source_feed":"consignatarias"})
            merge_obs(item,{"date":rd.isoformat(),"price":price_usd,"ars":ars,"fx":fx_at_date,
                            "source":"Consignatarias / MAG · promedio de categoría","quality":"observed"})
            updates += 1
        update_status(status,"Hacienda · Consignatarias/MAG",True,f"API consultada; {updates} categorías con precio compatible actualizadas",url,updates,warning=updates<3)
        return updates
    except Exception as exc:
        update_status(status,"Hacienda · Consignatarias/MAG",False,f"No se reemplazaron datos previos: {exc}",url)
        return 0


class TableParser(HTMLParser):
    def __init__(self): super().__init__(); self.rows=[]; self.row=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row=[]
        elif tag in ("td","th") and self.row is not None: self.cell=[]
    def handle_endtag(self, tag):
        if tag in ("td","th") and self.row is not None and self.cell is not None:
            self.row.append(re.sub(r"\s+"," ","".join(self.cell)).strip()); self.cell=None
        elif tag == "tr" and self.row is not None:
            if self.row: self.rows.append(self.row)
            self.row=None
    def handle_data(self,data):
        if self.cell is not None: self.cell.append(data)


def parse_eia_date(s: str) -> date | None:
    s=re.sub(r"\s+"," ",s.strip())
    for fmt in ("%b %d, %Y","%B %d, %Y","%m/%d/%Y","%Y-%m-%d"):
        try:return datetime.strptime(s,fmt).date()
        except ValueError:pass
    return None


def update_brent(data: dict, status: dict, fx_current: float | None) -> int:
    item=item_by_name(data["items"],"Petróleo")
    try:
        text=get_text(EIA_BRENT)
        parser=TableParser(); parser.feed(text)
        obs=[]
        for row in parser.rows:
            if len(row)<2: continue
            d=None; val=None
            for cell in row:
                d=d or parse_eia_date(cell)
                n=num(cell)
                if n and n>5 and n<500: val=n
            if d and val: obs.append({"date":d.isoformat(),"price":val,"source":"U.S. EIA · Brent spot","quality":"benchmark"})
        if not obs: raise ValueError("No se reconocieron filas de fecha/precio Brent en la tabla de EIA")
        for o in obs: merge_obs(item,o)
        latest=max(obs,key=lambda o:o["date"])
        item.update({"price":latest["price"],"date":latest["date"],"source":"U.S. EIA · Brent spot", "source_url":EIA_BRENT,
                     "quality":"Benchmark spot internacional", "source_feed":"eia-brent"})
        # Create local equivalent history (USD price × FX) when FX is known.
        if fx_current: merge_obs(item,{"date":latest["date"],"price":latest["price"],"fx":fx_current,"source":"U.S. EIA · Brent spot","quality":"benchmark"})
        update_status(status,"Petróleo · Brent spot",True,f"Serie EIA cargada; {len(obs)} filas observadas",EIA_BRENT,len(obs))
        return len(obs)
    except Exception as exc:
        update_status(status,"Petróleo · Brent spot",False,f"Se conserva el último dato: {exc}",EIA_BRENT)
        return 0




def update_agromove_livestock(data: dict, status: dict, fx_history: list[dict]) -> int:
    """Use dated public market pages for novillo/vaca and monthly PampaIA cattle reference for ternero."""
    specs = [
        ("Novillo", "https://agromove.com.ar/cotizaciones/novillo", r"novillo cotiza\s*\$\s*([0-9.,]+)\s*por kg vivo al\s*(\d{2}/\d{2}/\d{4})", False),
        ("Carne vacuna", "https://agromove.com.ar/cotizaciones/vaca", r"vaca cotiza\s*\$\s*([0-9.,]+)\s*por kg vivo al\s*(\d{2}/\d{2}/\d{4})", True),
    ]
    updated=0; warnings=[]
    for name,url,pattern,is_proxy in specs:
        item=item_by_name(data["items"],name)
        try:
            text=" ".join(html_lines(get_text(url)))
            m=re.search(pattern,text,re.I)
            if not m: raise ValueError("no se pudo identificar el precio y la fecha publicados")
            ars=num_argentine_thousands(m.group(1)); d=parse_date(m.group(2))
            if not ars or ars<=0 or not d: raise ValueError("precio o fecha inválidos")
            fxrow=history_value_on_or_before(fx_history,d,max_back_days=45)
            fx=num(fxrow.get("value")) if fxrow else None
            if not fx or fx<=0: raise ValueError("no hay cotización oficial histórica para la fecha del precio")
            usd=ars/fx
            quality="Proxy: precio de vaca en pie del MAG Cañuelas; no es carne al gancho" if is_proxy else "Observado: promedio ponderado de novillo en pie, MAG Cañuelas"
            item.update({"price":round(usd,6),"ars":ars,"date":d.isoformat(),"source":"AgroMove · Mercado Agroganadero de Cañuelas",
                         "source_url":url,"quality":quality,"source_feed":"agromove-livestock"})
            merge_obs(item,{"date":d.isoformat(),"price":usd,"ars":ars,"fx":fx,"source":"AgroMove · MAG Cañuelas","quality":"proxy" if is_proxy else "observed"})
            updated+=1
        except Exception as exc:
            warnings.append(f"{name}: {exc}")
    # Monthly PampaIA/Consignatarias series for ternero/invernada, quoted directly in USD/kg using A3500 average.
    name="Ternero"; url="https://pampaia.ar/agrodata/invernada"; item=item_by_name(data["items"],name)
    try:
        text=" ".join(html_lines(get_text(url)))
        # Prefer the most-sold 180-200 kg range; it is explicit and repeatable month to month.
        m=re.search(r"([0-9][0-9.,]*)\s*USD por kilo:\s*terneros\s*180 a 200 kg\s*\$\s*([0-9][0-9.,]*)",text,re.I)
        period=re.search(r"Del\s*(\d{2}/\d{2}/\d{4})\s*al\s*(\d{2}/\d{2}/\d{4})",text,re.I)
        if not m or not period: raise ValueError("no se encontró el promedio mensual publicado con período")
        usd=num_argentine_thousands(m.group(1)); ars=num_argentine_thousands(m.group(2)); d=parse_date(period.group(2))
        if not usd or not ars or not d: raise ValueError("precio mensual de ternero inválido")
        fx=ars/usd
        item.update({"price":round(usd,6),"ars":ars,"date":d.isoformat(),"source":"PampaIA · Cámara Argentina de Consignatarios / invernada",
                     "source_url":url,"quality":"Observado mensual: terneros 180–200 kg, rango más vendido; USD calculado con A3500 promedio del período",
                     "source_feed":"pampaia-invernada"})
        merge_obs(item,{"date":d.isoformat(),"price":usd,"ars":ars,"fx":fx,"source":"PampaIA · terneros 180–200 kg","quality":"observed monthly"})
        updated+=1
    except Exception as exc:
        warnings.append(f"Ternero: {exc}")
    update_status(status,"Hacienda · AgroMove/PampaIA",True,
                  f"Se actualizaron {updated}/3 categorías con fecha y unidad compatibles" + ("; " + "; ".join(warnings) if warnings else ""),
                  "https://agromove.com.ar/cotizaciones/novillo",updated,warning=(updated<3 or bool(warnings)))
    return updated

def update_surtidores_gasoil(data: dict, status: dict, fx_history: list[dict], fx_current: float | None) -> int:
    """Read the public monthly YPF CABA diesel table; retain ARS and convert with A3500."""
    url = "https://surtidores.com.ar/precios/"
    item = item_by_name(data["items"], "Gas Oil")
    months = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
    try:
        parser = TableParser(); parser.feed(get_text(url))
        current_year = None; year_month_indexes = {}; offset = 1
        monthly_ars = []
        for row in parser.rows:
            if not row: continue
            clean = [str(c).strip() for c in row]
            first_year = next((int(c) for c in clean if re.fullmatch(r"20\d{2}", c)), None)
            contains_months = any(c.casefold() in months for c in clean)
            if first_year and contains_months:
                current_year = first_year
                # If the year occupies column 0, month names align to the same column positions as prices.
                year_month_indexes = {c.casefold(): i for i,c in enumerate(clean) if c.casefold() in months}
                offset = 0
                continue
            if contains_months and not first_year:
                year_month_indexes = {c.casefold(): i for i,c in enumerate(clean) if c.casefold() in months}
                offset = 1
                continue
            if clean[0].casefold() != "gasoil" or current_year is None or not year_month_indexes:
                continue
            for month, idx in year_month_indexes.items():
                # Product rows have a leading product label, header rows sometimes have a leading year.
                pos = idx + offset
                if pos >= len(clean): continue
                value = num_argentine_thousands(clean[pos])
                if value and value > 10:
                    month_no = months.index(month) + 1
                    monthly_ars.append({"date": date(current_year, month_no, 1).isoformat(), "ars": value})
        if not monthly_ars:
            raise ValueError("No se pudo extraer la serie mensual de gasoil de la tabla pública")
        monthly_ars = sorted({r["date"]: r for r in monthly_ars}.values(), key=lambda r:r["date"])
        for rec in monthly_ars:
            dt = parse_date(rec["date"])
            fxrow = history_value_on_or_before(fx_history, dt, max_back_days=45)
            fx = num(fxrow.get("value")) if fxrow else fx_current
            if not fx or fx <= 0: continue
            usd = rec["ars"] / fx
            merge_obs(item, {"date":dt.isoformat(),"price":usd,"ars":rec["ars"],"fx":fx,"source":"Surtidores · YPF CABA · gasoil","quality":"observed monthly"})
        target_month = date(TODAY.year, TODAY.month, 1)
        current_row = next((r for r in reversed(monthly_ars) if parse_date(r["date"]) == target_month), None)
        if not current_row:
            # Public table may not yet contain this month: keep latest published month and disclose its date.
            current_row = monthly_ars[-1]
        dt = parse_date(current_row["date"])
        fxrow = history_value_on_or_before(fx_history, dt, max_back_days=45)
        fx = num(fxrow.get("value")) if fxrow else fx_current
        if not fx or fx <= 0: raise ValueError("No hay tipo de cambio para convertir el precio mensual a USD/L")
        ars = current_row["ars"]; usd = ars/fx
        item.update({"price":round(usd,6),"ars":ars,"date":dt.isoformat(),"source":"Surtidores · YPF CABA · gasoil grado 2",
                     "source_url":url,"quality":"Referencia mensual observada; no es promedio nacional","source_feed":"surtidores"})
        item["hist"] = [round(float(o["price"]),6) for o in item.get("observations",[]) if o.get("source","").startswith("Surtidores")][-24:] or item.get("hist",[])
        series = [o for o in item.get("observations",[]) if o.get("source","").startswith("Surtidores")][-24:]
        if series: item["histLabels"] = [parse_date(o["date"]).strftime("%b-%y") for o in series]
        update_status(status,"Gas Oil · Surtidores",True,f"Serie mensual YPF CABA cargada ({len(monthly_ars)} meses); última observación {dt.isoformat()}",url,len(monthly_ars),warning=(dt.month != TODAY.month or dt.year != TODAY.year))
        return len(monthly_ars)
    except Exception as exc:
        update_status(status,"Gas Oil · Surtidores",False,f"Se conserva último precio: {exc}",url)
        return 0


def num_argentine_thousands(value: Any) -> float | None:
    """Parse Argentine displayed values where dot is often a thousands separator."""
    if value is None: return None
    s = str(value).strip().replace("\u00a0", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):
        s = s.replace(".", "").replace(",", ".")
        return num(s)
    return num(s)


def update_maquinac(data: dict, status: dict, fx_history: list[dict], fx_current: float | None) -> int:
    """Update the published RNM average for new machinery, preserving its half-year caveat."""
    url = "https://maquinac.com/estadistica/rnm-precios-dolares/"
    item = item_by_name(data["items"], "Maquinaria agrícola")
    try:
        page = get_text(url)
        # RNM labels 2026 as preliminary Jan-Jun and shows the USD average per new equipment.
        period_match = re.search(r"primer semestre de (20\d{2}).{0,700}?U\$S\s*([0-9.]+(?:,[0-9]+)?)\s*por equipo", page, re.I | re.S)
        if period_match:
            report_year = int(period_match.group(1))
            current_val = num_argentine_thousands(period_match.group(2))
        else:
            # Some page builds place the value first in an image caption; use the first explicit
            # RNM per-equipment quote and its nearest reporting year, never a hard-coded price.
            current_match = re.search(r"U\$S\s*([0-9.]+(?:,[0-9]+)?)\s*por equipo", page, re.I)
            current_val = num_argentine_thousands(current_match.group(1)) if current_match else None
            year_matches = re.findall(r"primer semestre de (20\d{2})", page, re.I)
            report_year = int(year_matches[0]) if year_matches else TODAY.year
        if current_val is None or current_val <= 0:
            raise ValueError("No se pudo extraer el promedio RNM en USD/equipo")
        # The previous comparable typically follows the current value in the same sentence
        # (e.g. “3.7% below the 2025 mean (U$S 122.330)”). Parse only after the current price
        # to avoid accidentally reading an older article/graphic before the current benchmark.
        current_end = period_match.end() if period_match else current_match.end()
        after_current = page[current_end:current_end + 700]
        prior_match = re.search(r"U\$S\s*([0-9.]+(?:,[0-9]+)?)", after_current, re.I)
        prior_val = num_argentine_thousands(prior_match.group(1)) if prior_match else None
        prior_year = report_year - 1
        if prior_val is None:
            # Historical price may be the latest source-supported value from the previous annual cycle.
            previous_candidates = [o for o in item.get("observations",[]) if parse_date(o.get("date")) and parse_date(o.get("date")).year == prior_year]
            prior_val = num(previous_candidates[-1].get("price")) if previous_candidates else None
        if prior_val is None:
            raise ValueError("RNM current price was found but the previous-year comparable was not; preserving previous item")
        current_date = date(report_year, 6, 30)
        prior_date = date(prior_year, 6, 30)  # period-end markers for annual-average benchmark series.
        fx_now_row = history_value_on_or_before(fx_history, current_date, max_back_days=45)
        fx_prev_row = history_value_on_or_before(fx_history, prior_date, max_back_days=45)
        fx_now = num(fx_now_row.get("value")) if fx_now_row else fx_current
        fx_prev = num(fx_prev_row.get("value")) if fx_prev_row else None
        item.update({"price":round(current_val,2),"date":current_date.isoformat(),"source":"RNM Radar de Negocios Maquinac",
                     "source_url":url,"quality":f"Benchmark sectorial anual; {report_year} preliminar ene-jun vs promedio {prior_year}",
                     "source_feed":"maquinac","variationQuality":"benchmark",
                     "variationPeriod":f"{prior_year} promedio anual → {report_year} promedio enero-junio",
                     "variationMethod":"RNM informa promedios por equipo vendido. El dato del año de reporte es preliminar del primer semestre, por lo que no es una observación diaria ni un promedio anual cerrado."})
        # Historical benchmark observations get period-end markers and remain clearly labelled annual/half-year.
        merge_obs(item,{"date":prior_date.isoformat(),"price":prior_val,"fx":fx_prev,"source":f"RNM Maquinac · promedio {prior_year}","quality":"benchmark annual"})
        merge_obs(item,{"date":current_date.isoformat(),"price":current_val,"fx":fx_now,"source":f"RNM Maquinac · promedio ene-jun {report_year}","quality":"benchmark half-year"})
        item["hist"]=[prior_val,current_val]
        item["histLabels"]=[f"{prior_year} prom.",f"{report_year} ene-jun"]
        variation=(current_val/prior_val-1)*100 if prior_val else None
        if variation is not None:
            item["real"]=round(variation,2)
            if fx_now and fx_prev:
                item["deval"]=round((fx_now/fx_prev-1)*100,2)
                item["annual"]=round(((1+variation/100)*(1+item["deval"]/100)-1)*100,2)
            else:
                item["annual"]=round(variation,2)
                item["deval"]=None
            item["variationSourceUrl"]=url
        update_status(status,"Maquinaria · RNM Maquinac",True,"Precio promedio RNM actualizado; 2026 es preliminar del primer semestre",url,2,warning=True)
        return 2
    except Exception as exc:
        update_status(status,"Maquinaria · RNM Maquinac",False,f"Se conserva el último precio: {exc}",url)
        return 0


def update_pork_agromove(data: dict, status: dict, fx_history: list[dict], fx_current: float | None) -> int:
    url = "https://agromove.com.ar/cotizaciones/capon-porcino"
    item = item_by_name(data["items"], "Cerdo")
    try:
        text = " ".join(html_lines(get_text(url)))
        m = re.search(r"cap[oó]n porcino cotiza\s*\$\s*([0-9.,]+)\s*por\s*kg vivo\s*al\s*(\d{2}/\d{2}/\d{4})", text, re.I)
        if not m: raise ValueError("No se pudo identificar el precio fechado de capón porcino")
        ars = num_argentine_thousands(m.group(1)); d = parse_date(m.group(2))
        if not ars or ars<=0 or not d: raise ValueError("Cotización porcina inválida")
        fxrow = history_value_on_or_before(fx_history,d,max_back_days=45)
        fx = num(fxrow.get("value")) if fxrow else fx_current
        if not fx or fx<=0: raise ValueError("Falta tipo de cambio para expresar el dato en USD/kg")
        usd = ars/fx
        item.update({"price":round(usd,6),"ars":ars,"date":d.isoformat(),"source":"AgroMove · SIO Porcino (MAGyP)",
                     "source_url":url,"quality":"Referencia sectorial semanal observada · capón general kg vivo","source_feed":"agromove-pork"})
        merge_obs(item,{"date":d.isoformat(),"price":usd,"ars":ars,"fx":fx,"source":"AgroMove · SIO Porcino (MAGyP)","quality":"observed weekly"})
        update_status(status,"Cerdo · AgroMove/SIO Porcino",True,f"Capón general actualizado al {d.isoformat()}; convertido a USD/kg con tipo de cambio de referencia",url,1,warning=True)
        return 1
    except Exception as exc:
        update_status(status,"Cerdo · AgroMove/SIO Porcino",False,f"Se conserva último dato: {exc}",url)
        return 0



def update_freight_granos_page(data: dict, status: dict, fx_current: float | None) -> int:
    """Convert the public CATAC-core-region freight reference into today's USD/t equivalent."""
    url="https://granos.ar/"
    item=item_by_name(data["items"],"Flete agrícola")
    months={"enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12}
    try:
        text=" ".join(html_lines(get_text(url)))
        m=re.search(r"Flete\s+zona\s+n[uú]cleo.{0,100}?Rosario.{0,100}?(?:~\s*)?\$\s*([0-9.,]+)\s*/\s*tn",text,re.I)
        if not m:
            # Some versions render the route and price in separate components with more whitespace.
            m=re.search(r"Flete.{0,80}zona n[uú]cleo.{0,100}?Rosario.{0,160}?\$\s*([0-9.,]+)\s*/\s*tn",text,re.I)
        if not m: raise ValueError("No se pudo identificar la referencia ARS/tn de flete núcleo→Rosario")
        ars=num_argentine_thousands(m.group(1))
        tariff=re.search(r"Cuadro tarifario\s*[—–-]?\s*(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\s+(20\d{2})",text,re.I)
        if not tariff: raise ValueError("La página no expone fecha del último cuadro CATAC; se conserva el último precio")
        tariff_date=date(int(tariff.group(2)),months[tariff.group(1).casefold()],1)
        if not ars or ars<=0 or not fx_current or fx_current<=0: raise ValueError("Importe de flete o tipo de cambio inválido")
        usd=ars/fx_current
        item.update({"price":round(usd,5),"ars":ars,"date":TODAY.isoformat(),"source":"Granos.ar · referencia de flete CATAC núcleo→Rosario",
                     "source_url":url,"quality":f"Proxy estimativo: tarifa CATAC {tariff.group(1).capitalize()} {tariff.group(2)} convertida a USD con tipo de cambio actual; no es una cotización de transportista",
                     "source_feed":"granosar-freight","tariff_reference_date":tariff_date.isoformat(),"tariff_reference_ars_t":ars,
                     "conversion_fx_date":TODAY.isoformat()})
        merge_obs(item,{"date":TODAY.isoformat(),"price":usd,"ars":ars,"fx":fx_current,
                        "source":"Granos.ar · referencia CATAC núcleo→Rosario","quality":"proxy tariff converted at current FX"})
        update_status(status,"Logística · flete CATAC/Granos.ar",True,
                      f"ARS {ars:,.0f}/t convertidos a USD/t con TC actual; cuadro subyacente {tariff.group(1).capitalize()} {tariff.group(2)}",url,1,warning=True)
        return 1
    except Exception as exc:
        update_status(status,"Logística · flete CATAC/Granos.ar",False,f"Se conserva el último dato: {exc}",url)
        return 0

def update_pampaia_rent(data: dict, status: dict) -> int:
    url = "https://pampaia.ar/agrodata/arrendamientos"
    item = item_by_name(data["items"], "Alquiler de campo")
    try:
        text = " ".join(html_lines(get_text(url)))
        match = re.search(r"([0-9][0-9.,]*)\s*USD por hect[aá]rea, la regi[oó]n m[aá]s cara",text,re.I)
        if not match: raise ValueError("La página no expone la referencia máxima por hectárea")
        price = num_argentine_thousands(match.group(1))
        if not price or price<=0: raise ValueError("Precio de alquiler inválido")
        dm = re.search(r"Verificado[^0-9]{0,30}(\d{2}/\d{2}/\d{4})",text,re.I)
        d = parse_date(dm.group(1)) if dm else None
        # Page verification date may be shown as 27/10/2026; if not, keep last published item date.
        if d is None:
            d = parse_date(item.get("date")) or TODAY
        old_price = num(item.get("price")); old_source_date = parse_date(item.get("date"))
        # The page's verification timestamp is not the lease/campaign valuation date. Only append
        # a series observation if the value actually changed; otherwise preserve the prior observation date.
        price_changed = old_price is None or abs(old_price-price) > 1e-6
        observation_date = d if price_changed or old_source_date is None else old_source_date
        item.update({"price":price,"date":observation_date.isoformat(),"source":"PampaIA · referencia regional de arrendamientos",
                     "source_url":url,"quality":"Referencia regional máxima (Núcleo Norte), no promedio nacional; campaña agrícola","source_feed":"pampaia-rent",
                     "page_verified_at":d.isoformat()})
        if price_changed or not item.get("observations"):
            merge_obs(item,{"date":observation_date.isoformat(),"price":price,"source":"PampaIA · Núcleo Norte","quality":"regional maximum benchmark"})
        update_status(status,"Tierra · PampaIA arrendamientos",True,f"Página revisada; referencia Núcleo Norte {price:.0f} USD/ha, no representa promedio nacional",url,1,warning=True)
        return 1
    except Exception as exc:
        update_status(status,"Tierra · PampaIA arrendamientos",False,f"Se conserva último dato: {exc}",url)
        return 0


def update_peanuts_bccba(data: dict, status: dict) -> int:
    url = "https://www.bccba.org.ar/"
    item = item_by_name(data["items"], "Maní")
    try:
        lines = html_lines(get_text(url))
        hits=[]
        for i,line in enumerate(lines):
            if not re.search(r"man[ií]",line,re.I): continue
            snippet=" ".join(lines[i:min(i+6,len(lines))])
            pm=parse_price_match(snippet)
            if pm and pm[1] and pm[1].casefold() in {"t","tn","ton"}:
                hits.append((pm[0],snippet))
        if not hits: raise ValueError("No se encontró una cotización fechada y expresada en USD/t en la página")
        price, snippet = hits[0]
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})",snippet)
        observed_date = parse_date(date_match.group(1)) if date_match else None
        if not observed_date:
            raise ValueError("Se encontró un precio candidato pero no una fecha de observación; no se reemplaza el último valor")
        item.update({"price":price,"date":observed_date.isoformat(),"source":"Bolsa de Cereales de Córdoba · referencia publicada",
                     "source_url":url,"quality":"Referencia sectorial observada; validar variedad y condición comercial","source_feed":"bccba-peanut"})
        merge_obs(item,{"date":observed_date.isoformat(),"price":price,"source":"BCCBA · maní","quality":"sectoral reference"})
        update_status(status,"Maní · BCCBA",True,f"Cotización candidata con fecha {observed_date.isoformat()}; confirmar variedad/condición",url,1,warning=True)
        return 1
    except Exception as exc:
        update_status(status,"Maní · BCCBA",False,f"No se sustituyó el valor: {exc}",url)
        return 0

def discover_world_bank_xlsx() -> str:
    landing="https://www.worldbank.org/en/research/commodity-markets"
    html=get_text(landing)
    links=re.findall(r'href=[\"\']([^\"\']+)[\"\']',html,re.I)
    candidates=[urljoin(landing,h) for h in links if "CMO-Historical-Data-Monthly.xlsx" in h]
    if not candidates: raise ValueError("No se encontró el XLSX mensual en la página del World Bank")
    return candidates[0]


def update_world_bank_benchmarks(data: dict, status: dict) -> int:
    """Attach monthly global benchmark series without overwriting local supplier prices."""
    try:
        import io
        import openpyxl
        url=discover_world_bank_xlsx()
        response=SESSION.get(url,timeout=TIMEOUT);response.raise_for_status()
        wb=openpyxl.load_workbook(io.BytesIO(response.content),data_only=True,read_only=True)
        sheet=next((wb[n] for n in wb.sheetnames if "monthly" in n.casefold() and "price" in n.casefold()),wb[wb.sheetnames[0]])
        rows=list(sheet.iter_rows(values_only=True))
        # Find the row containing the monthly-period axis and a row of commodity headers.
        period_row_idx=None
        period_re=re.compile(r"^\d{4}M\d{2}$")
        for i,row in enumerate(rows):
            if any(isinstance(v,str) and period_re.match(v.strip()) for v in row):
                period_row_idx=i;break
        if period_row_idx is None: raise ValueError("No se reconoció la columna de períodos YYYY Mmm en el XLSX")
        header_candidates=[]
        for i in range(max(0,period_row_idx-8),period_row_idx):
            row=rows[i]
            joined=" ".join(str(v) for v in row if v is not None)
            if any(k in joined.casefold() for k in ("urea","dap","potassium chloride","crude oil, brent","soybeans","maize","wheat")):
                header_candidates.append((i,row))
        if not header_candidates: raise ValueError("No se encontraron los nombres de commodities del World Bank")
        # Prefer the last row before periods that contains series names.
        hidx, headers=header_candidates[-1]
        labels={"Soja":("soybeans",),"Maíz":("maize", "corn"),"Trigo":("wheat",),"Urea":("urea",),
                "DAP":("dap fertilizer","dap fertiliser","diammonium phosphate"),
                "Cloruro de potasio (KCl)":("potassium chloride",),"Petróleo":("crude oil, brent","crude oil brent")}
        col_map={}
        for idx,val in enumerate(headers):
            if val is None:continue
            label=str(val).casefold()
            for item_name,terms in labels.items():
                if any(term in label for term in terms) and item_name not in col_map:
                    col_map[item_name]=idx
        period_rows=[]
        for row in rows[period_row_idx:]:
            period=next((v.strip() for v in row if isinstance(v,str) and period_re.match(v.strip())),None)
            if not period:continue
            yy=int(period[:4]); mm=int(period[-2:])
            try:d=date(yy,mm,1)
            except ValueError:continue
            period_rows.append((d,row))
        updates=0
        for name,idx in col_map.items():
            item=item_by_name(data["items"],name)
            if not item:continue
            points=[]
            for d,row in period_rows:
                if idx<len(row):
                    v=num(row[idx])
                    if v and v>0: points.append({"date":d.isoformat(),"price":v,"source":"World Bank Pink Sheet","quality":"benchmark"})
            if not points:continue
            for p in points:
                hist=item.setdefault("benchmark_history",[])
                if not any(o.get("date")==p["date"] and o.get("source")==p["source"] for o in hist): hist.append(p)
            item["benchmark_history"]=sorted(item["benchmark_history"],key=lambda o:o.get("date",""))[-1000:]
            latest=max(points,key=lambda x:x["date"])
            item["benchmark"]={"price":latest["price"],"unit":"USD/t" if name not in ("Petróleo",) else "USD/barril",
                                "date":latest["date"],"source":"World Bank Pink Sheet","source_url":url,"quality":"benchmark mensual internacional"}
            # KCl is explicitly benchmark-priced in the existing dashboard; update its headline value.
            if name=="Cloruro de potasio (KCl)" and "World Bank" in str(item.get("quality", "")):
                item.update({"price":latest["price"],"date":latest["date"],"source":"World Bank Pink Sheet · KCl benchmark",
                             "source_url":url,"quality":"Benchmark mensual internacional (no precio minorista argentino)"})
                merge_obs(item,{"date":latest["date"],"price":latest["price"],"source":"World Bank Pink Sheet · KCl benchmark","quality":"benchmark"})
                kb = item.get("benchmark_history",[])
                sample = sorted([z for z in kb if z.get("source")=="World Bank Pink Sheet"],key=lambda z:z.get("date",""))[-24:]
                if sample:
                    item["hist"]=[round(float(z["price"]),3) for z in sample]
                    item["histLabels"]=[parse_date(z["date"]).strftime("%b-%y") for z in sample]
            # Benchmark annual rate is always distinct from local retail price and labelled as such.
            prior=yearly_comparable(points,TODAY)
            if prior and latest["price"]>0:
                item["benchmark"]["annual_pct"]=round((latest["price"]/prior["price"]-1)*100,2)
            updates+=1
        wb.close()
        update_status(status,"World Bank · benchmarks mensuales",True,f"Benchmarks cargados para {updates} productos; precios locales no sobrescritos",url,updates,warning=True)
        return updates
    except Exception as exc:
        update_status(status,"World Bank · benchmarks mensuales",False,f"No se actualizaron benchmarks; precios guardados intactos: {exc}","https://www.worldbank.org/en/research/commodity-markets")
        return 0


def load_data() -> dict:
    raw=json.loads(DATA_FILE.read_text(encoding="utf-8"))
    if isinstance(raw,list):
        return {"schema_version":1,"updated_at":None,"items":raw,"sources_status":{},"macro":{}}
    raw.setdefault("schema_version",1);raw.setdefault("items",[]);raw.setdefault("sources_status",{});raw.setdefault("macro",{})
    for item in raw["items"]:
        item.setdefault("observations",[])
    return raw


def save_atomic(data: dict):
    data["updated_at"] = datetime.now(ARG_TZ).isoformat(timespec="seconds")
    data["last_checked_at"] = NOW_UTC
    data["update_timezone"] = "America/Argentina/Buenos_Aires"
    tmp=DATA_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    tmp.replace(DATA_FILE)


def main() -> int:
    data=load_data()
    if len(data.get("items",[])) < 20:
        print("ERROR: el dataset trae menos de 20 elementos; abortando para proteger los datos.",file=sys.stderr);return 2
    status=data.setdefault("sources_status",{})
    # 1) Exchange series first so other sources can be converted consistently.
    live_official=update_dollar_series(data,status)
    fx_current=update_bcra_fx(data,status)
    if fx_current is None: fx_current=live_official
    # If BCRA failed but the API includes a grain-specific exchange quote, that is a last-resort daily proxy.
    # 2) Grains and other public feeds.
    update_grains(data,status,fx_current)
    update_milk_from_granos_indicators(data,status,fx_current)
    update_agromove_livestock(data,status,data.get("macro",{}).get("fx_history",[]))
    update_campo_argento(data,status)
    update_brent(data,status,fx_current)
    update_surtidores_gasoil(data,status,data.get("macro",{}).get("fx_history",[]),fx_current)
    update_maquinac(data,status,data.get("macro",{}).get("fx_history",[]),fx_current)
    update_pork_agromove(data,status,data.get("macro",{}).get("fx_history",[]),fx_current)
    update_pampaia_rent(data,status)
    update_freight_granos_page(data,status,fx_current)
    update_peanuts_bccba(data,status)
    update_world_bank_benchmarks(data,status)
    # Record a daily snapshot for all price-bearing items, using current values only where source is still valid.
    for item in data["items"]:
        price=num(item.get("price"))
        if price is None or price <= 0: continue
        # Never fabricate a new observation for a product whose source didn't publish today.
        updated_today = parse_date(item.get("date")) == TODAY
        if updated_today and not any(parse_date(o.get("date"))==TODAY for o in item.get("observations",[])):
            fx = fx_current if str(item.get("unit","")).startswith("USD/") else None
            merge_obs(item,{"date":TODAY.isoformat(),"price":price,"ars":item.get("ars"),"fx":fx,
                            "source":item.get("source",""),"quality":item.get("quality","last-known")})
    # Metrics only become newly observed when a year-back comparable point exists; otherwise retain previous values.
    update_metrics(data,data.get("macro",{}).get("fx_history",[]))
    # Item-level freshness makes it visible which prices have a scheduled adapter and which remain a last-known reference.
    item_source_map = {
        "Soja":"Granos · pizarra actual", "Maíz":"Granos · pizarra actual", "Trigo":"Granos · pizarra actual",
        "Girasol":"Granos · pizarra actual", "Sorgo":"Granos · pizarra actual",
        "Dólar oficial":"Dólar oficial", "Dólar MEP":"Dólar MEP",
        "Carne vacuna":"Hacienda · AgroMove/PampaIA", "Novillo":"Hacienda · AgroMove/PampaIA", "Ternero":"Hacienda · AgroMove/PampaIA",
        "Leche":"Leche · indicador sectorial", "Gas Oil":"Gas Oil · Surtidores", "Petróleo":"Petróleo · Brent spot",
        "Urea":"Campo Argento · insumos", "MAP":"Campo Argento · insumos", "DAP":"Campo Argento · insumos",
        "Glifosato":"Campo Argento · insumos", "2,4-D":"Campo Argento · insumos", "Atrazina":"Campo Argento · insumos",
        "Dicamba":"Campo Argento · insumos", "Semilla de trigo":"Campo Argento · insumos",
        "Semilla de soja":"Campo Argento · insumos", "Semilla de maíz":"Campo Argento · insumos", "Semilla de girasol":"Campo Argento · insumos",
        "Cloruro de potasio (KCl)":"World Bank · benchmarks mensuales",
        "Maquinaria agrícola":"Maquinaria · RNM Maquinac", "Cerdo":"Cerdo · AgroMove/SIO Porcino",
        "Alquiler de campo":"Tierra · PampaIA arrendamientos", "Maní":"Maní · BCCBA",
        "Flete agrícola":"Logística · flete CATAC/Granos.ar"
    }
    for item in data["items"]:
        adapter = item_source_map.get(item.get("name"))
        info = status.get(adapter) if adapter else None
        item["update_mode"] = "scheduled_source" if adapter else "reference_only"
        item["last_checked_at"] = NOW_UTC
        item["refresh_status"] = info.get("status") if info else "no_auto_feed"
        item["refresh_note"] = (info.get("message") if info else "Sin adaptador automático confiable todavía; se conserva el último dato disponible.")
    # Summary used by the UI and logs.
    vals=list(status.values())
    data["update_summary"]={"sources_ok":sum(1 for s in vals if s.get("status")=="ok"),
                             "sources_partial":sum(1 for s in vals if s.get("status")=="partial"),
                             "sources_failed":sum(1 for s in vals if s.get("status")=="error"),
                             "items":len(data["items"]),
                             "note":"El workflow revisa a diario. Sólo cambia un precio si la fuente devuelve una observación válida; los demás conservan su último dato y fecha."}
    save_atomic(data)
    print(json.dumps(data["update_summary"],ensure_ascii=False,indent=2))
    for source, info in status.items():
        print(f"[{info.get('status','?'):7}] {source}: {info.get('message','')}")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
