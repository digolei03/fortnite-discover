"""Gera credenciais de device auth da Epic. RODE VOCE MESMO, localmente.

O device auth e um par (device_id, secret) de longa duracao, preso a uma conta
Epic. E o que permite ao coletor autenticar sem interacao humana.

COMO USAR

1. Faca login em https://www.epicgames.com com a conta SECUNDARIA que vai
   coletar (nunca a conta principal do estudio).

2. Abra a URL de autorizacao do cliente do Fortnite que voce esta usando e
   copie o `authorizationCode` do JSON devolvido. O codigo vale poucos segundos
   e serve uma vez so.

3. Rode, com EPIC_OAUTH_CLIENT_ID e EPIC_OAUTH_CLIENT_SECRET no ambiente:

       python -m tools.bootstrap_device_auth <authorizationCode>

4. Ele imprime tres valores. Coloque nos secrets do repositorio
   (Settings > Secrets and variables > Actions) ou num .env local:

       EPIC_DEVICE_AUTH_ACCOUNT_ID
       EPIC_DEVICE_AUTH_DEVICE_ID
       EPIC_DEVICE_AUTH_SECRET

O `secret` aparece UMA VEZ so na resposta da Epic. Se perder, gere outro.

Para revogar depois:
    DELETE account/api/public/account/<accountId>/deviceAuth/<deviceId>
"""
from __future__ import annotations

import sys

import httpx

from collectors.epic_auth import ACCOUNT_OAUTH_TOKEN, basic_auth_header

DEVICE_AUTH = ("https://account-public-service-prod.ol.epicgames.com"
               "/account/api/public/account/%s/deviceAuth")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    code = sys.argv[1].strip()

    with httpx.Client(timeout=30.0) as http:
        resp = http.post(
            ACCOUNT_OAUTH_TOKEN,
            headers={"Authorization": basic_auth_header(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "token_type": "eg1"})
        if resp.status_code != 200:
            raise SystemExit("Troca do authorization_code falhou (HTTP %d): %s\n"
                             "O codigo expira em segundos e so pode ser usado uma vez."
                             % (resp.status_code, resp.text[:300]))
        body = resp.json() or {}
        access_token, account_id = body.get("access_token"), body.get("account_id")
        if not access_token or not account_id:
            raise SystemExit("Resposta sem access_token/account_id")

        resp = http.post(DEVICE_AUTH % account_id,
                         headers={"Authorization": "Bearer " + access_token,
                                  "Content-Type": "application/json"})
        if resp.status_code not in (200, 201):
            raise SystemExit("Criacao do deviceAuth falhou (HTTP %d): %s"
                             % (resp.status_code, resp.text[:300]))
        device = resp.json() or {}

    print("\nGuarde estes valores como secrets. O secret nao pode ser recuperado depois.\n")
    print("EPIC_DEVICE_AUTH_ACCOUNT_ID=" + str(device.get("accountId") or account_id))
    print("EPIC_DEVICE_AUTH_DEVICE_ID=" + str(device.get("deviceId")))
    print("EPIC_DEVICE_AUTH_SECRET=" + str(device.get("secret")))
    print("\nNao cole estes valores em chat nem commite no repositorio.")


if __name__ == "__main__":
    main()
