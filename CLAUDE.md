# Nexus — memória do projeto (leia antes de trabalhar)

Integração PC Linux (Zorin OS 18 / GNOME 46 Wayland) ↔ Android, própria e cifrada. Visão geral para humanos: `README.md`.
Histórico de tarefas e status: `.ia/plan.md`. Decisões: `docs/architecture/decisions/0001…0005`. Protocolo: `docs/protocol/v1.md`.
Repositórios de referência (Beamer, Coucou, Zorin Connect) e o commit estudado de cada um: `docs/research/referencias.md`.

## Como trabalhar aqui
- **Idioma:** responda, comente e escreva a UI em **português do Brasil**.
- **Commits:** **nunca** faça commit ou push sem o usuário pedir. Remotos: `git@github.com:JvFontenele/nexus.git` (branch `main`) e `git@github.com:JvFontenele/balao.git`. Use SSH; não há `gh` instalado.
- **ChatGPT como orquestrador/revisor:** a conversa fica no Chrome, em https://chatgpt.com/c/6adcc434-05dc-83e8-9f0d-8d66687d6e59.
  - **Quando:** em cada marco ou decisão de arquitetura, mande uma consulta com o cabeçalho `Projeto | Fase | Consulta #ID | Objetivo | Contexto técnico | Tentativas/resultados | Restrições | Decisão necessária | Resultado esperado`, terminando com "Responda curto.".
  - **Numeração:** a última foi a **#013**; a próxima é a **#014**.
  - **Pendente (#014):** não foi enviada porque a extensão do Chrome não estava conectada à conta pessoal. Assunto: áudio do aparelho → PC (AudioPlaybackCapture + `audio.push`, vários aparelhos mixados no PipeWire) e reorganização dos apps (abas no PC, barra inferior no Android, Preferências).
  - **Ler a resposta:** pegue o texto depois de "Responda curto." em `main.innerText`, em pedaços de até 2000 caracteres.
  - **Plano gratuito:** a conversa está longa, e o próprio ChatGPT avisa que perde detalhes.
- **ia-local:** o modelo local Qwen (comando `ia`) inventou schemas inseguros na T02a. Código de segurança, protocolo e pareamento é escrito e revisado pelo Claude.
- **Regras do prompt original (seções 1–21 na conversa do ChatGPT):**
  - sem root como requisito;
  - sem dependência nova sem justificar;
  - sem shell com dado vindo da rede;
  - mudanças destrutivas só com aprovação.
- **Comandos com `pkill`:** use o truque do colchete (`pkill -f "[n]exus daemon"`), senão o comando se mata.

## Arquitetura (resumo)
- **`nexusd`** (`nexus/daemon.py`, asyncio):
  - **Rede:** TCP 47210 + `start_tls` com o `SSLContext` vigente; TLS 1.3, mTLS com `CERT_OPTIONAL`. A autoridade é o `trusted.json` (`lookup_der`), não o handshake.
  - **Conexões:** 1 de controle por dispositivo, registrada no `hello`. As conexões de dados (`audio.attach`, `file.push`, `file.pull`) não a substituem.
  - **Ações remotas:** só `phone_safe` + permissão por dispositivo; nunca shell.
  - **Interfaces locais:** D-Bus `dev.nexus.Daemon1` (`nexus/dbus.py`, thread do GLib) e socket de controle para a CLI.
  - **Execução:** roda como `systemctl --user` (`packaging/`). O IP é lido na partida; ao trocar de rede, rode `systemctl --user restart nexusd`.
- **Identidade e pareamento:** `nexus/proto/` (identity, pairing SAS commit/reveal, trust, framing, messages). Dados ficam em `~/.local/share/nexus/` e log em `~/.local/state/nexus/events.jsonl` (só chaves, nunca conteúdo).
- **Áudio** (`nexus/audio.py`, ADR-0004): saída virtual `nexus-audio` vira a padrão, `pw-cat` captura, PCM 48 kHz vai por TLS, e o celular toca no AudioTrack. A saída anterior sempre é restaurada.
- **Claude Code** (`nexus/agentes.py`): hook `PermissionRequest` (`python3 -P …/nexus/agentes.py hook`) → `agent.request` no socket de controle → cartão no notch (Negar/Permitir/Sempre/No terminal) → decisão volta ao hook. Qualquer falha = sem decisão (o terminal pergunta); nunca aprova sozinho.
- **Arquivos** (`nexus/files.py`): `~/Downloads/Nexus`; PC → celular por `file.offer`/`file.pull` (uso único). Progresso em `file.progress`/`file.done`, cancelamento por `file.cancel`.
- **Espelhar:** `scrcpy` com argumentos fixos. O dispositivo é casado pelo `model:` do `adb devices -l` (com `-` virando `_`); o adb sem fio é reconectado pelo Avahi `_adb-tls-connect`, só para o IP do dispositivo conectado.
- **Frontends:**
  - **Balão** (repo `~/Documentos/projetos/pesquisa-linux`): `nexus.js` + `notch.js`, opção "Usar o Nexus".
  - **App GTK:** `nexus/app.py`, com o Nexo em `nexus/mascote.py`.
  - **Android:** `android/` (LinkService persistente, blocos rápidos, Compose).

## Rodar, testar e compilar
```bash
python3 -m unittest discover -s tests                       # 185 testes (inclui TLS real e D-Bus em barramento privado)
cd ../pesquisa-linux && tests/aninhado.sh                   # 36 testes do Balão num GNOME Shell aninhado (abre uma janela)
cd android && JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 ~/.local/opt/gradle-9.8.2/bin/gradle -q assembleDebug
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
systemctl --user restart nexusd                             # depois de mudar código Python
```
- **Gradle:** não há wrapper. Se o Kotlin acusar "Unresolved reference" em classe existente, rode `gradle clean assembleDebug`.
- **Balão:** mudanças só valem no Shell real depois de **sair e entrar na sessão** (Wayland). Nos testes, não envie teclas virtuais.
- **Testes de pareamento:** precisam `self.d._pairing_notice` neutralizado, senão notificam a área de trabalho real.
- **Prints:**
  - **celular:** `adb exec-out screencap -p`;
  - **app GTK:** renderize a janela com `Gtk.WidgetPaintable` (ver o histórico);
  - **Shell:** use as fotos de `tests/aninhado.sh` em `/tmp/balao-*/fotos`.

## Aparelhos e ambiente
- **POCO X5 Pro** (`22101320G`, HyperOS/Android 14): pareado. O HyperOS mata o serviço (o app o religa no `onStart`); o adb sem fio está pareado com este PC.
- **Galaxy Tab S10 Lite** (`SM-X400`): ainda **não** pareado, porque estava em outra rede Wi-Fi. Cuidado com a ancoragem USB ligada.
- **ufw:** 47210/tcp liberado para 192.168.0.0/24 e para o tailscale0. Ao mudar de rede, confira se a rede atual está liberada.
- **Tailscale do PC:** `100.77.106.53`. No celular, o Tailscale não respondia no último teste.

## Pendências e próximas etapas (em ordem)
0. **Aprovar o Claude Code pelo notch (Fase B, pronta, sem instalar):** `nexus agents install [--config-dir DIR]` mostra o diff do hook `PermissionRequest` no `settings.json` e só grava com `--yes` (backup `settings.json.bak-AAAAMMDD-HHMMSS`). Instalar em `~/.claude` e `~/.claude-pessoal` só com o usuário. Depois: validar com uma sessão real; **Fase C** = aprovar pelo celular (mensagem nova no protocolo, permissão por dispositivo desligada por padrão); AskUserQuestion fora por ora.
1. **Tablet:** parear na mesma rede e validar multidispositivo (áudio, arquivo e espelhamento para o dispositivo certo; o seletor de alvo no notch).
2. **Android no boot:** `BOOT_COMPLETED` + guia de "Sem restrições" de bateria/autostart no HyperOS/One UI. Validar após bloqueio, remoção dos recentes e reinício.
3. **Testes longos (#011/#012):** áudio por 30–60 min, queda de Wi-Fi no meio, bloquear/desbloquear o celular, latência pela tailnet (Opus só se precisar).
4. **Cancelar transferência pelo celular:** hoje só o PC cancela, e o celular para junto.
5. **Arrastar arquivos até o notch:** prova de conceito de DnD no Shell/Wayland. Se não der, o app GTK recebe o arrasto.
6. **IP dinâmico:** o daemon reanunciar e reescutar quando a rede muda, sem precisar reiniciar. Excluir interfaces de ancoragem USB (`enx…`) do `default_listen`.
7. **Revisão de escopo da v0.1** contra o prompt original: IA, ações dinâmicas/configuráveis, VPN completa pela tailnet, distribuição para outras distros (empacotamento, desinstalação).
