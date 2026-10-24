# Zorin Connect Android 1.35.13: referência para o app próprio

APK puxado do POCO (`com.zorinos.zorin_connect`, versionCode 13513, minSdk 32, targetSdk 37), sha256 `3b475385…f7a`. É um fork do KDE Connect Android (GPL), por isso serve para estudar padrões, sem copiar código.

Ferramenta: REA + JADX headless 0.7.1. Evidências: ev_37646c09… (classe), ev_06660c2e… (performControlAction), ev_9d744364… (createStatefulBuilder). Análise estática, nada foi executado.

## Pontos de entrada de "atalho no telefone" (manifesto)
- `RunCommandControlsProviderService`: Device Controls do Android 11+ (menu de controles).
- `RunCommandWidgetProvider` + `CommandsRemoteViewsService`: widget de lista na tela inicial.
- `RunCommandUrlActivity`: deep link que dispara um comando (útil para Tasker e atalhos).
- `ClipboardTileService`: tile de Quick Settings para clipboard.
- `ShareBroadcastReceiver` / `SEND` / `SEND_MULTIPLE`: alvo de compartilhamento.

## Padrão do Device Controls (observado no código descompilado)
- O id do controle é `"<deviceId>:<commandKey>"`. Ao executar, o app separa o deviceId, pega o plugin do dispositivo e envia só a `key`.
- `Control.StatefulBuilder(id, pendingIntent)` com `title` = nome do comando, `subtitle` = comando, `structure` = nome do dispositivo (agrupa por PC), `StatelessTemplate(key)`, `deviceType 52` e ícone próprio.
- `performControlAction`: aceita só `CommandAction`, chama `consumer.accept(1)` em caso de sucesso e `2` em caso de falha, e publica o estado de volta (`setStatus(1)`, "toque para executar") num `ReplayProcessor`.

## O que levar para o Nexus
- O telefone nunca envia comando livre, só o id estável de uma ação do catálogo do PC (o mesmo modelo de segurança do `command-list` do lado Linux).
- O catálogo de ações é publicado pelo PC e o app gera dinamicamente os Device Controls, o widget e os tiles a partir dele.
