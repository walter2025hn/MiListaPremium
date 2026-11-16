#!/usr/bin/env python3
"""Build static JSON pages (50 entries each) for the MasterCode TV app."""
import hashlib, json, re, shutil
from pathlib import Path

SOURCE = Path("Completa.m3u")
OUT = Path("catalog_pages")
PAGE_SIZE = 50  # 50 elementos por solicitud; las categorías se intercalan para llenar cada sección
URL_RE = re.compile(r"^(https?|rtmp|rtsp|udp)://", re.I)
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')

def parse():
    entries, current = [], None
    section = ""
    for raw in SOURCE.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            current = {"extinf": line, "url": ""}
        elif current and URL_RE.match(line):
            current["url"] = line
            info = current["extinf"]
            attrs = dict(ATTR_RE.findall(info.split(",", 1)[0]))
            title = info.rsplit(",", 1)[-1].strip()
            entries.append({
                "name": title or "Sin título",
                "url": line,
                "group": attrs.get("group-title", "") or "Sin categoría",
                "logo": attrs.get("tvg-logo", ""),
                "tvgId": attrs.get("tvg-id", ""),
                "attributes": info[:info.rfind(",")] if "," in info else info,
                "section": section
            })
            current = None
    return entries

def page_digest(chunk):
    digest = hashlib.sha256()
    for entry in chunk:
        for key in ("name", "url", "group", "logo", "tvgId", "attributes", "section"):
            value = entry.get(key, "").encode("utf-8")
            digest.update(len(value).to_bytes(4, "big"))
            digest.update(value)
    return digest.hexdigest()

def main():
    if not SOURCE.exists():
        raise SystemExit("No existe Completa.m3u")
    entries = parse()
    if not entries:
        raise SystemExit("No se encontraron entradas; no se publica catálogo vacío.")
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()

    # No publicar primero miles de canales dejando vacías las demás secciones.
    # Repartir el catálogo en grupos y mezclarlos de forma equilibrada, conservando
    # el orden original dentro de cada grupo. La app puede poblar TV, películas,
    # series, Anime y Kids desde las primeras páginas.
    buckets = {name: [] for name in ("kids", "anime", "live", "movies", "series", "other")}
    for entry in entries:
        name = entry["name"].lower()
        group = entry["group"].lower()
        url = entry["url"].lower().split("?", 1)[0]
        text = f"{name} {group}"
        if re.search(r"kids|infantil|niños|ninos|cartoon|disney junior|nick jr|paw patrol|peppa pig", text):
            bucket = "kids"
        elif re.search(r"anime|manga|shonen|otaku|donghua|naruto|dragon ball|one piece|pokemon|demon slayer", text):
            bucket = "anime"
        elif "/series/" in url or re.search(r"\b(series|serie|temporada|novela|doramas)\b|s\d{1,2}e\d{1,3}|\d{1,2}x\d{1,3}", text):
            bucket = "series"
        elif "/movie/" in url or "/movies/" in url or re.search(r"\b(pelicula|peliculas|movie|movies|vod|films?)\b", text) or re.search(r"\.(mp4|mkv|avi|mov|m4v|webm)$", url):
            bucket = "movies"
        elif "/live/" in url or "/channel/" in url or "/channels/" in url or "/tv/" in url:
            bucket = "live"
        else:
            bucket = "other"
        buckets[bucket].append(entry)

    # Intercalado round-robin: cada página contiene una mezcla de categorías,
    # en lugar de llenar las primeras páginas solo con canales.
    ordered = []
    positions = {key: 0 for key in buckets}
    while True:
        added = False
        for key in buckets:
            pos = positions[key]
            if pos < len(buckets[key]):
                ordered.append(buckets[key][pos])
                positions[key] = pos + 1
                added = True
        if not added:
            break
    entries = ordered

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    page_count = (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE
    page_hashes = []
    for start in range(0, len(entries), PAGE_SIZE):
        page_no = start // PAGE_SIZE + 1
        name = f"page-{page_no:06d}.json"
        chunk = entries[start:start + PAGE_SIZE]
        payload = (json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        (OUT / name).write_bytes(payload)
        page_hashes.append(page_digest(chunk))
    manifest = {
        "schema": 1,
        "source_sha256": digest,
        "total": len(entries),
        "page_size": PAGE_SIZE,
        "page_count": page_count,
        "page_sha256": page_hashes,
        "page_pattern": "page-{page:06d}.json"
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Catálogo paginado: {len(entries)} entradas, {page_count} páginas de hasta {PAGE_SIZE}.")
if __name__ == "__main__":
    main()
