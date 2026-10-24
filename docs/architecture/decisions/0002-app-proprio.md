# ADR-0002 — Ambiente próprio, sem depender do Zorin/KDE Connect

Data: 2026-10-24 · Status: aprovada (consulta #004 ao ChatGPT) · Substitui as partes de integração do ADR-0001

## Motivo
O usuário quer instalar o Nexus em outras máquinas e distros. Zorin Connect, KDE Connect, GSConnect e Roc passam a ser referências de estudo, não dependências (`docs/research/`).

## Decisão
- **Protocolo:** próprio e versionado, sobre TCP + TLS 1.3, com mensagens JSON delimitadas, limite de tamanho, timeouts e validação rígida. Não reproduz o KDE Connect e não tem criptografia própria.
- **Segurança:** identidade persistente por dispositivo (certificado autoassinado, pinning após o pareamento); pareamento por código curto com expiração, confirmado nos dois lados; revogação; permissões por ação. O telefone só envia o id de uma ação do catálogo, nunca um comando livre.
- **Linux:** daemon `nexusd` em Python 3.12 (núcleo de T01).
- **Android:** Kotlin nativo + Jetpack Compose (Quick Settings tiles, widget, Device Controls, share target).
- **Descoberta:** mDNS (`_nexus._tcp`) na LAN; fallback manual por IP/hostname e QR code; broadcast UDP opcional. Pela tailnet, o mesmo canal.
- **Áudio:** receptor próprio com AudioTrack; RTP fica como PoC (já funciona com o VLC); comparar PCM L16 com Opus antes de fixar o formato.
- **Espelhamento:** scrcpy como ferramenta externa na v0.1.

## Ordem
T01 → T02' (protocolo, TLS, identidade, pareamento, reconexão, mDNS) → A01 (app mínimo: conectar, parear, ping) → A02 (catálogo remoto, compartilhar texto/arquivo, clipboard) → A03 (tiles e share target) → A04 (receptor de áudio, L16 × Opus, latência) → A05 (scrcpy e atalhos GNOME).

## Fora da v0.1
Controle remoto completo e apps Linux no Android; histórico/contexto unificado, automações e IA; suporte garantido a todas as distros e fabricantes; áudio bidirecional avançado; compatibilidade com o protocolo KDE Connect.

## Aceite central
Parear um PC Linux com o app Nexus Android, reconectar com segurança, executar uma ação autorizada pelo telefone e compartilhar texto, sem o Zorin Connect. Áudio experimental: só conta como concluído com reprodução real e latência medida.
