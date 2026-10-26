#!/usr/bin/env bash
# Instala o Nexus para o usuário atual (sem root): serviço systemd de usuário + atalho no menu.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p ~/.config/systemd/user ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
cp "$SRC/packaging/dev.nexus.App.svg" ~/.local/share/icons/hicolor/scalable/apps/
sed "s|@SRC@|$SRC|" "$SRC/packaging/nexusd.service" > ~/.config/systemd/user/nexusd.service
sed "s|@SRC@|$SRC|" "$SRC/packaging/dev.nexus.App.desktop" > ~/.local/share/applications/dev.nexus.App.desktop
systemctl --user daemon-reload
systemctl --user enable --now nexusd.service
echo "nexusd ativo: systemctl --user status nexusd"
for p in scrcpy adb; do
    command -v "$p" >/dev/null || echo "Opcional: '$p' não encontrado; para espelhar a tela: sudo apt install scrcpy adb"
done
