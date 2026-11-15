#!/usr/bin/env python3
"""Safely deduplicate and repair Completa.m3u using only approved M3U secrets.

Only HTTP 404/410 are treated as definitive dead-link signals. 401/403,
timeouts, 5xx, and unsupported protocols are retained as uncertain/restricted.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

PLAYLIST = Path("Completa.m3u")
STATE = Path("mantenimiento_estado.json")
REPORT = Path("reporte_mantenimiento.txt")
PAGE_SIZE = 1000
WORKERS = 32
TIMEOUT = 8
RETRIES = 3
MAX_ALTERNATIVES = 5
USER_AGENT = "CodigoMaster-M3U-Maintenance/1.0"
URL_RE = re.compile(r"^(https?|rtmp|rtsp|udp)://", re.I)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def parse_m3u(text: str):
    blocks, current = [], []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line == "#EXTM3U":
            continue
        if line.startswith("#EXTINF:"):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    entries = []
    for block in blocks:
        url = next((line for line in block if URL_RE.match(line)), "")
        if not url:
            continue
        info = block[0]
        name = info.rsplit(",", 1)[-1].strip() if "," in info else info
        group_match = re.search(r'group-title="([^"]*)"', info, re.I)
        id_match = re.search(r'tvg-id="([^"]*)"', info, re.I)
        group = group_match.group(1).strip() if group_match else ""
        tvg_id = id_match.group(1).strip() if id_match else ""
        # Exact URL is the duplicate key. Never collapse different episodes
        # merely because their display names or group titles happen to match.
        match_key = (normalize(name), normalize(group), normalize(tvg_id))
        entries.append({"block": block, "url": url, "name": name,
                        "group": group, "match_key": match_key})
    return entries


def fetch_source(url: str, index: int):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(50 * 1024 * 1024).decode("utf-8-sig", errors="replace")
    parsed = parse_m3u(data)
    if not parsed:
        raise RuntimeError(f"fuente {index} sin entradas M3U válidas")
    print(f"Fuente autorizada {index}: {len(parsed)} entradas.", flush=True)
    return parsed


def check_url(url: str):
    if not url.lower().startswith(("http://", "https://")):
        return "unknown", "protocolo_no_http"

    last = "error_desconocido"
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={"Range": "bytes=0-0", "User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                code = response.status
                if 200 <= code < 400:
                    return "alive", str(code)
                if code in (401, 403):
                    return "restricted", str(code)
                if code in (404, 410, 408, 425, 429) or 500 <= code <= 599:
                    last = str(code)
                else:
                    return "unknown", str(code)
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in (401, 403):
                return "restricted", str(code)
            if code in (404, 410, 408, 425, 429) or 500 <= code <= 599:
                last = str(code)
            else:
                return "unknown", str(code)
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
            # Reintentar fallos transitorios; nunca borrar por timeout o error de red.
            last = type(exc).__name__
        except Exception as exc:
            return "unknown", type(exc).__name__

        if attempt + 1 < RETRIES:
            time.sleep(min(2 ** attempt, 4))

    # Solo 404/410 persistentes en todos los intentos permiten retirar una URL.
    if last in ("404", "410"):
        return "dead", last
    return "unknown", last


def main():
    if not PLAYLIST.exists():
        print("ERROR: no existe Completa.m3u; no se modifica nada.")
        return 2

    original_text = PLAYLIST.read_text(encoding="utf-8-sig", errors="replace")
    original = parse_m3u(original_text)
    if not original:
        print("ERROR: Completa.m3u no contiene entradas válidas; no se modifica.")
        return 3

    source_urls = [os.getenv(f"M3U_SOURCE_{i}", "").strip() for i in range(1, 4)]
    source_urls = [value for value in source_urls if value]
    if not source_urls:
        print("ERROR: no hay fuentes autorizadas configuradas en GitHub Secrets.")
        return 4

    source_entries = []
    for index, url in enumerate(source_urls, 1):
        try:
            source_entries.extend(fetch_source(url, index))
        except Exception as exc:
            print(f"ERROR: no se pudo leer la fuente autorizada {index} ({type(exc).__name__}); se conserva la lista.")
            return 5

    # Index possible replacements by metadata. Keep different episode URLs.
    replacement_index = defaultdict(list)
    for item in source_entries:
        replacement_index[item["match_key"]].append(item["url"])

    # Remove exact duplicate URLs only; preserve the first metadata block.
    unique, seen = [], set()
    duplicate_count = 0
    for item in original:
        if item["url"] in seen:
            duplicate_count += 1
            continue
        seen.add(item["url"])
        unique.append(item)

    if not unique:
        print("ERROR: no quedaron entradas tras deduplicar; se conserva la lista.")
        return 6

    try:
        state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception:
        state = {}
    # Continuar desde la siguiente URL, no desde un índice que cambia cuando
    # se retiran entradas. El índice queda como respaldo para estados antiguos.
    cursor = int(state.get("cursor", 0)) % len(unique)
    next_url = state.get("next_url", "")
    start = next((i for i, item in enumerate(unique) if item["url"] == next_url), cursor)
    batch = unique[start:start + PAGE_SIZE]
    if not batch:
        print("ERROR: lote vacío; no se modifica la lista.")
        return 8
    next_index = (start + len(batch)) % len(unique)
    following_url = unique[next_index]["url"] if next_index != 0 else ""
    checked = {}
    print(f"Lista: {len(original)} entradas; {duplicate_count} URLs duplicadas exactas.", flush=True)
    print(f"Comprobando {len(batch)} enlaces de esta vuelta (posición {start + 1}).", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(check_url, item["url"]): item["url"] for item in batch}
        total = len(futures)
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            checked[futures[future]] = future.result()
            if count % 100 == 0 or count == total:
                print(f"Progreso: {count}/{total}.", flush=True)

    replacement_checks = {}
    repaired, removed, restricted, uncertain = [], [], 0, 0
    replaced_count = 0
    for item in unique:
        status, detail = checked.get(item["url"], ("unchecked", ""))
        if status == "dead":
            replacement = None
            attempts = 0
            for candidate in dict.fromkeys(replacement_index.get(item["match_key"], [])):
                if candidate == item["url"]:
                    continue
                if candidate not in replacement_checks:
                    replacement_checks[candidate] = check_url(candidate)
                candidate_status, _ = replacement_checks[candidate]
                attempts += 1
                if candidate_status == "alive":
                    replacement = candidate
                    break
                if attempts >= MAX_ALTERNATIVES:
                    break
            if replacement:
                new_block = [replacement if line == item["url"] else line for line in item["block"]]
                repaired.append({**item, "block": new_block, "url": replacement})
                replaced_count += 1
            else:
                removed.append(item)
        else:
            if status == "restricted":
                restricted += 1
            elif status in ("unknown", "unchecked"):
                uncertain += 1
            repaired.append(item)

    # Fail-safe: do not publish a dramatically smaller playlist.
    if len(repaired) < max(1, int(len(original) * 0.50)):
        print(f"ERROR: resultado demasiado pequeño ({len(repaired)} vs {len(original)}); no se reemplaza Completa.m3u.")
        return 7

    output = ["#EXTM3U"]
    for item in repaired:
        output.extend(item["block"])
    new_text = "\n".join(output) + "\n"
    if new_text != original_text:
        PLAYLIST.write_text(new_text, encoding="utf-8")

    STATE.write_text(json.dumps({
        "cursor": next_index,
        "next_url": following_url,
        "total_entries": len(unique),
        "batch_size": len(batch),
        "checked_in_batch": len(checked),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    counts = Counter(item["group"] or "SIN GRUPO" for item in repaired)
    report = [
        "REPORTE DE MANTENIMIENTO — CÓDIGO MASTER",
        f"Entradas antes: {len(original)}",
        f"Duplicados exactos eliminados: {duplicate_count}",
        f"Enlaces comprobados en este lote: {len(checked)}",
        f"Reintentos ante fallos transitorios: hasta {RETRIES} intentos por URL",
        f"Enlaces sustituidos con una alternativa válida: {replaced_count}",
        f"Enlaces confirmados 404/410 sin sustituto y retirados: {len(removed)}",
        f"Enlaces restringidos 401/403 conservados: {restricted}",
        f"Enlaces inciertos o pendientes conservados: {uncertain}",
        f"Entradas finales: {len(repaired)}",
        f"Próxima posición de revisión: {next_index + 1 if next_index else 1}",
        "",
        "Entradas finales por grupo:",
    ]
    report.extend(f"- {name}: {count}" for name, count in sorted(counts.items()))
    report += ["", "Elementos retirados sin sustituto (solo nombres; no se publican URLs):"]
    report.extend(f"- {item['name']} [{item['group']}]" for item in removed[:500])
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")

    print(
        f"OK: duplicados eliminados={duplicate_count}; sustituidos="
        f"{replaced_count}; "
        f"retirados={len(removed)}; finales={len(repaired)}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
