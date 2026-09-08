"""Historico de colocacao no Discover e de jogadores, por ilha.

Estes dois endpoints do fortnite.gg devolvem HISTORICO COMPLETO (~5 meses),
nao apenas o dia corrente. Sao a razao de o estudo nao precisar esperar semanas
acumulando serie:

  /player-count-discovery?range=0&id=<fgg_id>&type=
      -> [[nome_da_row, inicio_ts, fim_ts, posicao], ...]
      Resolucao horaria. Diz em QUAL row do Discover a ilha apareceu, QUANDO e
      em QUE posicao. Rows observadas: Horror, Top Rated, Popular, Sponsored,
      First Person Islands, New. Nao tem o teto de #50 da pagina de genero.

  /player-count-graph?range=all&id=<fgg_id>
      -> {"start": ts, "step": 86400, "values": [...]}
      CCU diario desde o inicio do rastreamento. `range=1d` devolve passo de
      10 minutos para as ultimas 24h; qualquer outro valor devolve tudo.

Como os endpoints reenviam a serie inteira a cada chamada, a gravacao e por
upsert e particionada por mes, para que o commit diario toque so o arquivo do
mes corrente.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
from typing import Any, Dict, List

import pandas as pd

from .common import DATA, FGG, fgg_fetcher, log, now_utc, our_islands, read_table, today_str

DISCOVERY_URL = FGG + "/player-count-discovery?range=0&id={id}&type="
PLAYERS_URL = FGG + "/player-count-graph?range=all&id={id}"


def _ts(value: int) -> str:
    return dt.datetime.utcfromtimestamp(value).replace(tzinfo=dt.timezone.utc).isoformat()


def fetch_placements(fetcher, code: str, fgg_id: int) -> List[Dict[str, Any]]:
    data = fetcher.get_json(DISCOVERY_URL.format(id=fgg_id))
    if not isinstance(data, dict):
        return []
    out: List[Dict[str, Any]] = []
    for item in data.get("discovery") or []:
        if not isinstance(item, list) or len(item) < 4:
            continue
        row_name, start, end, position = item[0], item[1], item[2], item[3]
        out.append({
            "island_code": code,
            "row_name": row_name,
            "window_start": _ts(start),
            "window_end": _ts(end),
            "position": position,
            "hours": round((end - start) / 3600, 2),
            "ym": dt.datetime.utcfromtimestamp(start).strftime("%Y-%m"),
        })
    return out


def fetch_players(fetcher, code: str, fgg_id: int) -> List[Dict[str, Any]]:
    data = fetcher.get_json(PLAYERS_URL.format(id=fgg_id))
    if not isinstance(data, dict):
        return []
    payload = data.get("data") or {}
    start, step, values = payload.get("start"), payload.get("step"), payload.get("values")
    if not (start and step and isinstance(values, list)):
        return []
    out: List[Dict[str, Any]] = []
    for i, value in enumerate(values):
        if value is None:
            continue
        moment = dt.datetime.utcfromtimestamp(start + i * step)
        out.append({
            "island_code": code,
            "date": moment.strftime("%Y-%m-%d"),
            "ccu": value,
            "step_seconds": step,
            "ym": moment.strftime("%Y-%m"),
        })
    return out


def _upsert_monthly(rows: List[Dict[str, Any]], dataset: str, keys: List[str]) -> None:
    """Grava particionado por mes (`ym`), fazendo upsert dentro de cada particao."""
    if not rows:
        log.warning("%s: nada para gravar", dataset)
        return
    df = pd.DataFrame(rows)
    total = 0
    for ym, part in df.groupby("ym"):
        out_dir = DATA / dataset / ("ym=" + str(ym))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / (dataset + ".parquet")
        if path.exists():
            part = pd.concat([pd.read_parquet(path), part], ignore_index=True)
        part = part.drop_duplicates(subset=keys, keep="last")
        part.to_parquet(path, index=False)
        total += len(part)
    log.info("%s: %d linhas em %d meses", dataset, total, df["ym"].nunique())


def target_islands(scope: str) -> pd.DataFrame:
    dim = read_table("islands")
    if dim.empty or "fgg_id" not in dim.columns:
        log.error("dimensao vazia - rode `python -m collectors.islands` antes")
        return pd.DataFrame()
    dim = dim.dropna(subset=["fgg_id"])

    if scope == "all":
        return dim
    if scope == "ours":
        return dim[dim["island_code"].isin(our_islands())]

    # padrao: ilhas ranqueadas hoje + as nossas
    codes = set(our_islands())
    for dataset in ("ranks", "movers"):
        path = DATA / dataset / ("dt=" + today_str()) / (dataset + ".parquet")
        if path.exists():
            codes.update(pd.read_parquet(path, columns=["island_code"])["island_code"].dropna())
    return dim[dim["island_code"].isin(codes)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", choices=["today", "all", "ours"], default="today",
                    help="today: ranqueadas hoje + nossas (padrao) | all: tudo | ours: so as nossas")
    args = ap.parse_args()

    targets = target_islands(args.scope)
    if targets.empty:
        return
    log.info("historico para %d ilhas (scope=%s)", len(targets), args.scope)

    fetcher = fgg_fetcher()
    placements: List[Dict[str, Any]] = []
    players: List[Dict[str, Any]] = []

    for i, rec in enumerate(targets.itertuples(index=False), 1):
        code, fgg_id = rec.island_code, int(rec.fgg_id)
        placements.extend(fetch_placements(fetcher, code, fgg_id))
        players.extend(fetch_players(fetcher, code, fgg_id))
        if i % 100 == 0 or i == len(targets):
            log.info("  %d/%d ilhas | %d colocacoes, %d pontos de ccu",
                     i, len(targets), len(placements), len(players))

    _upsert_monthly(placements, "placements", ["island_code", "row_name", "window_start"])
    _upsert_monthly(players, "players_daily", ["island_code", "date"])


if __name__ == "__main__":
    main()
