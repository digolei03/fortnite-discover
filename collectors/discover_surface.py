"""Coleta a Discover direto da API da Epic, um snapshot por perfil.

Substitui o caminho do fortnite.gg como fonte de colocacao. A diferenca nao e
so de procedencia: aqui cada linha sabe DE QUEM e a leitura.

    data/surface/dt=AAAA-MM-DD/surface.parquet

Um snapshot e o produto cartesiano dos perfis em config/profiles.json
(regiao x plataforma x locale) pelos paineis retornados. Cada linha guarda
`test_variant_name`, porque a Epic roda testes A/B na Discover e a posicao so
faz sentido dentro de uma variante.

CADENCIA: a chamada e autenticada como jogador. Rode pouco - 2x ao dia com
poucos perfis, nao um loop. Ver o aviso de risco de conta em epic_auth.py.
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List

from .common import CONFIG, log, now_utc, today_str, write_snapshot
from .epic_auth import EpicSession

DEFAULT_SURFACE = "CreativeDiscoverySurface_Frontend"
DEFAULT_PROFILES = [{"region": "BR", "platform": "Windows", "locale": "pt-BR"}]

# Guarda-corpos: um painel com muitas paginas viraria dezenas de chamadas.
MAX_PAGES_PER_PANEL = 5
PAUSE_BETWEEN_CALLS = 1.0


def load_profiles() -> List[Dict[str, str]]:
    path = CONFIG / "profiles.json"
    if not path.exists():
        log.warning("config/profiles.json ausente - usando o perfil padrao (BR/Windows/pt-BR)")
        return DEFAULT_PROFILES
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("profiles", DEFAULT_PROFILES) if isinstance(data, dict) else data


def _entry_rows(results: List[Dict[str, Any]], start_rank: int, page_index: int,
                panel: Dict[str, Any], base: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    rank = start_rank
    for item in results:
        link_code = str(item.get("linkCode") or "")
        if not link_code:
            continue
        row = dict(base)
        row.update({
            "panel_name": panel.get("panel_name"),
            "panel_display_name": panel.get("panel_display_name"),
            "panel_type": panel.get("panel_type"),
            "feature_tags": panel.get("feature_tags"),
            "page_index": page_index,
            "rank": rank,
            "link_code": link_code,
            # codigo de ilha e NNNN-NNNN-NNNN; o resto e colecao
            "link_code_type": "island" if link_code.count("-") == 2 and
                              link_code.replace("-", "").isdigit() else "collection",
            "island_code": link_code if link_code.count("-") == 2 and
                           link_code.replace("-", "").isdigit() else None,
            "global_ccu": item.get("globalCCU"),
            "is_visible": item.get("isVisible"),
            "lock_status": item.get("lockStatus"),
            "lock_status_reason": item.get("lockStatusReason"),
        })
        rows.append(row)
        rank += 1
    return rows


def collect_profile(session: EpicSession, surface_name: str,
                    profile_spec: Dict[str, str]) -> List[Dict[str, Any]]:
    profile = session.profile_body(
        region=profile_spec["region"],
        platform=profile_spec.get("platform", "Windows"),
        locale=profile_spec.get("locale", "en"))

    payload = session.surface(surface_name, profile)
    variant = str(payload.get("testVariantName") or "Baseline")
    base = {
        "dt": today_str(),
        "fetched_at": now_utc().isoformat(),
        "surface_name": surface_name,
        "region": profile_spec["region"],
        "platform": profile_spec.get("platform", "Windows"),
        "locale": profile_spec.get("locale", "en"),
        "branch": session.branch,
        "test_variant_name": variant,
        "test_name": payload.get("testName"),
        "test_analytics_id": payload.get("testAnalyticsId"),
    }

    rows: List[Dict[str, Any]] = []
    for raw_panel in payload.get("panels") or []:
        panel_name = str(raw_panel.get("panelName") or "")
        if not panel_name:
            continue
        panel = {
            "panel_name": panel_name,
            "panel_display_name": raw_panel.get("panelDisplayName"),
            "panel_type": raw_panel.get("panelType"),
            "feature_tags": ",".join(str(t) for t in (raw_panel.get("featureTags") or [])) or None,
        }
        first = raw_panel.get("firstPage") or {}
        results = first.get("results") or []
        rows.extend(_entry_rows(results, 1, 0, panel, base))

        if not first.get("hasMore"):
            continue
        rank = len(results) + 1
        for page_index in range(1, MAX_PAGES_PER_PANEL + 1):
            time.sleep(PAUSE_BETWEEN_CALLS)
            page = session.page(surface_name, panel_name, page_index, variant, profile)
            page_results = page.get("results") or []
            if not page_results:
                break
            rows.extend(_entry_rows(page_results, rank, page_index, panel, base))
            rank += len(page_results)
            if not page.get("hasMore"):
                break

    log.info("  %s/%s/%s | variante=%s | %d paineis, %d entradas",
             profile_spec["region"], profile_spec.get("platform", "Windows"),
             profile_spec.get("locale", "en"), variant,
             len(payload.get("panels") or []), len(rows))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--surface", default=DEFAULT_SURFACE)
    args = ap.parse_args()

    profiles = load_profiles()
    session = EpicSession().login()
    log.info("autenticado | branch=%s | %d perfis", session.branch, len(profiles))

    rows: List[Dict[str, Any]] = []
    for spec in profiles:
        try:
            rows.extend(collect_profile(session, args.surface, spec))
        except SystemExit as exc:
            log.error("perfil %s falhou: %s", spec, exc)
        time.sleep(PAUSE_BETWEEN_CALLS)

    write_snapshot(rows, "surface", today_str())


if __name__ == "__main__":
    main()
