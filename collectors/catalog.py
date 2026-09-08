"""Catalogo completo de ilhas publicas do Fortnite.

    GET https://api.fortnite.com/ecosystem/v1/islands?size=100&after=<cursor>

Publico, sem autenticacao, paginado por cursor. Medido em ~400 ilhas/s, entao
o catalogo inteiro sai em poucos minutos - e a base do universo sobre o qual
todo o resto e priorizado.

O que vem: code, creatorCode, title, category, createdIn, tags[].
`category` NAO e o genero - e a categoria de IP licenciado (Star Wars, LEGO,
Squid Game) e vem nula na grande maioria. Genero so aparece via Discover.

Este coletor existe para responder "quais ilhas existem". Quem responde "onde
elas aparecem" e discover_surface.py; quem responde "como elas performam" e
metrics.py - e este ultimo custa 4 req/s por ilha, o que torna impossivel
cobrir o catalogo inteiro diariamente. Dai a priorizacao em camadas descrita
no README.
"""
from __future__ import annotations

import argparse
import time
from typing import Any, Dict, List

import pandas as pd

from .common import EPIC, epic_fetcher, log, now_utc, today_str, write_snapshot

PAGE_SIZE = 100


def walk(fetcher, max_pages: int = 0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cursor = None
    pages = 0
    started = time.time()

    while True:
        url = "%s/islands?size=%d" % (EPIC, PAGE_SIZE)
        if cursor:
            url += "&after=" + cursor
        payload = fetcher.get_json(url)
        if not isinstance(payload, dict):
            log.error("pagina %d falhou - parando com o que ja temos", pages + 1)
            break

        for item in payload.get("data") or []:
            code = item.get("code")
            if not code:
                continue
            rows.append({
                "island_code": code,
                "title_epic": item.get("title"),
                "creator_code_epic": item.get("creatorCode"),
                "category": item.get("category"),
                "created_in": item.get("createdIn"),
                "tags": ",".join(item.get("tags") or []),
            })

        pages += 1
        cursor = ((payload.get("meta") or {}).get("page") or {}).get("nextCursor")
        if not cursor or (max_pages and pages >= max_pages):
            break
        if pages % 100 == 0:
            rate = len(rows) / max(time.time() - started, 1e-9)
            log.info("  %d paginas, %d ilhas (%.0f ilhas/s)", pages, len(rows), rate)

    log.info("catalogo: %d ilhas em %d paginas (%.0fs)",
             len(rows), pages, time.time() - started)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-pages", type=int, default=0, help="0 = catalogo inteiro")
    args = ap.parse_args()

    rows = walk(epic_fetcher(), args.max_pages)
    if not rows:
        return
    fetched_at = now_utc().isoformat()
    for row in rows:
        row["fetched_at"] = fetched_at
    write_snapshot(rows, "catalog", today_str())

    df = pd.DataFrame(rows)
    log.info("criadores distintos: %d | com tags: %d",
             df["creator_code_epic"].nunique(), int(df["tags"].astype(bool).sum()))


if __name__ == "__main__":
    main()
