# Repositórios de referência

Projetos externos que estudamos para trazer funcionalidades ao Nexus. Para atualizar: compare o commit anotado aqui com o atual (`git log <commit>..HEAD`) e leia o CHANGELOG. O código deles é material de estudo e nunca é executado aqui.

| Projeto | Repositório | Estudado em | Licença | O que levamos |
|---|---|---|---|---|
| Beamer | https://github.com/kalkman-code/beamer | `550386e` (2026-10-24, v1.5.13) | GPL-3.0 | Um teclado e um mouse para várias máquinas: atravessar pela borda da tela ou por tecla de atalho, clipboard na travessia, efeitos de borda e de chegada. No Nexus, o alvo inclui o celular. |
| Coucou | https://github.com/louis-cfm/coucou | `47d1c2d` (2026-10-23, Linux 0.5.3) | MIT no código. O nome Coucou, o Mochi e os sons são © Louis Raillé (`LICENSE-ASSETS.md`). | Funções de notch disponíveis no Linux (app Tauri em `windows/`, que também gera o build Linux; `linux/` só tem o PKGBUILD; as exclusivas do Mac ficam de fora) e a forma "líquida" do notch: como ele se funde ao topo da tela e as animações de mola. |
| Zorin Connect Android | APK 1.35.13 | ver `zorin-connect-android.md` | GPL (fork do KDE Connect) | Padrões de Device Controls, tiles e compartilhamento. |

## Cuidados de licença
- **Beamer (GPL-3.0):** o Nexus é código aberto, então pode reaproveitar código do Beamer quando for preciso. Nesse caso, o arquivo copiado mantém o cabeçalho de copyright e a licença GPL-3.0, o que já é compatível: o Nexus é GPL-3.0 (`LICENSE`).
- **Coucou:** copiar trechos de código exige manter o aviso MIT. Não usar o Mochi, o nome nem os sons; o mascote do Nexus é o Nexo.
