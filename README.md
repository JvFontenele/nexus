# Nexus

Integração entre um PC Linux (GNOME) e dispositivos Android, no estilo da "Continuidade" da Apple. Tudo é próprio, sem KDE Connect nem Zorin Connect, e o protocolo é cifrado de ponta a ponta.

- **Pareamento seguro:** código de 6 dígitos (SAS com commit/reveal), certificados por dispositivo, mTLS e revogação.
- **Celular → PC:** texto e área de transferência, arquivos pelo menu Compartilhar, blocos rápidos e ações do PC liberadas uma a uma, como tocar/pausar, bloquear a tela e notificar.
- **PC → celular:** texto, links e arquivos, e o **áudio do PC tocando no celular** (~115 ms).
- **Espelhar a tela do celular no PC** com [scrcpy](https://github.com/Genymobile/scrcpy), por USB ou por depuração sem fio (o adb é reconectado sozinho).
- **Notch no topo da tela**, dentro da extensão [Balão](https://github.com/JvFontenele/balao), com o mascote Nexo. Sem o Balão, o app Nexus do PC faz o mesmo papel.

## Peças
| Pasta | O quê |
|---|---|
| `nexus/` | `nexusd` (Python 3.12, asyncio, só stdlib + `gi`): protocolo, pareamento, ações, áudio (PipeWire), arquivos, D-Bus; app GTK; CLI |
| `android/` | app Android (Kotlin + Jetpack Compose, minSdk 29) |
| `packaging/` | serviço systemd **de usuário** + atalho no menu (`install.sh`) |
| `docs/` | ADRs 0001–0005, protocolo v1, pesquisas |
| `tests/` | `python3 -m unittest discover -s tests` |

## Instalar no PC
```bash
packaging/install.sh            # nexusd como serviço do usuário (sem root) + "Nexus" no menu
sudo apt install scrcpy adb     # opcional: espelhar a tela
```
Abra a porta 47210/tcp no firewall **só para a sua rede local e para a tailnet**. Exemplo com ufw:
`sudo ufw allow from 192.168.0.0/24 to any port 47210 proto tcp`.

## Usar
1. Instale o app no celular (`android/`, ver `CLAUDE.md` para compilar), toque em **Procurar** e depois em **Parear**.
2. Confira o código no PC (notch do Balão, app Nexus ou `nexus pair confirm <id>`) e no celular.
3. Libere no PC as ações que o celular pode executar (app Nexus → permissões).

CLI: `nexus devices list|allow|deny|revoke`, `nexus pair pending|confirm`, `nexus mirror`, `nexus app`.
