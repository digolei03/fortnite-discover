"""Metricas diarias por ilha, da Epic Ecosystem API (publica, sem autenticacao).

  GET https://api.fortnite.com/ecosystem/v1/islands/{code}/metrics

Devolve oito series com um ponto por dia:
  averageMinutesPerPlayer, peakCCU, favorites, minutesPlayed,
  recommendations, plays, uniquePlayers, retention{d1,d7}

Tres desses campos NAO existem no fortnite.gg e sao a hipotese central do
estudo: `favorites`, `recommendations` e `plays`. A documentacao da Epic diz
que as rows pontuam sinais de engajamento, retencao, SOCIAL e similaridade -
e favorites/recommendations sao os candidatos diretos ao termo social.
Derivados uteis: plays/uniquePlayers (sessoes por jogador) e
favorites/uniquePlayers (taxa de favoritagem).

ATENCAO: a janela retroativa e curta (a Epic documenta 7 dias; nos testes uma
ilha pequena devolveu 2). Nao ha backfill confiavel. Cada dia sem coletar e um
dia perdido para sempre - por isso este coletor roda todo dia, sem excecao.
`from`/`to` em ISO 8601 sao aceitos pela validacao mas devolveram serie vazia
nos testes; por isso chamamos sem parametros e ficamos com a janela padrao.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List

import pandas as pd

from .common import (
    DATA, EPIC, epic_fetcher, log, now_utc, our_islands, read_table,
    today_str, write_snapshot,
)

# nome na API -> nome na nossa tabela
SERIES = {
    "uniquePlayers": "unique_players",
    "plays": "plays",
    "minutesPlayed": "minutes_played",
    "averageMinutesPerPlayer": "avg_minutes_per_player",
    "peakCCU": "peak_ccu",
    "favorites": "favorites",
    "recommendations": "recommendations",
}


def fetch_metrics(fetcher, code: str) -> List[Dict[str, Any]]:
    """Pivota as series da API para uma linha por (ilha, dia)."""
    data = fetcher.get_json(EPIC + "/islands/" + code + "/metrics")
    if not isinstance(data, dict):
        return []

    by_date: Dict[str, Dict[str, Any]] = {}

    def slot(timestamp: str) -> Dict[str, Any]:
        day = (timestamp or "")[:10]
        return by_date.setdefault(day, {"island_code": code, "metric_date": day})

    for api_name, column in SERIES.items():
        for point in data.get(api_name) or []:
            slot(point.get("timestamp"))[column] = point.get("value")

    # retention vem com formato proprio: {"d1": 0.11, "d7": 0, "timestamp": ...}
    for point in data.get("retention") or []:
        rec = slot(point.get("timestamp"))
        rec["retention_d1"] = point.get("d1")
        rec["retention_d7"] = point.get("d7")

    return [r for day, r in sorted(by_date.items()) if day]


def target_codes(scope: str) -> List[str]:
    codes = set(our_islands())
    if scope == "ours":
        return sorted(codes)
    if scope == "all":
        dim = read_table("islands")
        if not dim.empty:
            codes.update(dim["island_code"].dropna())
        return sorted(codes)

    for dataset in ("ranks", "movers"):
        path = DATA / dataset / ("dt=" + today_str()) / (dataset + ".parquet")
        if path.exists():
            codes.update(pd.read_parquet(path, columns=["island_code"])["island_code"].dropna())
    return sorted(c for c in codes if c)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", choices=["today", "all", "ours"], default="today")
    args = ap.parse_args()

    codes = target_codes(args.scope)
    if not codes:
        log.error("nenhum codigo alvo - rode `python -m collectors.ranks` antes")
        return
    log.info("metricas da Epic para %d ilhas (scope=%s)", len(codes), args.scope)

    fetcher = epic_fetcher()
    fetched_at = now_utc().isoformat()
    dt = today_str()
    rows: List[Dict[str, Any]] = []
    missing = 0

    for i, code in enumerate(codes, 1):
        recs = fetch_metrics(fetcher, code)
        if not recs:
            missing += 1
        for rec in recs:
            rec["dt"] = dt
            rec["fetched_at"] = fetched_at
        rows.extend(recs)
        if i % 200 == 0 or i == len(codes):
            log.info("  %d/%d ilhas | %d linhas", i, len(codes), len(rows))

    if missing:
        log.warning("%d ilhas sem metricas (privadas, novas ou fora do Discover)", missing)
    write_snapshot(rows, "metrics", dt)


if __name__ == "__main__":
    main()
