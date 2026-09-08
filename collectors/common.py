"""Infra compartilhada: sessoes HTTP, retry, escrita Parquet.

Python 3.9+ (sem sintaxe 3.10). fortnite.gg exige impersonacao de TLS por causa
do Cloudflare; a API da Epic aceita cliente comum.
"""
from __future__ import annotations

import gzip
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = ROOT / "config"


def _load_dotenv() -> None:
    """Carrega .env da raiz, sem sobrescrever o que ja veio do ambiente.

    Assim o mesmo codigo funciona local (arquivo .env, git-ignored) e no
    Actions (secrets viram variaveis de ambiente e tem precedencia).
    Implementado a mao para nao adicionar dependencia so por isto.
    """
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

FGG = "https://fortnite.gg"
EPIC = "https://api.fortnite.com/ecosystem/v1"

# Slugs confirmados em fortnite.gg/genres (2026-09-08).
GENRE_SLUGS = [
    "shooter", "battle-royale", "simulation-tycoon", "party-mini-games",
    "roleplaying-social", "deathrun-platformer", "horror", "roguelike",
    "strategy", "adventure-rpg", "survival", "sports-racing", "music-rhythm",
]

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("fdl")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_str() -> str:
    return now_utc().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Fetcher:
    """Cliente com retry/backoff. `impersonate` liga o modo curl_cffi (fortnite.gg)."""

    def __init__(self, impersonate: Optional[str] = None, min_interval: float = 0.2):
        self.min_interval = min_interval
        self._last = 0.0
        if impersonate:
            from curl_cffi import requests as creq
            self._session = creq.Session(impersonate=impersonate)
        else:
            import httpx
            self._session = httpx.Client(
                timeout=30.0,
                headers={"User-Agent": "surprise-discover-lab/1.0"},
                follow_redirects=True,
            )

    def _throttle(self) -> None:
        gap = time.time() - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.time()

    def get(self, url: str, tries: int = 4) -> Optional[Any]:
        """Devolve o objeto de resposta, ou None se todas as tentativas falharem."""
        for attempt in range(tries):
            self._throttle()
            try:
                r = self._session.get(url, timeout=30)
            except Exception as exc:  # rede instavel, DNS, TLS
                log.warning("erro de rede %s (tentativa %d): %s", url, attempt + 1, exc)
                time.sleep(2 ** attempt + random.random())
                continue

            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After") or (2 ** attempt))
                log.warning("%s em %s, aguardando %.0fs", r.status_code, url, wait)
                time.sleep(wait + random.random())
                continue
            if 400 <= r.status_code < 500:
                # 404 e afins nao adianta repetir
                log.debug("%s em %s, desistindo", r.status_code, url)
                return None
            time.sleep(2 ** attempt)
        log.error("falha definitiva em %s", url)
        return None

    def get_json(self, url: str) -> Optional[Any]:
        r = self.get(url)
        if r is None:
            return None
        try:
            return r.json()
        except Exception as exc:
            log.error("JSON invalido em %s: %s", url, exc)
            return None


def fgg_fetcher() -> Fetcher:
    # 0.6s entre requisicoes: ~1.7 req/s no fortnite.gg, educado o suficiente.
    return Fetcher(impersonate="chrome", min_interval=0.6)


def epic_fetcher() -> Fetcher:
    # Rate limit da Epic nao e documentado. 0.25s = ~4 req/s.
    return Fetcher(min_interval=0.25)


# --------------------------------------------------------------------------- #
# Persistencia
# --------------------------------------------------------------------------- #

def write_snapshot(rows: List[Dict[str, Any]], dataset: str, dt: Optional[str] = None) -> Path:
    """Grava um snapshot particionado por data. Deduplica contra o que ja existe."""
    dt = dt or today_str()
    out_dir = DATA / dataset / ("dt=" + dt)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (dataset + ".parquet")

    df = pd.DataFrame(rows)
    if df.empty:
        log.warning("%s: nada para gravar", dataset)
        return path

    if path.exists():
        df = pd.concat([pd.read_parquet(path), df], ignore_index=True)

    keys = [c for c in ("island_code", "genre_slug", "metric_date", "row_name", "window_start")
            if c in df.columns]
    if keys:
        # mantem a coleta mais recente do dia para cada chave
        df = df.sort_values("fetched_at").drop_duplicates(subset=keys, keep="last")

    df.to_parquet(path, index=False)
    log.info("%s: %d linhas -> %s", dataset, len(df), path.relative_to(ROOT))
    return path


def write_table(df: pd.DataFrame, dataset: str, key: str) -> Path:
    """Tabela nao particionada (dimensao). Faz upsert pela chave."""
    out_dir = DATA / dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (dataset + ".parquet")
    if path.exists():
        df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
    df = df.drop_duplicates(subset=[key], keep="last")
    df.to_parquet(path, index=False)
    log.info("%s: %d linhas -> %s", dataset, len(df), path.relative_to(ROOT))
    return path


def read_table(dataset: str) -> pd.DataFrame:
    path = DATA / dataset / (dataset + ".parquet")
    return pd.read_parquet(path) if path.exists() else pd.DataFrame()


def save_raw_html(text: str, name: str, dt: Optional[str] = None) -> None:
    """HTML bruto comprimido.

    O parser vai quebrar quando o fortnite.gg mudar o layout. Como a janela de
    metricas da Epic e de 7 dias sem backfill, um bug silencioso destroi dados
    insubstituiveis. Com o HTML salvo, basta reprocessar.
    """
    out_dir = DATA / "raw_html" / ("dt=" + (dt or today_str()))
    out_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_dir / (name + ".html.gz"), "wt", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- #
# Parsing de numeros do fortnite.gg ("167.9K", "5,242", "20.74%")
# --------------------------------------------------------------------------- #

def parse_num(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = raw.strip().replace(",", "").replace("%", "").replace("$", "")
    if not s or s == "-":
        return None
    mult = 1.0
    if s[-1] in "KkMm":
        mult = 1_000.0 if s[-1] in "Kk" else 1_000_000.0
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def our_islands() -> List[str]:
    """Codigos das ilhas da Surprise, rastreadas mesmo fora do top 50."""
    path = CONFIG / "our_islands.txt"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            out.append(line)
    return out
