#!/usr/bin/env python3
"""Merge authorized M3U sources into Completa.m3u without exposing source URLs."""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

OUTPUT = Path("Completa.m3u")
REPORT = Path("reporte_enlaces.txt")
STATE = Path("verificacion_estado.json")
TIMEOUT = 5
RETRIES = 2
WORKERS = 64
BATCH_SIZE = 3000

def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

def parse_m3u(text: str):
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    entries, block = [], []
    for line in lines:
        if not line or line == "#EXTM3U" or line.startswith("----"):
            continue
        if line.startswith("#EXTINF:"):
            if block:
                entries.append(block)
            block = [line]
        elif block:
            block.append(line)
    if block:
        entries.append(block)
    parsed = []
    for block in entries:
        url = next((x for x in block if re.match(r"^(https?|rtmp|rtsp|udp)://", x, re.I)), None)
        if not url:
            continue
        info = block[0]
        name = info.rsplit(",", 1)[-1].strip() if "," in info else info
        group_match = re.search(r'group-title="([^"]*)"', info, re.I)
        group = group_match.group(1).strip() if group_match else ""
        key = (normalize(name), normalize(group))
        parsed.append({"block": block, "url": url, "name": name, "group": group, "key": key})
    return parsed

def check_url(url: str):
    # Range GET is more compatible with IPTV servers than HEAD.
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                url, headers={"Range": "bytes=0-0", "User-Agent": "CodigoMasterPlaylist/1.0"}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                code = response.status
                if 200 <= code < 400:
                    return True, str(code)
                if code not in (401, 403, 404, 410) and code < 500:
                    return True, str(code)
                last = str(code)
        except urllib.error.HTTPError as exc:
            last = str(exc.code)
            if exc.code not in (401, 403, 404, 410) and exc.code < 500:
                return True, last
        except Exception as exc:
            last = type(exc).__name__
        if attempt + 1 < RETRIES:
            time.sleep(1)
    return False, last

def main():
    source_urls = [os.getenv(f"M3U_SOURCE_{i}", "").strip() for i in range(1, 4)]
    source_urls = [u for u in source_urls if u]
    if not source_urls:
        print("ERROR: configura M3U_SOURCE_1, M3U_SOURCE_2 y M3U_SOURCE_3 en GitHub Secrets.")
        return 2

    all_entries = []
    for index, url in enumerate(source_urls, 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CodigoMasterPlaylist/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read(30 * 1024 * 1024).decode("utf-8-sig", errors="replace")
            entries = parse_m3u(content)
            if not entries:
                print(f"ERROR: la fuente {index} no contiene entradas M3U válidas; se cancela para proteger la lista.")
                return 3
            print(f"Fuente {index}: {len(entries)} entradas.")
            all_entries.extend(entries)
        except Exception as exc:
            print(f"ERROR: no se pudo descargar la fuente {index} ({type(exc).__name__}); se cancela para proteger la lista.")
            return 4

    if len(all_entries) < 1:
        print("ERROR: no hay entradas; no se modificará la lista.")
        return 5

    # Check only a rotating batch each run; keep every source entry in the output.
    urls = list(dict.fromkeys(item["url"] for item in all_entries))
    try:
        state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception:
        state = {}
    cursor = int(state.get("cursor", 0)) % len(urls)
    batch = [urls[(cursor + offset) % len(urls)] for offset in range(min(BATCH_SIZE, len(urls)))]
    next_cursor = (cursor + len(batch)) % len(urls)
    checks = {}
    print(f"Enlaces únicos: {len(urls)}. Lote de esta ejecución: {len(batch)} (posición {cursor + 1}).", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(check_url, url): url for url in batch}
        total = len(futures)
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            checks[futures[future]] = future.result()
            completed += 1
            if completed % 100 == 0 or completed == total:
                print(f"Progreso del lote: {completed}/{total} ({completed * 100 // total}%).", flush=True)

    STATE.write_text(json.dumps({
        "cursor": next_cursor,
        "total_urls": len(urls),
        "batch_size": len(batch)
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Prefer a verified reachable duplicate. If none was checked successfully,
    # keep an unchecked copy where possible; never drop an unverified entry.
    groups = {}
    for item in all_entries:
        groups.setdefault(item["key"], []).append(item)
    selected, dead_names = [], []
    for key, copies in groups.items():
        reachable = [x for x in copies if checks.get(x["url"], (False, ""))[0]]
        unchecked = [x for x in copies if x["url"] not in checks]
        chosen = reachable[0] if reachable else (unchecked[0] if unchecked else copies[0])
        selected.append(chosen)
        if not reachable and not unchecked:
            dead_names.append(chosen["name"])

    # Dict insertion order preserves the first-seen order of each title/group.
    # Avoid all_entries.index() here: its repeated linear scans become O(n^2).
    output_lines = ["#EXTM3U"]
    for item in selected:
        output_lines.extend(item["block"])
    new_content = "\n".join(output_lines) + "\n"

    # Never overwrite a healthy existing playlist with a suspiciously small result.
    if OUTPUT.exists():
        old_count = len(parse_m3u(OUTPUT.read_text(encoding="utf-8", errors="replace")))
        if old_count and len(selected) < max(1, int(old_count * 0.35)):
            print(f"ERROR: resultado sospechosamente pequeño ({len(selected)} frente a {old_count}); no se reemplaza Completa.m3u.")
            return 6

    OUTPUT.write_text(new_content, encoding="utf-8")
    counts = Counter((x["group"] or "SIN GRUPO") for x in selected)
    report = [
        "REPORTE AUTOMÁTICO — CÓDIGO MASTER",
        f"Entradas descargadas: {len(all_entries)}",
        f"Entradas finales (sin duplicados): {len(selected)}",
        f"URLs únicas totales: {len(urls)}",
        f"URLs comprobadas en este lote: {len(checks)}",
        f"URLs pendientes de comprobación en esta vuelta: {len(urls) - len(checks)}",
        f"Elementos confirmados sin respuesta en sus copias: {len(dead_names)}",
        f"Siguiente posición de revisión: {next_cursor + 1}",
        "",
        "Entradas por grupo:",
    ]
    report.extend(f"- {name}: {count}" for name, count in sorted(counts.items()))
    report += ["", "Posibles enlaces sin respuesta en copias ya comprobadas (no se eliminan):"]
    report.extend(f"- {name}" for name in dead_names[:500])
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"OK: Completa.m3u generada con {len(selected)} entradas. Enlaces sin respuesta: {len(dead_names)}.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
