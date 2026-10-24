# ADR-0003 — Nexus e Balão: apps separados, integração opcional

Data: 2026-10-24 · Status: aprovada pelo usuário · Complementa o ADR-0002

## Contexto
O Balão (`~/Documentos/projetos/pesquisa-linux`, extensão do GNOME Shell 46) já está pronto e em uso.
Ele faz correção ortográfica, pesquisa e histórico da área de transferência em balões junto do cursor. O usuário quer que o Balão seja a interface principal no
GNOME e que o Nexus acrescente a ele as funções de dispositivo, mantendo os dois instaláveis e utilizáveis sozinhos.

## Decisão
- **Repositórios separados.** O Nexus (daemon, protocolo, app Linux, app Android) e o Balão (extensão) continuam independentes.
- **`nexusd` é o dono da rede e da segurança** (TLS, pareamento, store, ações). Nada de rede dentro do Shell: uma falha
  ali congela a sessão gráfica.
- **API local:** o `nexusd` exporta `dev.nexus.Daemon` no D-Bus da sessão (`/dev/nexus/Daemon`, interface `dev.nexus.Daemon1`):
  - `Command(s json) → s json`: os mesmos comandos do socket de controle (`pair.pending`, `pair.confirm`, `devices.*`, `send.*`…).
  - sinal `Event(s json)`: `pair.pending`, `pair.result`, `text.received`, `device.connected` e `device.disconnected`.
  - O barramento da sessão só aceita o próprio usuário, o mesmo nível de confiança do socket de controle (que segue para a CLI).
- **Frontends:**
  - **Balão + módulo Nexus** (GNOME). Liga-se nas preferências do Balão ("Integração com o Nexus"). Com o `nexusd` presente, ele
    acrescenta itens aos balões ("Enviar para…", "Pesquisar no dispositivo"), mostra o pedido de pareamento num balão e coloca o
    texto recebido na área de transferência (`St.Clipboard`) e no histórico. Ele ocupa `dev.nexus.Shell` no barramento para o daemon saber
    que há um frontend do Shell.
  - **App Nexus (GTK4/libadwaita, Python)**, que funciona com ou sem o Balão: dispositivos, pareamento com código, permissões por ação,
    enviar texto e executar ações. Sem o Balão, os pedidos de pareamento chegam como notificação com o botão "Abrir", que abre o app.
- **Área de transferência no PC:** `dev.nexus.Shell` presente → o Balão escreve. Ausente → `wl-copy`, se existir. Senão, só a notificação.
- **Android:** sem mudanças de arquitetura.

## Instalação esperada
1. Balão (opcional). 2. Nexus: `nexusd` como serviço systemd **de usuário** (sem root) + o app com `.desktop`.
3. No Balão: Preferências → "Integração com o Nexus".

## Ordem
B01 D-Bus do `nexusd` → B02 app Nexus GTK → B03 módulo Nexus no Balão → A03 conexão persistente do Android
(foreground service; PC→telefone: texto, abrir URL) → ações em balão, Quick Settings, áudio.
