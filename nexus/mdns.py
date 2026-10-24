"""Anúncio _nexus._tcp pelo Avahi (D-Bus do sistema). Falha não é fatal: o fallback é IP manual/QR."""
import sys

SERVICE = "_nexus._tcp"


def publish(name: str, port: int, txt: dict[str, str]):
    """Publica o serviço; devolve uma função que remove o anúncio, ou None se o Avahi não estiver disponível."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

        def proxy(path, iface):
            return Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.Avahi", path, iface, None)

        group = proxy(proxy("/", "org.freedesktop.Avahi.Server").EntryGroupNew(), "org.freedesktop.Avahi.EntryGroup")
        records = [f"{k}={v}".encode() for k, v in txt.items()]
        # interface -1 / protocol -1 = todas; flags 0; domínio e host padrão do Avahi
        group.call_sync("AddService", GLib.Variant("(iiussssqaay)", (-1, -1, 0, name, SERVICE, "", "", port, records)),
                        Gio.DBusCallFlags.NONE, -1, None)
        group.Commit()
        return group.Free
    except Exception as e:  # sem Avahi/D-Bus/gi: segue sem descoberta
        print(f"nexusd: mDNS indisponível ({type(e).__name__}: {e})", file=sys.stderr)
        return None


def browse(service: str, seconds: float = 3.0) -> list[tuple[str, int]]:
    """(endereço, porta) dos anúncios IPv4 de `service` na rede, via Avahi. Vazio se o Avahi faltar."""
    import time
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        ctx = GLib.MainContext.new()
        ctx.push_thread_default()  # roda numa thread qualquer sem disputar o loop de ninguém
        try:
            bus = Gio.DBusConnection.new_for_address_sync(
                Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None),
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
            server = Gio.DBusProxy.new_sync(bus, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.Avahi", "/",
                                            "org.freedesktop.Avahi.Server", None)
            found = []
            path = server.call_sync("ServiceBrowserNew", GLib.Variant("(iissu)", (-1, 0, service, "local", 0)),
                                    Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
            bus.signal_subscribe("org.freedesktop.Avahi", "org.freedesktop.Avahi.ServiceBrowser", "ItemNew", path, None,
                                 Gio.DBusSignalFlags.NONE, lambda *a: found.append(a[5].unpack()))
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                ctx.iteration(False)
                time.sleep(0.05)
            out = []
            for iface, proto, name, stype, domain, _flags in found:
                r = server.call_sync("ResolveService", GLib.Variant("(iisssiu)", (iface, proto, name, stype, domain, -1, 0)),
                                     Gio.DBusCallFlags.NONE, 5000, None).unpack()
                out.append((r[7], r[8]))
            bus.close_sync(None)
            return out
        finally:
            ctx.pop_thread_default()
    except Exception as e:
        print(f"nexusd: mDNS browse falhou ({type(e).__name__}: {e})", file=sys.stderr)
        return []
