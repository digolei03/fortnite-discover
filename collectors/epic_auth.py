"""Cadeia de autenticacao da Epic para a API de Discovery.

    device_auth  ->  token EG1  ->  token de discovery  ->  surface

Nada aqui e documentado publicamente pela Epic. O contrato foi extraido por
leitura e esta descrito abaixo para nao virar magia:

1. `GET fortnite/api/version` devolve a versao viva. O parametro `stream` de
   todas as chamadas seguintes e `++Fortnite+Release-<versao>`. Ele muda a cada
   atualizacao do jogo, entao e resolvido em tempo de execucao - fixar isso
   quebra sozinho na proxima temporada.

2. `POST account/api/oauth/token` com `grant_type=device_auth` e
   `token_type=eg1` troca as credenciais de dispositivo por um access token.
   O Basic Auth usa o par client_id:client_secret de um cliente do Fortnite.

3. `GET fortnite/api/discovery/accessToken/<branch>` troca o token EG1 por um
   token especifico de discovery, que vai no header `X-Epic-Access-Token`.

CREDENCIAIS (variaveis de ambiente, nunca commitadas, nunca impressas):

    EPIC_OAUTH_CLIENT_ID
    EPIC_OAUTH_CLIENT_SECRET
    EPIC_DEVICE_AUTH_ACCOUNT_ID
    EPIC_DEVICE_AUTH_DEVICE_ID
    EPIC_DEVICE_AUTH_SECRET

Gere as tres ultimas com `python -m tools.bootstrap_device_auth`.

RISCO DE CONTA: estas chamadas sao feitas como se fossem de um jogador. Use
uma conta secundaria, NUNCA a conta principal do estudio, e mantenha a
cadencia baixa.
"""
from __future__ import annotations

import base64
import os
from typing import Dict, Optional
from urllib.parse import quote

import httpx

ACCOUNT_OAUTH_TOKEN = "https://account-public-service-prod.ol.epicgames.com/account/api/oauth/token"
FORTNITE_VERSION = "https://fngw-mcp-gc-livefn.ol.epicgames.com/fortnite/api/version"
DISCOVERY_ACCESS_TOKEN = "https://fngw-mcp-gc-livefn.ol.epicgames.com/fortnite/api/discovery/accessToken"
DISCOVERY_SURFACE = ("https://fn-service-discovery-live-public.ogs.live.on.epicgames.com"
                     "/api/v2/discovery/surface")

REQUIRED = (
    "EPIC_OAUTH_CLIENT_ID", "EPIC_OAUTH_CLIENT_SECRET",
    "EPIC_DEVICE_AUTH_ACCOUNT_ID", "EPIC_DEVICE_AUTH_DEVICE_ID", "EPIC_DEVICE_AUTH_SECRET",
)


def _env(name: str) -> str:
    value = os.environ.get(name) or ""
    if not value:
        missing = [k for k in REQUIRED if not os.environ.get(k)]
        raise SystemExit(
            "Faltam credenciais da Epic: " + ", ".join(missing) + "\n"
            "Coloque num .env local ou nos secrets do repositorio.\n"
            "Para gerar o device auth: python -m tools.bootstrap_device_auth")
    return value


def basic_auth_header() -> str:
    pair = _env("EPIC_OAUTH_CLIENT_ID") + ":" + _env("EPIC_OAUTH_CLIENT_SECRET")
    return "Basic " + base64.b64encode(pair.encode()).decode()


class EpicSession:
    """Mantem a cadeia de tokens viva para uma rodada de coleta."""

    def __init__(self, timeout: float = 30.0):
        self._http = httpx.Client(timeout=timeout)
        self.account_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.branch: Optional[str] = None
        self.discovery_token: Optional[str] = None

    def _raise(self, resp: httpx.Response, what: str) -> None:
        try:
            payload = resp.json()
            detail = payload.get("errorMessage") or payload.get("error") or payload.get("errorCode")
        except Exception:
            detail = resp.text[:200]
        corr = resp.headers.get("x-epic-correlation-id")
        raise SystemExit("%s falhou (HTTP %d): %s%s" % (
            what, resp.status_code, detail, (" [correlation %s]" % corr) if corr else ""))

    def login(self) -> "EpicSession":
        # 1) versao viva -> branch
        resp = self._http.get(FORTNITE_VERSION)
        if resp.status_code != 200:
            self._raise(resp, "fortnite/api/version")
        version = (resp.json() or {}).get("version")
        if not version:
            raise SystemExit("fortnite/api/version devolveu versao vazia")
        self.branch = "++Fortnite+Release-" + str(version)

        # 2) device_auth -> token EG1
        resp = self._http.post(
            ACCOUNT_OAUTH_TOKEN,
            headers={"Authorization": basic_auth_header(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "device_auth",
                  "account_id": _env("EPIC_DEVICE_AUTH_ACCOUNT_ID"),
                  "device_id": _env("EPIC_DEVICE_AUTH_DEVICE_ID"),
                  "secret": _env("EPIC_DEVICE_AUTH_SECRET"),
                  "token_type": "eg1"})
        if resp.status_code != 200:
            self._raise(resp, "oauth device_auth")
        body = resp.json() or {}
        self.access_token = body.get("access_token")
        self.account_id = body.get("account_id") or os.environ.get("EPIC_DEVICE_AUTH_ACCOUNT_ID")
        if not self.access_token:
            raise SystemExit("oauth device_auth devolveu access_token vazio")

        # 3) token EG1 -> token de discovery
        # O branch ("++Fortnite+Release-XX.YY") precisa ir percent-encoded: os "+"
        # sao significativos e nao podem ser entregues crus no path.
        resp = self._http.get(
            DISCOVERY_ACCESS_TOKEN + "/" + quote(self.branch, safe=""),
            headers={"Authorization": "Bearer " + self.access_token,
                     "Accept": "application/json",
                     "User-Agent": "Fortnite/%s Windows/10" % self.branch})
        if resp.status_code != 200:
            self._raise(resp, "discovery/accessToken")
        self.discovery_token = (resp.json() or {}).get("token")
        if not self.discovery_token:
            raise SystemExit("discovery/accessToken devolveu token vazio")
        return self

    # -- Discovery -----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer " + str(self.access_token),
                "X-Epic-Access-Token": str(self.discovery_token),
                "Content-Type": "application/json",
                "Accept": "application/json"}

    def profile_body(self, region: str, platform: str, locale: str,
                     rating: str = "TEEN", rating_authority: str = "ESRB") -> Dict[str, object]:
        """O corpo da requisicao E o perfil do jogador.

        E por isso que nao existe "o rank": a resposta muda conforme quem
        pergunta. Toda linha coletada carrega estes campos junto.
        """
        return {"playerId": self.account_id, "partyMemberIds": [self.account_id],
                "locale": locale, "matchmakingRegion": region, "platform": platform,
                "isCabined": False, "ratingAuthority": rating_authority,
                "rating": rating, "numLocalPlayers": 1}

    def surface(self, surface_name: str, profile: Dict[str, object]) -> Dict[str, object]:
        resp = self._http.post(
            "%s/%s" % (DISCOVERY_SURFACE, surface_name),
            params={"appId": "Fortnite", "stream": self.branch},
            headers=self._headers(), json=profile)
        if resp.status_code != 200:
            self._raise(resp, "v2/discovery/surface")
        return resp.json() or {}

    def page(self, surface_name: str, panel_name: str, page_index: int,
             test_variant_name: str, profile: Dict[str, object]) -> Dict[str, object]:
        body = dict(profile)
        body.update({"testVariantName": test_variant_name,
                     "panelName": panel_name, "pageIndex": page_index})
        resp = self._http.post(
            "%s/%s/page" % (DISCOVERY_SURFACE, surface_name),
            params={"appId": "Fortnite", "stream": self.branch},
            headers=self._headers(), json=body)
        if resp.status_code != 200:
            self._raise(resp, "v2/discovery/surface/page")
        return resp.json() or {}
