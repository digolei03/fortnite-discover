"""Roda as analises sobre os Parquet coletados.

    python -m analysis.run                # todas
    python -m analysis.run --only q1      # uma so
    python -m analysis.run --island 1234-5678-9012   # ficha de uma ilha

Q2 (o que move a colocacao) nao sai em SQL puro: precisa de regressao com efeito
fixo por ilha, feita aqui com statsmodels.
"""
from __future__ import annotations

import argparse
import glob
from pathlib import Path
from typing import List

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SQL = Path(__file__).resolve().parent

# Menor e melhor em `position`, entao o sinal esperado dos coeficientes se inverte:
# usamos improve_position (positivo = subiu) como variavel dependente.
DRIVERS = [
    "d_ccu", "d_unique_players", "d_favorites",
    "sessions_per_player", "favorite_rate", "recommend_rate",
    "retention_d1", "retention_d7",
]


# dataset -> (glob relativo a data/, schema de fallback quando nao ha arquivo).
# O fallback evita que a analise inteira estoure so porque um coletor ainda nao
# rodou (dia 1) ou falhou numa execucao.
SOURCES = {
    "islands": ("islands/islands.parquet",
                "island_code VARCHAR, fgg_id BIGINT, title_epic VARCHAR, "
                "creator_code_epic VARCHAR, created_in VARCHAR, tags VARCHAR, "
                "resolved_at VARCHAR"),
    "placements_raw": ("placements/ym=*/placements.parquet",
                       "island_code VARCHAR, row_name VARCHAR, window_start TIMESTAMP, "
                       "window_end TIMESTAMP, position BIGINT, hours DOUBLE, ym VARCHAR"),
    "players_daily": ("players_daily/ym=*/players_daily.parquet",
                      "island_code VARCHAR, date VARCHAR, ccu BIGINT, "
                      "step_seconds BIGINT, ym VARCHAR"),
    "ranks": ("ranks/dt=*/ranks.parquet",
              "dt VARCHAR, fetched_at VARCHAR, genre_slug VARCHAR, rank BIGINT, "
              "rank_delta_gg BIGINT, island_code VARCHAR, title VARCHAR, "
              "creator_code VARCHAR, players_now DOUBLE, all_time_peak DOUBLE, "
              "total_ranked_maps DOUBLE, player_share DOUBLE, players_24h DOUBLE, "
              "avg_playtime_24h DOUBLE, retention_d1_gg DOUBLE, retention_d7_gg DOUBLE"),
    "metrics": ("metrics/dt=*/metrics.parquet",
                "island_code VARCHAR, metric_date VARCHAR, unique_players BIGINT, "
                "plays BIGINT, minutes_played BIGINT, avg_minutes_per_player DOUBLE, "
                "peak_ccu BIGINT, favorites BIGINT, recommendations BIGINT, "
                "retention_d1 DOUBLE, retention_d7 DOUBLE, dt VARCHAR, fetched_at VARCHAR"),
    "genres": ("genres/dt=*/genres.parquet",
               "dt VARCHAR, fetched_at VARCHAR, genre_slug VARCHAR, genre_rank BIGINT, "
               "players DOUBLE, maps DOUBLE, avg_playtime DOUBLE, "
               "avg_dollar_per_player DOUBLE"),
}


def split_statements(sql: str) -> List[str]:
    """Divide SQL em statements respeitando comentarios e strings.

    Um `split(';')` ingenuo quebra aqui: os comentarios explicativos do
    build.sql contem ponto-e-virgula ("...DENTRO da row; menor e melhor").
    """
    out, buf = [], []
    i, n = 0, len(sql)
    in_line_comment = in_block_comment = in_string = False
    while i < n:
        ch, nxt = sql[i], sql[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                buf.append(ch); i += 1; ch = nxt
        elif in_string:
            if ch == "'":
                if nxt == "'":       # aspa escapada: 'Epic''s Picks'
                    buf.append(ch); i += 1; ch = nxt
                else:
                    in_string = False
        elif ch == "-" and nxt == "-":
            in_line_comment = True
        elif ch == "/" and nxt == "*":
            in_block_comment = True
        elif ch == "'":
            in_string = True
        elif ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))

    def has_code(statement: str) -> bool:
        return any(line.strip() and not line.strip().startswith("--")
                   for line in statement.splitlines())

    return [s.strip() for s in out if has_code(s)]


def connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()

    for name, (pattern, schema) in SOURCES.items():
        path = str(ROOT / "data" / pattern)
        if glob.glob(path):
            con.execute(f"CREATE OR REPLACE VIEW {name} AS "
                        f"SELECT * FROM read_parquet('{path}', union_by_name=true)")
        else:
            print(f"  aviso: dataset '{name}' ainda nao existe - view vazia")
            cols = ", ".join(f"NULL::{t.split(' ', 1)[1]} AS {t.split(' ', 1)[0]}"
                             for t in (c.strip() for c in schema.split(",")))
            con.execute(f"CREATE OR REPLACE VIEW {name} AS "
                        f"SELECT {cols} WHERE FALSE")

    for statement in split_statements((SQL / "build.sql").read_text(encoding="utf-8")):
        con.execute(statement)
    return con


def show(title: str, df: pd.DataFrame, limit: int = 25) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    if df.empty:
        print("  (sem linhas - serie ainda curta ou filtro sem correspondencia)")
        return
    with pd.option_context("display.width", 200, "display.max_columns", 40):
        print(df.head(limit).to_string(index=False))
    if len(df) > limit:
        print(f"  ... e mais {len(df) - limit} linhas")


def q2_drivers(con: duckdb.DuckDBPyConnection) -> None:
    """Regressao de improve_position contra os deltas, com efeito fixo por ilha.

    O efeito fixo faz cada ilha ser seu proprio controle: elimina o confundidor
    "essa ilha e grande" e a restricao de amplitude que contamina qualquer
    correlacao feita num top-N ja selecionado.
    """
    try:
        import statsmodels.formula.api as smf
    except ImportError:
        print("\n[q2] statsmodels nao instalado - pulando")
        return

    df = con.execute("""
        SELECT island_code, main_row, improve_position, """ + ", ".join(DRIVERS) + """
        FROM panel
        WHERE improve_position IS NOT NULL
          AND main_row IS NOT NULL
          AND on_editorial = FALSE
    """).df()

    print("\n" + "=" * 78)
    print("Q2 - o que move a colocacao (efeito fixo por ilha, por row)")
    print("=" * 78)

    for row_name, part in df.groupby("main_row"):
        cols = [c for c in DRIVERS if part[c].notna().sum() > 30 and part[c].std() > 0]
        part = part.dropna(subset=cols + ["improve_position"])
        if len(part) < 100 or not cols:
            continue
        try:
            model = smf.ols(
                "improve_position ~ " + " + ".join(cols) + " + C(island_code)",
                data=part,
            ).fit(cov_type="cluster", cov_kwds={"groups": part["island_code"]})
        except Exception as exc:
            print(f"  {row_name}: regressao falhou ({exc})")
            continue

        print(f"\n  {row_name}  (n={len(part)}, ilhas={part.island_code.nunique()}, "
              f"R2 intra={model.rsquared:.3f})")
        for name in cols:
            coef, pval = model.params.get(name), model.pvalues.get(name)
            if coef is None:
                continue
            star = "***" if pval < 0.01 else "**" if pval < 0.05 else "*" if pval < 0.1 else ""
            print(f"    {name:24} {coef:+12.5f}  p={pval:.3f} {star}")
        print("    (coeficiente positivo = a variavel empurra a ilha PARA CIMA)")


def island_report(con: duckdb.DuckDBPyConnection, code: str) -> None:
    show(f"Colocacoes recentes - {code}", con.execute("""
        SELECT date, main_row, position, best_position_any_row,
               ROUND(exposure_hours,1) AS horas, rows_present, ccu,
               unique_players, favorites, retention_d1, retention_d7
        FROM panel WHERE island_code = ? ORDER BY date DESC LIMIT 30
    """, [code]).df(), limit=30)

    show(f"Todas as rows em que ja apareceu - {code}", con.execute("""
        SELECT row_name, COUNT(*) AS janelas, MIN(position) AS melhor,
               MAX(position) AS pior, ROUND(SUM(hours),1) AS horas_totais,
               MIN(CAST(window_start AS DATE)) AS desde,
               MAX(CAST(window_start AS DATE)) AS ate
        FROM placements_raw WHERE island_code = ?
        GROUP BY 1 ORDER BY horas_totais DESC
    """, [code]).df())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=["q1", "q2", "q3", "cobertura"])
    ap.add_argument("--island", help="codigo da ilha para a ficha detalhada")
    args = ap.parse_args()

    con = connect()

    if args.island:
        island_report(con, args.island)
        return

    if args.only in (None, "cobertura"):
        show("Cobertura dos dados", con.execute("""
            SELECT 'placements' AS dataset, COUNT(*) AS linhas,
                   COUNT(DISTINCT island_code) AS ilhas,
                   MIN(CAST(window_start AS DATE)) AS de,
                   MAX(CAST(window_start AS DATE)) AS ate FROM placements_raw
            UNION ALL SELECT 'players_daily', COUNT(*), COUNT(DISTINCT island_code),
                   MIN(CAST(date AS DATE)), MAX(CAST(date AS DATE)) FROM players_daily
            UNION ALL SELECT 'metrics', COUNT(*), COUNT(DISTINCT island_code),
                   MIN(CAST(metric_date AS DATE)), MAX(CAST(metric_date AS DATE)) FROM metrics
            UNION ALL SELECT 'ranks', COUNT(*), COUNT(DISTINCT island_code),
                   MIN(CAST(dt AS DATE)), MAX(CAST(dt AS DATE)) FROM ranks
        """).df())

        show("Rows do Discover observadas", con.execute("""
            SELECT row_name, COUNT(*) AS janelas,
                   COUNT(DISTINCT island_code) AS ilhas,
                   MIN(position) AS melhor_pos, MAX(position) AS pior_pos
            FROM placements_raw GROUP BY 1 ORDER BY janelas DESC
        """).df())

    if args.only in (None, "q1"):
        show("Q1 - direcao da causalidade (colocacao vs trafego)",
             con.execute((SQL / "q1_causality.sql").read_text(encoding="utf-8")).df())

    if args.only in (None, "q2"):
        q2_drivers(con)

    if args.only in (None, "q3"):
        show("Q3 - destaques: quanto duram",
             con.execute((SQL / "q3_spikes.sql").read_text(encoding="utf-8")).df(), limit=30)


if __name__ == "__main__":
    main()
