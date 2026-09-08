"""Scrape das paginas de genero do fortnite.gg.

Coleta tres coisas por gênero:
  - top 50 ranqueado, com metricas visiveis     -> dataset `ranks`
  - movers (climbers/fallers/novos ate ~#300)   -> dataset `movers`
  - agregados por genero (players, mapas, $/player) -> dataset `genres`

Limitacao conhecida: a pagina de genero nao pagina. Sao 50 linhas fixas.
Posicoes abaixo de #50 so aparecem via `movers`, ou via o historico horario
coletado por history.py (que nao tem esse teto).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from selectolax.parser import HTMLParser

from .common import (
    FGG, GENRE_SLUGS, fgg_fetcher, log, now_utc, parse_num, save_raw_html,
    today_str, write_snapshot,
)

PARSE_ERRORS: List[Dict[str, Any]] = []


def _rank_and_delta(row) -> Tuple[Optional[int], Optional[int]]:
    """Extrai rank e variacao.

    O markup e `<div class='genre-rank-wrap'>#7<div class='genre-rank-delta down'>1</div></div>`.
    Pegar o texto do no inteiro concatena os dois ("#71"), entao o delta e
    removido antes. Sinal: `up` positivo (subiu), `down` negativo.
    """
    wrap = row.css_first("div.genre-rank-wrap")
    if wrap is None:
        return None, None

    delta = None
    dnode = wrap.css_first("div.genre-rank-delta")
    if dnode is not None:
        val = parse_num(dnode.text(strip=True))
        if val is not None:
            classes = dnode.attributes.get("class") or ""
            delta = int(val) if "up" in classes else -int(val)
        dnode.decompose()  # tira o delta antes de ler o rank

    m = re.search(r"#\s*([\d,]+)", wrap.text(strip=True))
    rank = int(m.group(1).replace(",", "")) if m else None
    return rank, delta


def _stats(row) -> Dict[str, Optional[float]]:
    """Le os blocos `.table-stat`, cujo texto vem como 'Rotulo' + 'Valor' colados."""
    out: Dict[str, Optional[float]] = {}
    labels = {
        "Player Share": "player_share",
        "24h Players": "players_24h",
        "24h Avg Playtime": "avg_playtime_24h",
        "24h Retention": "retention_d1_gg",
        "D7 Retention": "retention_d7_gg",
    }
    for stat in row.css("div.table-stat"):
        text = stat.text(strip=True)
        for label, field in labels.items():
            if text.startswith(label):
                out[field] = parse_num(text[len(label):])
                break
    return out


def _code(row) -> Optional[str]:
    href = row.attributes.get("href") or ""
    m = re.search(r"/island/([\d\-]+)", href)
    return m.group(1) if m else None


def parse_genre_page(html: str, slug: str, fetched_at: str, dt: str):
    tree = HTMLParser(html)
    ranks: List[Dict[str, Any]] = []
    movers: List[Dict[str, Any]] = []

    meta = tree.css_first("div.genre-meta")
    total_ranked = parse_num(meta.text(strip=True).split()[0]) if meta else None

    for row in tree.css("div#genre-islands a.island"):
        code = _code(row)
        rank, delta = _rank_and_delta(row)
        if code is None or rank is None:
            PARSE_ERRORS.append({"dt": dt, "genre_slug": slug, "html": row.html[:400]})
            continue

        title = row.css_first("h3.island-title")
        creator = row.css_first("div.island-creator")
        ccu = row.css_first("div.ccu")
        peak = row.css_first("div.peak")

        rec: Dict[str, Any] = {
            "dt": dt,
            "fetched_at": fetched_at,
            "genre_slug": slug,
            "rank": rank,
            "rank_delta_gg": delta,
            "island_code": code,
            "title": title.text(strip=True) if title else None,
            "creator_code": creator.text(strip=True) if creator else None,
            "players_now": parse_num(ccu.text(strip=True).replace("Players Now", "")) if ccu else None,
            "all_time_peak": parse_num(peak.text(strip=True).replace("All-Time Peak", "")) if peak else None,
            "total_ranked_maps": total_ranked,
        }
        rec.update(_stats(row))
        ranks.append(rec)

    # Movers: alcancam ate ~#300, fora do teto de 50 da tabela principal.
    for col in tree.css("div.genre-movers-col"):
        title_node = col.css_first("div.genre-movers-col-title")
        bucket = title_node.text(strip=True) if title_node else None
        for mv in col.css("a.genre-mover"):
            code = _code(mv)
            rank_node = mv.css_first("div.genre-mover-rank")
            delta_node = mv.css_first("div.genre-mover-delta")
            name_node = mv.css_first("div.genre-mover-title")
            if code is None or rank_node is None:
                continue
            m = re.search(r"#\s*([\d,]+)", rank_node.text(strip=True))
            delta = None
            if delta_node is not None:
                val = parse_num(delta_node.text(strip=True))
                if val is not None:
                    classes = delta_node.attributes.get("class") or ""
                    delta = int(val) if "up" in classes else -int(val)
            movers.append({
                "dt": dt,
                "fetched_at": fetched_at,
                "genre_slug": slug,
                "bucket": bucket,
                "island_code": code,
                "title": name_node.text(strip=True) if name_node else None,
                "rank": int(m.group(1).replace(",", "")) if m else None,
                "rank_delta_gg": delta,
                "is_new": delta_node is not None and "new" in (delta_node.attributes.get("class") or ""),
            })

    return ranks, movers


def parse_genres_index(html: str, fetched_at: str, dt: str) -> List[Dict[str, Any]]:
    """Pagina /genres: ranking dos generos, com $/player - a metrica de monetizacao."""
    tree = HTMLParser(html)
    out: List[Dict[str, Any]] = []
    for card in tree.css("a[href^='/genre/']"):
        href = card.attributes.get("href") or ""
        slug = href.rsplit("/", 1)[-1]
        if slug not in GENRE_SLUGS:
            continue
        text = re.sub(r"\s+", " ", card.text(separator=" ", strip=True))
        rank = re.search(r"#(\d+)", text)
        nums = re.findall(
            r"([\d.,]+[KM]?)\s*(?:PLAYERS|MAPS)|([\d.,]+)\s*MIN|\$([\d.,]+)",
            text, re.I,
        )
        rec = {
            "dt": dt, "fetched_at": fetched_at, "genre_slug": slug,
            "genre_rank": int(rank.group(1)) if rank else None,
        }
        for field, pat in (
            ("players", r"([\d.,]+[KM]?)\s*PLAYERS"),
            ("maps", r"([\d.,]+[KM]?)\s*MAPS"),
            ("avg_playtime", r"([\d.,]+)\s*MIN"),
            ("avg_dollar_per_player", r"\$([\d.,]+)"),
        ):
            m = re.search(pat, text, re.I)
            rec[field] = parse_num(m.group(1)) if m else None
        if not any(r["genre_slug"] == slug for r in out):
            out.append(rec)
    return out


def main() -> None:
    f = fgg_fetcher()
    dt = today_str()
    fetched_at = now_utc().isoformat()
    all_ranks: List[Dict[str, Any]] = []
    all_movers: List[Dict[str, Any]] = []

    r = f.get(FGG + "/genres")
    if r is not None:
        save_raw_html(r.text, "genres", dt)
        write_snapshot(parse_genres_index(r.text, fetched_at, dt), "genres", dt)

    for slug in GENRE_SLUGS:
        r = f.get(FGG + "/genre/" + slug)
        if r is None:
            log.error("genero %s falhou", slug)
            continue
        save_raw_html(r.text, slug, dt)
        ranks, movers = parse_genre_page(r.text, slug, fetched_at, dt)
        log.info("%-22s %3d ranqueados, %3d movers", slug, len(ranks), len(movers))
        all_ranks.extend(ranks)
        all_movers.extend(movers)

    write_snapshot(all_ranks, "ranks", dt)
    write_snapshot(all_movers, "movers", dt)
    if PARSE_ERRORS:
        log.warning("%d blocos nao parseados", len(PARSE_ERRORS))
        write_snapshot(PARSE_ERRORS, "parse_errors", dt)


if __name__ == "__main__":
    main()
