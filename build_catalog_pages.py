#!/usr/bin/env python3
"""Build static JSON pages (50 entries each) for the MasterCode TV app."""
import hashlib, json, re, shutil
from pathlib import Path

SOURCE = Path("Completa.m3u")
OUT = Path("catalog_pages")
PAGE_SIZE = 50
URL_RE = re.compile(r"^(https?|rtmp|rtsp|udp)://", re.I)
ATTR_RE = re.compile(r'([\\w-]+)="([^"]*)"')

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

def main():
    if not SOURCE.exists():
        raise SystemExit("No existe Completa.m3u")
    entries = parse()
    if not entries:
        raise SystemExit("No se encontraron entradas; no se publica catálogo vacío.")
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    page_count = (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE
    for start in range(0, len(entries), PAGE_SIZE):
        page_no = start // PAGE_SIZE + 1
        name = f"page-{page_no:06d}.json"
        chunk = entries[start:start + PAGE_SIZE]
        (OUT / name).write_text(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + "\\n", encoding="utf-8")
    manifest = {
        "schema": 1,
        "source_sha256": digest,
        "total": len(entries),
        "page_size": PAGE_SIZE,
        "page_count": page_count,
        "page_pattern": "page-{page:06d}.json"
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\\n", encoding="utf-8")
    print(f"Catálogo paginado: {len(entries)} entradas, {page_count} páginas de hasta {PAGE_SIZE}.")
if __name__ == "__main__":
    main()
