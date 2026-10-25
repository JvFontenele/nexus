# Nexus v0.1 — plano

Decisões: ADR-0001 (ambiente medido) e **ADR-0002 (app próprio, vigente)**. Revisor: ChatGPT (consultas #001–#004).

Linux: pacote Python 3.12 `nexus/` (stdlib + `gi`). Testes: `python3 -m unittest discover -s tests -v`.
Android: `android/` (Kotlin + Compose). Build: ver memória build-android-ambiente.

| # | Tarefa | Quem | Status |
|---|---|---|---|
| T01 | núcleo: catálogo, CLI, log | ia-local | feito |
| T01b | correções (exceção mascarada, timeout, log em finally, teste) | ia-local | feito (23 testes OK) |
| T02 | especificação do protocolo v1 + modelo de ameaças (`docs/protocol/v1.md`) | Claude (segurança) | feito, revisado #005, E1 ok |
| T02a | framing JSON + validação de mensagens (Python) | ia-local → Claude reescreveu messages.py (schemas inventados, tipo `shell`) | feito (23 testes) |
| T02b | TLS, identidade e pareamento por código (Python) | Claude | feito (identity/pairing/trust, 20 testes) |
| T02c | `nexusd`: servidor asyncio, controle local (CLI pair/devices), mDNS Avahi (#006) | Claude | feito (18 testes de integração; total 84) |
| A00 | Android SDK 36/37 + Gradle 9.8.2 + esqueleto Compose que compila | Claude | feito (install no POCO precisa de "Instalar via USB") |
| A01 | app mínimo: conectar, parear (SAS), ping com mTLS + pin, NSD | Claude | feito: pareado de verdade com o POCO em 2026-10-24 (códigos iguais, ping ok) |
| A02 | catálogo remoto, compartilhar texto, clipboard do PC (wl-copy) | Claude | testada no POCO: negada → forbidden, liberada → ok, share → PC (fallback de notificação; falta o wl-clipboard). Arquivo → A03 |
| A03 | Quick Settings tiles, Device Controls, share target | ia-local | pendente |
| A04 | receptor de áudio (AudioTrack), L16 × Opus, latência | ia-local + Claude mede | pendente |
| A05 | scrcpy e atalhos GNOME | ia-local | pendente |

PoC concluída: áudio PC→POCO por RTP (PipeWire rtp-sink → VLC), ouvido pelo usuário em 2026-10-24.

## Decisões A01 (consulta #007)
- Identidade: EC P-256 no AndroidKeyStore, chave não exportável. O certificado autoassinado vem do próprio KeyStore (`PURPOSE_SIGN`, SHA-256). Conferir no aparelho que a assinatura é real; se não for, reabrir a decisão. BouncyCastle só se houver necessidade comprovada.
- Servidor: TrustManager próprio. Sem pin, aceita o certificado só para o pareamento, e o SAS prova a identidade. Depois exige fp_s exato (DataStore), e qualquer falha fecha a conexão. Trocar a identidade do PC exige parear de novo, nunca aceitar em silêncio.
- Descoberta: NsdManager + IP manual. Os dados do mDNS não são confiáveis.
- ufw: 47210/tcp só de 192.168.0.0/24 e na interface tailscale0 (o usuário roda o sudo).
- Riscos pendentes: reinstalação ou limpeza de dados = novo pareamento (revogar o antigo no PC); timeouts e backoff de reconexão; teste no aparelho real.

## Decisões A02 (consulta #008)
- Clipboard do PC: wl-clipboard (`wl-copy`, apt, o usuário roda o sudo). Se falhar, cai para uma notificação com o texto. O clipboard só é trocado por ação explícita, como um compartilhamento feito no telefone.
- Android → PC: share target ACTION_SEND (texto) na A02. Tile + Activity com foco para ler o clipboard: A03.
- PC → Android: na A02 só o telefone inicia (request/response). O push persistente (foreground service + backoff, cuidando do HyperOS) fica para a A03.
- Ações: actions.list + action.run. O app mostra só as phone_safe. O daemon valida tudo e as permissões começam vazias.
- Aceite: no aparelho real, catálogo, execução autorizada e negada, compartilhamento, falha de clipboard e reconexão mTLS.

## Integração com o Balão (ADR-0003, consulta #009)
| # | Tarefa | Quem | Status |
|---|---|---|---|
| B01 | D-Bus `dev.nexus.Daemon1` (Command/Event), uid auditado, limite de tamanho, nome liberado no stop | Claude | feito (7 testes em barramento privado; testado na sessão real) |
| B02 | app Nexus GTK4/libadwaita: dispositivos, permissões, revogar, pareamento com código; notificação "Abrir" sem o Balão; `packaging/` (systemd de usuário + .desktop) | Claude | feito; falta o usuário testar a janela e decidir sobre o install.sh |
| B03 | módulo Nexus no Balão (`nexus.js`, repo do Balão) | Claude | feito e validado no uso real em 2026-10-24 (31/31 no aninhado) |
| A03 | Android: conexão persistente (foreground service connectedDevice, ping 30 s, backoff até 60 s) + PC→telefone (share.text → clipboard, open.url → notificação) | Claude | testada no POCO: reconectou em ~4 s após reiniciar o nexusd; texto e link chegaram; javascript: recusado. Frontends: Balão (Enviar/Pesquisar no dispositivo, Ctrl+E) |
| A03b | Quick Settings: "Clipboard → PC" (Activity transparente lê com foco) e "Ação do PC" (ação sem parâmetros escolhida no app); operações do app reaproveitam a conexão persistente (resposta casada por `re`) | Claude | validado no POCO em 2026-10-24 (clipboard 56 caracteres → PC; play/pause pausou o PC; sem queda da persistente) |
| A05 | Espelhar com scrcpy: `Command{cmd:"mirror"}` casa `hello.name` (Build.MODEL) com `model:` do `adb devices -l`, argumentos fixos; Balão "Espelhar tela do …" (também com campo vazio), botão no app GTK | Claude | espelhado sem cabo (adb sem fio + reconexão via Avahi só no IP do celular conectado); bloco "Espelhar no PC" (`mirror.request`); Balão Super+Shift+M; `nexus mirror` (CLI p/ atalho sem Balão). validado no uso real em 2026-10-24 (atalho e bloco; adb sem fio reencontrado após login) |
| A04 | Áudio PC → celular (ADR-0004): saída virtual "Nexus → …" padrão + `pw-cat`, 2ª conexão TLS `audio.attach`, PCM 48 kHz, AudioTrack low-latency, descarte > 80 ms | Claude | validado no POCO (bipe e música ouvidos, som limpo, 2 min estável); latência estimada 195 → 115 ms com buffer mínimo, 0 cortes. Botões: Balão "Áudio do PC no …"/"Parar áudio" e app GTK "Áudio"; evento audio.changed. Aprovado na #011; riscos em ADR-0004 |
| A06 | Arquivos (#011): conexão TLS de dados `file.push` (celular → `~/Downloads/Nexus`, nome saneado, `.part` + `xb`, sem sobrescrever, ≤ 4 GiB) e `file.offer`/`file.pull` (PC → `Download/Nexus` via MediaStore; oferta de uso único, 5 min, validada antes de consumir). Frontends: Compartilhar (1 ou vários), app GTK "Enviar arquivo", Balão Ctrl+E em arquivos copiados | Claude | testado no POCO: ida e volta byte a byte idênticas (cmp). Falta o usuário testar pelos menus |
| R01 | Redesign (design aprovado: https://claude.ai/artifact/3CRhhFpXTgrxsoeNu8zFtM): notch no Balão (`notch.js`, Nexo em Cairo, 6 controles, pareamento sem pegar o teclado, Super+Shift+N); app PC GTK (lateral de dispositivos, cartão com o Nexo, controles, permissões, atividade do log); app Android em Compose (boas-vindas, pareamento só com o código, início com 4 blocos e "Controlar o PC" com ações não liberadas esmaecidas). `ui.open` e `nexus send-file` para o notch | Claude | notch 32/32 no Shell aninhado (fotos conferidas); app PC renderizado e conferido; Android instalado no POCO e conferido por print (2 bugs visuais corrigidos). Notch real validado (escondido, desce pelo topo, encosta na borda com a barra embaixo). Progresso/cancelamento: `file.progress` (≤4/s), `file.done`, `file.cancel`; notch (estado 3), faixa no app GTK, notificação com barra no celular; 60 MB PC→POCO validado |

Próximas etapas e pendências: ver a seção final do `CLAUDE.md` (fonte única, atualizada em 2026-10-24).
