# ADR-0006 — Entrada compartilhada: adb/UHID e Bluetooth LE HOGP

Data: 2026-10-26 · Status: aprovada pelo usuário (modo híbrido) · PC↔PC na consulta #013

## Contexto
O teclado e o mouse do PC atravessam a borda da tela para o celular ou o tablet (`nexus/kvm.py`): o GNOME captura
pelo portal InputCapture (`nexus/capture.py`) e o Nexus injeta no aparelho. Sem root, há dois caminhos para o Android
ver um teclado e um mouse **de verdade** (cursor e layout nativos):

| | adb + scrcpy-server (UHID) | Bluetooth LE HOGP (`nexus/bthid.py`) |
|---|---|---|
| Pré-requisito | depuração USB ou sem fio | só o bond Bluetooth |
| Vários aparelhos | sim, um servidor por aparelho | **não**: o BlueZ manda cada relatório a todos os inscritos |
| Tela e aceleração | lidas pelo `dumpsys` (rotação a cada 2 s) | as salvas da última vez com adb, ou o padrão AOSP |
| Clipboard, aviso de clique automático | sim | não |
| Pontos fracos | o HyperOS desliga a depuração sem fio ao trocar de rede | reiniciar a aplicação GATT com o aparelho conectado quebra as notificações; no tablet (One UI 8) a entrada HID não abriu |

## Decisão
- **Híbrido, por aparelho:** `transport` no `trusted.json` = `"adb"` (padrão) ou `"bluetooth"`, escolhido no app.
  **No máximo um** aparelho usa Bluetooth (validado no `input.config`).
- O periférico BLE vive no nexusd: a aplicação GATT é registrada **uma vez** (thread GLib própria) quando existe um
  aparelho Bluetooth e nunca é desregistrada enquanto o daemon roda. Desativar a borda só para de mandar relatórios.
- **Pareamento só pedido pelo usuário** ("Parear por Bluetooth" no app): por 2 min, Pairable, Agent1 (só comparação
  numérica, sempre confirmada no app) e anúncio descobrível. Fora disso: sem agente e sem Pairable; só um anúncio de
  reconexão **não descobrível** enquanto o aparelho com bond está desconectado (sem anúncio o Android não reconecta).
  O endereço do bond fica no registro (`bt_address`).
- A barreira de um aparelho Bluetooth só existe enquanto `Device1.Connected` é verdadeiro.
- Teclas de mídia: relatório Consumer Control (id 3) no Report Map, com as mesmas teclas da tabela `MEDIA` do adb.
- Os dois caminhos são o mesmo tipo de alvo para o `Mux`/`Travessia`: um sink com move/button/key/wheel/release_all/close.

## Riscos e pendências
- Latência do Bluetooth não medida.
- Outro aparelho que já tenha bond com o PC e se inscreva no serviço HID também recebe a entrada; o Nexus não tem como
  escolher sem root (`AcquireNotify` por dispositivo fica para depois).
- Enquanto a janela de pareamento está aberta, o agente do Nexus é o padrão do BlueZ (o do GNOME volta depois).
- Se o aparelho Bluetooth é o único alvo e desconecta, a borda inteira acaba (a Capture precisa de ao menos uma barreira).
