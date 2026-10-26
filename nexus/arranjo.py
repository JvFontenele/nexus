"""Arranjo da travessia: onde o dispositivo fica em relação aos monitores (lado, monitor e trecho da borda).
Puro (sem GTK), usado pelo editor do app. Os monitores vêm em coordenadas lógicas, as mesmas do GetZones."""
from nexus.capture import EDGES, _outer

PHONE_ASPECT = 1080 / 2400  # largura/altura do celular em retrato (padrão até o aparelho dizer o tamanho dele)
PHONE_SCALE = 0.45          # altura desenhada do celular, em fração da altura do menor monitor


def aspect_of(screen) -> float:
    """Largura/altura da tela do dispositivo (tamanho lógico atual, já rotacionado); padrão: celular em retrato."""
    if isinstance(screen, (list, tuple)) and len(screen) == 2 and all(isinstance(n, int) and n > 0 for n in screen):
        return screen[0] / screen[1]
    return PHONE_ASPECT


def phone_size(monitors, aspect=PHONE_ASPECT):
    h = PHONE_SCALE * min(m[3] for m in monitors)
    if aspect > 1:  # paisagem: o lado maior (a largura) fica com o tamanho de referência
        return h, h / aspect
    return h * aspect, h


def _place(stretch, extent, center_along):
    """Começo do retângulo ao longo da borda: centrado no ponteiro, preso dentro do trecho externo."""
    a, b = stretch
    start = center_along - extent / 2
    return max(a, min(start, b - extent)) if b - a >= extent else a


def _free(stretches, mon, edge, taken):
    """Tira dos trechos externos as partes já ocupadas por outros dispositivos (sem sobreposição)."""
    first, length = (mon[1], mon[3]) if edge in ("left", "right") else (mon[0], mon[2])
    for t_edge, t_mon, t_start, t_end in taken:
        if t_edge != edge or list(t_mon) != list(mon):
            continue
        low, high = first + t_start * length, first + t_end * length
        cut = []
        for a, b in stretches:
            if high <= a or low >= b:
                cut.append((a, b))
                continue
            if a < low:
                cut.append((a, low))
            if high < b:
                cut.append((high, b))
        stretches = cut
    return [(a, b) for a, b in stretches if b - a >= 0.05 * length]


def snap(monitors, center, size, taken=()):
    """Encaixa o retângulo do dispositivo (centro, (w, h)) na borda externa livre mais próxima.
    `taken` = [(borda, monitor, start, end)] dos outros dispositivos. Devolve (arranjo, retângulo)
    com arranjo = {"edge", "monitor", "start", "end"}, ou (None, None) se não houver borda livre."""
    w, h = size
    cx, cy = center
    best = None
    for mon in monitors:
        mx, my, mw, mh = mon
        for edge in EDGES:
            vertical = edge in ("left", "right")
            extent = h if vertical else w
            for stretch in _free(_outer(monitors, mon, edge), mon, edge, taken):
                along = _place(stretch, extent, cy if vertical else cx)
                if edge == "left":
                    rect = (mx - w, along, w, h)
                elif edge == "right":
                    rect = (mx + mw, along, w, h)
                elif edge == "top":
                    rect = (along, my - h, w, h)
                else:
                    rect = (along, my + mh, w, h)
                dist = (rect[0] + w / 2 - cx) ** 2 + (rect[1] + h / 2 - cy) ** 2
                if best is None or dist < best[0]:
                    first, length = (my, mh) if vertical else (mx, mw)
                    lo, hi = max(along, stretch[0]), min(along + extent, stretch[1])
                    arr = {"edge": edge, "monitor": list(mon),
                           "start": round((lo - first) / length, 4), "end": round((hi - first) / length, 4)}
                    best = (dist, arr, rect)
    return (best[1], best[2]) if best else (None, None)


def rect_of(arr, monitors, size):
    """Retângulo do dispositivo para um arranjo salvo (None se o monitor não existe mais)."""
    mon = next((m for m in monitors if list(m) == list(arr["monitor"])), None)
    if mon is None:
        return None
    mx, my, mw, mh = mon
    w, h = size
    edge = arr["edge"]
    if edge in ("left", "right"):
        y = my + arr["start"] * mh
        return (mx - w if edge == "left" else mx + mw, y, w, h)
    x = mx + arr["start"] * mw
    return (x, my - h if edge == "top" else my + mh, w, h)
