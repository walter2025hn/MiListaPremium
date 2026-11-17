#!/usr/bin/env python3
"""Controlled M3U ingestion: 50 live, 50 movies and 50 series per manual run."""
from __future__ import annotations
import concurrent.futures, json, os, re, sys, time, unicodedata
import urllib.error, urllib.request
from collections import Counter
from pathlib import Path

ACTIVE = Path("Completa.m3u")
PENDING = Path("contenido_pendiente.m3u")
REPORT = Path("reporte_enlaces.txt")
CAP = 100_000
PER_CATEGORY = 50
MAX_CHECKS_PER_CATEGORY = 600
TIMEOUT = 7
RETRIES = 2
WORKERS = 32
UA = "CodigoMasterPlaylist/2.0"
URL_RE = re.compile(r"^(https?|rtmp|rtsp|udp)://", re.I)
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")

def clean_group(value):
    value = unicodedata.normalize("NFC", value or "")
    # Remove emoji/symbol characters, retaining readable punctuation and letters.
    value = "".join(ch for ch in value if not (unicodedata.category(ch) in ("So", "Sk") or 0x1F000 <= ord(ch) <= 0x1FAFF))
    value = re.sub(r"\s+", " ", value).strip(" -_|")
    return value or "Sin categoria"

def parse_m3u(text):
    entries, block = [], []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            if block:
                entries.append(block)
            block = [line]
        elif block:
            block.append(line)
    if block:
        entries.append(block)
    result = []
    for lines in entries:
        url = next((x for x in lines if URL_RE.match(x)), "")
        if not url:
            continue
        info = lines[0]
        attrs = dict(ATTR_RE.findall(info.split(",", 1)[0]))
        name = info.rsplit(",", 1)[-1].strip() if "," in info else "Sin titulo"
        group = clean_group(attrs.get("group-title", ""))
        # Rebuild only the group-title attribute; preserve other source metadata.
        if re.search(r'group-title="[^"]*"', info, re.I):
            info = re.sub(r'group-title="[^"]*"', lambda _: 'group-title="' + group.replace('"', "") + '"', info, count=1, flags=re.I)
        else:
            info = info.replace("#EXTINF:", '#EXTINF:-1 group-title="' + group.replace('"', "") + '"', 1)
        lines[0] = info
        result.append({"block": lines, "url": url, "name": name, "group": group, "logo": attrs.get("tvg-logo", "")})
    return result

def category(item):
    name, group, url = item["name"].lower(), item["group"].lower(), item["url"].lower().split("?", 1)[0]
    s = name + " " + group
    if ("/series/" in url or re.search(r"\b(series|serie|temporada|novela|doramas)\b|s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3}", s)):
        return "series"
    if ("/movie/" in url or "/movies/" in url or re.search(r"\b(pelicula|peliculas|movie|movies|vod|film)\b", s)
        or re.search(r"\.(mp4|mkv|avi|mov|m4v|webm)$", url)):
        return "movies"
    if ("/live/" in url or "/channel/" in url or "/channels/" in url or "/tv/" in url
        or re.search(r"\b(canales|channel|channels|live|television|tv)\b", s)):
        return "live"
    return "live" if group else "other"

def release_year(item):
    match = YEAR_RE.search(item["name"])
    return int(match.group(1)) if match else 0

def check_url(url):
    last = "unknown"
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"Range":"bytes=0-0", "User-Agent":UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                code = response.status
                if 200 <= code < 400:
                    return "alive", str(code)
                if code in (401,403):
                    return "unknown", str(code)
                if code in (404,410):
                    last = str(code)
                else:
                    return "unknown", str(code)
        except urllib.error.HTTPError as exc:
            if exc.code in (404,410):
                last = str(exc.code)
            else:
                return "unknown", str(exc.code)
        except Exception as exc:
            last = type(exc).__name__
        if attempt + 1 < RETRIES:
            time.sleep(1)
    return ("dead", last) if last in ("404","410") else ("unknown", last)

def write_m3u(path, items):
    lines = ["#EXTM3U"]
    for item in items:
        lines.extend(item["block"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def main():
    if not ACTIVE.exists():
        print("ERROR: no existe Completa.m3u; no se modifica nada.")
        return 2
    active = parse_m3u(ACTIVE.read_text(encoding="utf-8-sig", errors="replace"))
    if not active:
        print("ERROR: Completa.m3u está vacía o no es válida; se cancela por seguridad.")
        return 3

    # First migration: enforce the 100k ceiling and preserve every overflow item.
    seen = set()
    unique_active = []
    for item in active:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique_active.append(item)
    if len(unique_active) > CAP:
        overflow = unique_active[CAP:]
        unique_active = unique_active[:CAP]
        existing_pending = parse_m3u(PENDING.read_text(encoding="utf-8-sig", errors="replace")) if PENDING.exists() else []
        pending_seen = {x["url"] for x in existing_pending}
        existing_pending.extend(x for x in overflow if x["url"] not in pending_seen)
        write_m3u(PENDING, existing_pending)
        write_m3u(ACTIVE, unique_active)
        print(f"Tope aplicado: {len(unique_active)} activos; {len(overflow)} movidos a la cola pendiente (sin descartarlos).")

    # Read all authorized source lists only to refresh the durable pending queue.
    source_urls = [os.getenv(f"M3U_SOURCE_{i}", "").strip() for i in range(1,4)]
    source_urls = [u for u in source_urls if u]
    if not source_urls:
        print("ERROR: faltan las fuentes autorizadas M3U_SOURCE_1..3.")
        return 4
    pending = parse_m3u(PENDING.read_text(encoding="utf-8-sig", errors="replace")) if PENDING.exists() else []
    active_urls = {x["url"] for x in unique_active}
    known = set(active_urls)
    known.update(x["url"] for x in pending)
    for i, source_url in enumerate(source_urls, 1):
        try:
            req = urllib.request.Request(source_url, headers={"User-Agent":UA})
            with urllib.request.urlopen(req, timeout=40) as response:
                raw = response.read(60 * 1024 * 1024).decode("utf-8-sig", errors="replace")
            found = parse_m3u(raw)
            if not found:
                raise RuntimeError("fuente sin entradas M3U")
            for item in found:
                if item["url"] not in known:
                    pending.append(item)
                    known.add(item["url"])
            print(f"Fuente {i}: {len(found)} entradas leídas; pendientes únicos acumulados: {len(pending)}.")
        except Exception as exc:
            print(f"ERROR: no se pudo leer fuente {i} ({type(exc).__name__}); no se publica una cola incompleta.")
            return 5

    # One explicit workflow_dispatch adds up to 50 verified items per category.
    capacity = max(0, CAP - len(unique_active))
    selected = []
    removed_dead = 0
    checked_total = 0
    remaining = []
    # Preserve source order inside each category; process balanced batches.
    buckets = {"live": [], "movies": [], "series": [], "other": []}
    for item in pending:
        if item["url"] not in active_urls:
            buckets[category(item)].append(item)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for cat in ("live", "movies", "series"):
            if len(selected) >= capacity:
                break
            target = min(PER_CATEGORY, capacity - len(selected))
            queue = buckets[cat]
            batch = queue[:MAX_CHECKS_PER_CATEGORY]
            untouched = queue[len(batch):]
            futures = {pool.submit(check_url, item["url"]): item for item in batch}
            statuses = {}
            for future in concurrent.futures.as_completed(futures):
                statuses[futures[future]["url"]] = future.result()
            checked_total += len(statuses)
            added = 0
            for candidate in batch:
                status, detail = statuses[candidate["url"]]
                if status == "alive" and added < target:
                    selected.append(candidate)
                    added += 1
                elif status == "dead":
                    removed_dead += 1
                else:
                    remaining.append(candidate)
            remaining.extend(untouched)
            print(f"{cat}: {added} añadidos válidos; {len(batch)} comprobados.")
    # Unknown-category entries and candidates not visited remain safely queued.
    remaining.extend(buckets["other"])
    selected_urls = {x["url"] for x in selected}
    remaining.extend(x for cat in ("live","movies","series") for x in buckets[cat] if x["url"] not in selected_urls and x not in remaining)
    final_active = unique_active + selected
    if len(final_active) > CAP:
        raise RuntimeError("protección: el resultado superaría 100,000 entradas")
    # Date descending within VOD category; keep live order stable.
    live = [x for x in final_active if category(x) == "live"]
    movies = sorted([x for x in final_active if category(x) == "movies"], key=release_year, reverse=True)
    series = sorted([x for x in final_active if category(x) == "series"], key=release_year, reverse=True)
    other = [x for x in final_active if category(x) == "other"]
    write_m3u(ACTIVE, live + movies + series + other)
    # Deduplicate pending URLs and persist all not-yet-activated entries.
    final_pending, pending_seen = [], set()
    active_urls = {x["url"] for x in final_active}
    for item in remaining:
        if item["url"] not in active_urls and item["url"] not in pending_seen:
            final_pending.append(item)
            pending_seen.add(item["url"])
    write_m3u(PENDING, final_pending)
    missing_logos = sum(1 for x in selected if not x["logo"])
    REPORT.write_text(
        "REPORTE DE INCORPORACIÓN — CÓDIGO MASTER\n"
        f"Enlaces activos: {len(final_active)} / {CAP}\n"
        f"Canales, películas y series nuevos verificados y añadidos: {len(selected)}\n"
        f"Enlaces comprobados en esta ejecución: {checked_total}\n"
        f"Enlaces 404/410 confirmados y omitidos: {removed_dead}\n"
        f"Entradas conservadas en cola para futuras órdenes: {len(final_pending)}\n"
        f"Elementos nuevos sin tvg-logo en las fuentes: {missing_logos}\n"
        "Los logos existentes se conservan; no se inventan imágenes para entradas sin logo.\n"
        "Orden VOD: años detectables de más reciente a más antiguo; sin año al final.\n",
        encoding="utf-8"
    )
    print(f"OK: {len(final_active)} activos; {len(selected)} añadidos; {len(final_pending)} pendientes.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
