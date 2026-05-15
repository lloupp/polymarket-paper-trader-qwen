# APK Android (WebView + Start/Stop/Status)

Este app Android é um cliente para o seu servidor do trader.

## Importante
- O app **não roda o backend Python sozinho**.
- Ele controla o backend via HTTP:
  - `/api/control/start`
  - `/api/control/stop`
  - `/api/control/status`
- O dashboard abre em WebView.

## Passos
1. Abra `android-app/` no Android Studio.
2. Em `MainActivity.kt`, ajuste:
   - `baseUrl` para IP/porta do servidor (ex.: `http://192.168.0.15:8090`)
   - `token` se você usar `CONTROL_TOKEN`.
3. Build > Build APK(s).

## Segurança
Defina token no servidor (.env):

`CONTROL_TOKEN=troque-isto`

No app, preencha o mesmo token em `MainActivity.kt`.

## Observação de rede
No Android, `127.0.0.1` aponta para o próprio celular, não para seu PC.
Use o IP local do PC/servidor.
