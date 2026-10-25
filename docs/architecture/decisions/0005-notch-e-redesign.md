# ADR-0005 — Notch no Balão, mascote Nexo e redesign dos apps

Data: 2026-10-24 · Status: aprovada pelo usuário (design: https://claude.ai/artifact/3CRhhFpXTgrxsoeNu8zFtM)

## Decisão
- **Notch no Balão** (`pesquisa-linux/notch.js`), escolhido pelo usuário em vez de uma extensão própria.
  - **Pílula escondida por padrão:** desce com animação quando o mouse é empurrado contra o topo (`Layout.PressureBarrier`, a mesma técnica dos cantos ativos: não rouba cliques da borda) ou pelo atalho `Super+Shift+N`. Some sozinha em ~2,5 s.
  - **Posição:** `topoLivre()` fica logo abaixo da barra do GNOME quando ela está no topo. Se a barra estiver embaixo ou de lado (Zorin/dash-to-panel), encosta na borda.
  - **Modo normal (modal):** cartão do dispositivo e 6 controles (Espelhar, Áudio, Enviar arquivo, Copiar no celular, Pesquisar no celular, Permissões).
  - **Pareamento e arquivo (não modais, não pegam o teclado):** o pareamento abre com o código e foco em Recusar; o arquivo mostra barra, velocidade e cancelar.
- **Mascote Nexo:** bloco branco arredondado com 6 expressões. A expressão é escolhida pela prioridade pareando > reação recente > áudio > conectado > dormindo.
  - **Expressões:** conectado, feito, atento, curtindo, surpreso, dormindo.
  - **Implementações:** desenhado em três lugares com as mesmas coordenadas (viewBox 96×76): Cairo/GJS (`notch.js`), Cairo/Python (`nexus/mascote.py`) e Compose (`Visual.kt`).
- **App PC (GTK4/libadwaita):** escuro, com lateral de dispositivos, cartão com o Nexo, controles, permissões e atividade lida de `events.jsonl`. Tem uma faixa de progresso de arquivo.
- **App Android (Compose):** boas-vindas, tela de pareamento só com o código, início com 4 blocos e "Controlar o PC" (ações não liberadas aparecem esmaecidas). Os ícones são desenhados em Canvas para não depender de biblioteca de ícones.
- **O Shell não abre seletor de arquivos:** `ui.open` faz o daemon abrir o app ou `nexus send-file <id>` (só ids pareados viram argumento).

## Fora / pendente
Arrastar arquivos até o notch (o Wayland não entrega DnD de apps a atores do Shell com facilidade; precisa de prova de conceito).
