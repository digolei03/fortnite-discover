"""Colocacao no Discover a partir da API oficial da Epic, via uefntoolkit.

POR QUE ESTE COLETOR EXISTE
---------------------------
`history.py` monta a colocacao raspando o fortnite.gg. Isso funciona, mas o
fortnite.gg mostra UMA leitura da Discover - de um perfil, uma regiao, uma
plataforma, uma variante de teste.

A fonte real e a API de Discovery da Epic:

    POST fn-service-discovery-live-public.ogs.live.on.epicgames.com
         /api/v2/discovery/surface/<surface>?appId=Fortnite&stream=<branch>

O corpo dela e um PERFIL DE JOGADOR (`playerId`, `matchmakingRegion`,
`platform`, `locale`, `rating`), e a resposta traz `testVariantName` /
`testName`: a Epic roda testes A/B na Discover e informa em qual variante
aquela resposta caiu.

Consequencia para toda analise deste repositorio: nao existe "o rank".
Existe uma DISTRIBUICAO de ranks ao longo de pelo menos cinco dimensoes.
Qualquer numero de posicao precisa dizer de qual perfil ele veio.

O projeto uefntoolkit (KiKoZl1) ja coleta essa API com device auth e consolida
em `discovery_exposure_rollup_daily`. Em vez de reimplementar a autenticacao
(e submeter mais uma conta Epic ao risco de chamadas automatizadas), este
coletor le o resultado ja consolidado pela edge function `discover-data-api`.

CREDENCIAIS
-----------
As tabelas de exposicao exigem nivel `admin` no discover-data-api, que e
concedido a quem tem papel `admin` OU `editor` em `user_roles` - ou a quem
apresenta a service role key.

NAO USE A SERVICE ROLE KEY AQUI. Ela ignora o RLS de TODAS as tabelas do
projeto Supabase, e o projeto e de terceiro (KiKoZl1/uefntoolkit). Num
repositorio publico isso seria expor a credencial-mestra de outra pessoa.

Use um usuario dedicado com papel `editor`. Como o JWT de usuario expira em
~1h (inutil para cron), o caminho normal e o refresh token:

    UEFN_DATA_API_URL        https://<project-ref>.supabase.co
    UEFN_SUPABASE_ANON_KEY   chave anon (publica por design, ja vai no frontend)
    UEFN_REFRESH_TOKEN       refresh token do usuario editor (longa duracao)

Alternativa para rodar a mao, sem refresh:

    UEFN_DATA_API_TOKEN      access token ja valido (expira em ~1h)

Local: num `.env` na raiz (ignorado pelo .gitignore).
Actions: Settings > Secrets and variables > Actions.

Se o refresh token vazar, o dano e leitura como aquele usuario editor, e a
revogacao e deslogar/rotacionar so ele - nao o projeto inteiro.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
from typing import Any, Dict, List, Optional

import pandas as pd

from .common import DATA, log, now_utc, write_table

FUNCTION_PATH = "/functions/v1/discover-data-api"

# Teto imposto pelo proprio discover-data-api (runSelect capa em 10.000).
PAGE_LIMIT = 10000


class DataApi:
    """Cliente minimo do discover-data-api (somente leitura)."""

    def __init__(self) -> None:
        self.base = (os.environ.get("UEFN_DATA_API_URL") or "").rstrip("/")
        if not self.base:
            raise SystemExit("Falta UEFN_DATA_API_URL (num .env local ou nos secrets).")

        import httpx
        self._httpx = httpx
        self.token = os.environ.get("UEFN_DATA_API_TOKEN") or ""
        if not self.token:
            self.token = self._refresh_access_token()

        self._client = httpx.Client(
            timeout=60.0,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "apikey": self.token,
            },
        )

    def _refresh_access_token(self) -> str:
        """Troca o refresh token por um access token novo.

        JWT de usuario dura ~1h, entao guardar o access token num secret nao
        funciona para execucao agendada. O refresh token e de longa duracao e
        fica limitado ao usuario editor.
        """
        anon = os.environ.get("UEFN_SUPABASE_ANON_KEY") or ""
        refresh = os.environ.get("UEFN_REFRESH_TOKEN") or ""
        if not anon or not refresh:
            raise SystemExit(
                "Faltam credenciais. Defina UEFN_SUPABASE_ANON_KEY + UEFN_REFRESH_TOKEN "
                "(recomendado), ou UEFN_DATA_API_TOKEN para uma execucao manual.\n"
                "NAO use a service role key: ela ignora o RLS do projeto inteiro."
            )
        resp = self._httpx.post(
            self.base + "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            headers={"apikey": anon, "Content-Type": "application/json"},
            json={"refresh_token": refresh},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise SystemExit(
                "Falha ao renovar o token (HTTP %d). O refresh token pode ter sido "
                "revogado ou ja usado - gere um novo logando como o usuario editor."
                % resp.status_code)
        token = (resp.json() or {}).get("access_token")
        if not token:
            raise SystemExit("A renovacao nao devolveu access_token.")
        log.info("access token renovado a partir do refresh token")
        return token

    def select(self, table, columns="*", filters=None, order=None, limit=PAGE_LIMIT):
        payload: Dict[str, Any] = {"table": table, "columns": columns, "limit": limit}
        if filters:
            payload["filters"] = filters
        if order:
            payload["order"] = order

        resp = self._client.post(self.base + FUNCTION_PATH,
                                 json={"op": "select", "payload": payload})
        if resp.status_code in (401, 403):
            raise SystemExit(
                "discover-data-api recusou o token (HTTP %d). As tabelas de exposicao "
                "exigem papel admin/editor - um token anon nao basta." % resp.status_code)
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("data", body) if isinstance(body, dict) else body
        return rows if isinstance(rows, list) else []


def fetch_targets(api: DataApi) -> pd.DataFrame:
    """Dimensao dos perfis coletados: regiao x plataforma x locale x surface.

    E o que da sentido a qualquer posicao: um best_rank sem o target ao lado
    e um numero sem unidade.
    """
    rows = api.select(
        "discovery_exposure_targets",
        columns="id,region,surface_name,platform,locale,interval_minutes,last_status")
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.rename(columns={"id": "target_id"})
        df["fetched_at"] = now_utc().isoformat()
    return df


def fetch_rollup(api: DataApi, since: str, until: str) -> List[Dict[str, Any]]:
    """Rollup diario de exposicao, dia a dia.

    Paginamos por DATA em vez de offset: o discover-data-api nao expoe offset,
    e um dia raramente passa do teto de 10.000 linhas. Se passar, o aviso sai
    no log e basta quebrar tambem por painel.
    """
    out: List[Dict[str, Any]] = []
    day = dt.date.fromisoformat(since)
    last = dt.date.fromisoformat(until)
    while day <= last:
        iso = day.isoformat()
        rows = api.select(
            "discovery_exposure_rollup_daily",
            filters=[{"column": "date", "op": "eq", "value": iso}],
            order=[{"column": "panel_name", "ascending": True}])
        if len(rows) >= PAGE_LIMIT:
            log.warning("%s bateu o teto de %d linhas - pode haver truncamento",
                        iso, PAGE_LIMIT)
        if rows:
            log.info("  %s: %d linhas", iso, len(rows))
            out.extend(rows)
        day += dt.timedelta(days=1)
    return out


def last_collected_date() -> Optional[str]:
    paths = sorted((DATA / "exposure_epic").glob("ym=*/exposure_epic.parquet"))
    if not paths:
        return None
    try:
        return str(pd.read_parquet(paths[-1], columns=["date"])["date"].max())[:10]
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="AAAA-MM-DD. Padrao: dia seguinte ao ultimo coletado, ou 30 dias atras.")
    ap.add_argument("--until", help="AAAA-MM-DD. Padrao: hoje.")
    args = ap.parse_args()

    api = DataApi()

    targets = fetch_targets(api)
    if targets.empty:
        log.warning("nenhum target configurado no uefntoolkit - nada a coletar")
        return
    write_table(targets, "exposure_targets", key="target_id")
    log.info("targets: %s", ", ".join(sorted(
        {"%s/%s/%s" % (t.region, t.platform, t.locale) for t in targets.itertuples()})))

    until = args.until or now_utc().strftime("%Y-%m-%d")
    if args.since:
        since = args.since
    else:
        last = last_collected_date()
        since = ((dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat() if last
                 else (dt.date.today() - dt.timedelta(days=30)).isoformat())
    if since > until:
        log.info("nada novo desde %s", since)
        return

    log.info("rollup de exposicao: %s -> %s", since, until)
    rows = fetch_rollup(api, since, until)
    if not rows:
        log.warning("nenhuma linha no periodo")
        return

    df = pd.DataFrame(rows)
    df["ym"] = df["date"].astype(str).str[:7]
    df["fetched_at"] = now_utc().isoformat()
    # link_code e o codigo da ilha quando link_code_type == 'island': mesma
    # chave de juncao usada pelo resto do repositorio.
    df["island_code"] = df["link_code"].where(df.get("link_code_type") == "island")

    total = 0
    for ym, part in df.groupby("ym"):
        out_dir = DATA / "exposure_epic" / ("ym=" + str(ym))
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "exposure_epic.parquet"
        if path.exists():
            part = pd.concat([pd.read_parquet(path), part], ignore_index=True)
        part = part.drop_duplicates(
            subset=["date", "target_id", "panel_name", "link_code"], keep="last")
        part.to_parquet(path, index=False)
        total += len(part)
    log.info("exposure_epic: %d linhas em %d meses", total, df["ym"].nunique())


if __name__ == "__main__":
    main()
