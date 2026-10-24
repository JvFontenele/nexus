# ADR-0004 — Áudio do PC no dispositivo (A04 v1)

Data: 2026-10-24 · Status: aprovada (consulta #011) · Ordem aprovada na #010

## Decisão
- **Captura no PC:** o nexusd cria uma saída virtual do PipeWire (`support.null-audio-sink`, `node.name=nexus-audio`, "Nexus → <dispositivo>"),
  torna-a a saída padrão (`wpctl set-default`) e captura o monitor dela com `pw-cat --record` (s16, 48 kHz, estéreo, blocos de 10 ms).
  Ao parar: volta a saída anterior e destrói o nó. Restos de um nexusd que caiu são removidos na partida.
  Experimento (2026-10-24): 380 928 bytes em 2 s = 48 000 × 4 B/s, fluxo contínuo mesmo em silêncio.
- **Transporte:** uma **segunda conexão TLS na mesma porta 47210** com mTLS (só dispositivo pareado). A primeira mensagem
  é `audio.attach` em vez de `hello`. O servidor responde `audio.ready{rate, channels, format}` e passa a mandar frames binários
  (4 bytes de tamanho + PCM). Ela não conta como "a conexão" do dispositivo, então a persistente continua de pé. Não abre porta nova no ufw.
  Confidencialidade: o áudio vai cifrado (TLS), diferente da PoC RTP.
- **Controle:** `audio.start`/`audio.stop` (PC → dispositivo pela persistente) pedem ao celular que abra ou feche o fluxo.
  O celular também pode abrir e fechar sozinho (botão no app).
- **Formato:** PCM L16 (≈1,5 Mbit/s), suficiente na LAN. Opus só se a tailnet/4G exigir (MediaCodec no Android, GStreamer no PC; nenhuma dependência nova agora).
- **Latência:** o celular usa AudioTrack `PERFORMANCE_MODE_LOW_LATENCY`. Se a fila de rede passar de 80 ms, descarta frames para alcançar o tempo real.
  Latência estimada = fila de rede + buffer do AudioTrack (via `getTimestamp`) + 10 ms de captura. Ela aparece na notificação.
- **Fora da v1:** áudio do celular no PC, microfone, Opus, sincronia com o vídeo do scrcpy (o scrcpy já tem áudio próprio do celular → PC).

## Resultado e riscos (consulta #011)
- Validado no POCO: som limpo e sincronizado, latência estimada de 115 ms com o buffer mínimo do AudioTrack (era 195 ms com 2×), 0 underruns.
- Testar ainda: sessões de 30–60 min, queda de Wi-Fi no meio, `pw-cat` morrendo (hoje: fim do fluxo + rollback da saída), bloquear/desbloquear o celular, latência pela tailnet.
- Opus só se a tailnet mostrar banda, instabilidade ou latência ruins.
