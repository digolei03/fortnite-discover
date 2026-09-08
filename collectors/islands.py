"""Dimensao de ilhas: resolve `island_code` -> `fgg_id` e metadados da Epic.

O `fgg_id` (numerico, interno do fortnite.gg) e obrigatorio para os endpoints
de historico usados por history.py. Ele so aparece como `data-id` no HTML da
pagina da ilha, entao custa uma requisicao por ilha nova.

Metadados quase nao mudam, entao so resolvemos codigos ainda ausentes da
dimensao. Rodar isso todo dia custa poucas requisicoes depois do primeiro dia.
"""
from __future__ import annotations

import glob
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from .common import (
    DATA, EPIC, FGG, epic_fetcher, fgg_fetcher, log, now_utc, our_islands,
    read_table, write_table,
)


def known_codes() -> List[str]:
    """Todos os codigos ja vistos em ranks/movers, mais as ilhas da Surprise."""
    codes = set(our_islands())
    for dataset in ("ranks", "movers"):
        for path in glob.glob(str(DATA / dataset / "dt=*" / (dataset + ".parquet"))):
            try:
                codes.update(pd.read_parquet(path, columns=["island_code"])["island_code"].dropna())
            except Exception as exc:
                log.warning("nao consegui ler %s: %s", path, exc)
    return sorted(c for c in codes if c)


def resolve_fgg_id(fetcher, code: str) -> Optional[int]:
    r = fetcher.get(FGG + "/island/" + code)
    if r is None:
        return None
    m = re.search(r"data-id=['\"](\d+)['\"]", r.text)
    return int(m.group(1)) if m else None


def epic_metadata(fetcher, code: str) -> Dict[str, Any]:
    data = fetcher.get_json(EPIC + "/islands/" + code)
    if not isinstance(data, dict):
        return {}
    return {
        "title_epic": data.get("title"),
        "creator_code_epic": data.get("creatorCode"),
        "created_in": data.get("createdIn"),
        "tags": ",".join(data.get("tags") or []),
    }


def main() -> None:
    dim = read_table("islands")
    have = set(dim["island_code"]) if not dim.empty else set()
    # Reprocessa quem ficou sem fgg_id (falha de rede numa rodada anterior).
    if not dim.empty and "fgg_id" in dim.columns:
        have -= set(dim.loc[dim["fgg_id"].isna(), "island_code"])

    todo = [c for c in known_codes() if c not in have]
    log.info("dimensao: %d conhecidas, %d a resolver", len(have), len(todo))
    if not todo:
        return

    fgg, epic = fgg_fetcher(), epic_fetcher()
    rows: List[Dict[str, Any]] = []
    for i, code in enumerate(todo, 1):
        rec: Dict[str, Any] = {
            "island_code": code,
            "fgg_id": resolve_fgg_id(fgg, code),
            "resolved_at": now_utc().isoformat(),
        }
        rec.update(epic_metadata(epic, code))
        rows.append(rec)
        if i % 50 == 0 or i == len(todo):
            log.info("  resolvidas %d/%d", i, len(todo))

    df = pd.DataFrame(rows)
    missing = int(df["fgg_id"].isna().sum())
    if missing:
        log.warning("%d ilhas sem fgg_id (sem historico ate resolver)", missing)
    write_table(df, "islands", key="island_code")


if __name__ == "__main__":
    main()
