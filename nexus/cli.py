import argparse
import json
import sys

from nexus.catalog import REGISTRY, get, all_actions, parse_params, run
from nexus.actions import load_all
from nexus.catalog import (
    UnknownAction,
    InvalidParams,
    PreconditionFailed,
    NeedsConfirmation,
    ActionTimeout,
)


def main(argv=None):
    load_all()
    parser = argparse.ArgumentParser(prog="nexus")
    sub = parser.add_subparsers(dest="command")

    act = sub.add_parser("actions")
    act.add_argument("--json", action="store_true")

    run_ = sub.add_parser("run")
    run_.add_argument("id")
    run_.add_argument("pairs", nargs="*", default=[])
    run_.add_argument("--yes", action="store_true")

    dmn = sub.add_parser("daemon")
    dmn.add_argument("--listen", help="IPs separados por vírgula (padrão: LAN + tailnet)")
    dmn.add_argument("--port", type=int, default=47210)

    sub.add_parser("app")
    sub.add_parser("mirror").add_argument("device_id", nargs="?", help="prefixo; padrão: o primeiro conectado")

    pair = sub.add_parser("pair").add_subparsers(dest="pair_cmd", required=True)
    pair.add_parser("pending")
    for name in ("confirm", "reject"):
        pc = pair.add_parser(name)
        pc.add_argument("pid")
        pc.add_argument("--yes", action="store_true")

    dev = sub.add_parser("devices").add_subparsers(dest="dev_cmd", required=True)
    dev.add_parser("list")
    dev.add_parser("revoke").add_argument("device_id")
    al = dev.add_parser("allow")
    al.add_argument("device_id")
    al.add_argument("action")

    args = parser.parse_args(argv)

    if args.command == "actions":
        return _cmd_actions(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "daemon":
        from nexus.daemon import serve
        return serve(args.listen.split(",") if args.listen else None, args.port)
    if args.command == "app":
        from nexus.app import main as app_main
        return app_main()
    if args.command == "mirror":
        return _cmd_mirror(args)
    if args.command == "pair":
        return _cmd_pair(args)
    if args.command == "devices":
        return _cmd_devices(args)

    parser.print_help()
    return 2


def _cmd_actions(args):
    if args.json:
        items = []
        for a in all_actions():
            items.append(dict(
                id=a.id,
                description=a.description,
                dangerous=a.dangerous,
                phone_safe=a.phone_safe,
                params=[
                    dict(
                        name=p.name,
                        type=p.type,
                        required=p.required,
                        default=p.default,
                        help=p.help,
                    )
                    for p in a.params
                ],
            ))
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    for a in all_actions():
        print(f"{a.id}  {a.description}")
        for p in a.params:
            opt = "?" if not p.required else ""
            print(f"    {p.name}:{p.type}{opt}")
    return 0


def _cmd_run(args):
    try:
        action = get(args.id)
    except UnknownAction:
        print(f"nexus: Unknown action: {args.id}", file=sys.stderr)
        return 2

    try:
        params = parse_params(action, args.pairs)
    except InvalidParams as e:
        print(f"nexus: {e}", file=sys.stderr)
        return 2

    try:
        result = run(args.id, params, confirm=args.yes)
    except NeedsConfirmation as e:
        print(f"nexus: {e}", file=sys.stderr)
        return 4
    except PreconditionFailed as e:
        print(f"nexus: {e}", file=sys.stderr)
        return 3
    except ActionTimeout as e:
        print(f"nexus: {e}", file=sys.stderr)
        return 124
    except InvalidParams as e:
        print(f"nexus: {e}", file=sys.stderr)
        return 2
    except UnknownAction as e:
        print(f"nexus: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"nexus: {e}", file=sys.stderr)
        return 1

    if result is None:
        return 0
    if isinstance(result, str):
        print(result)
        return 0
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0



def _control(req):
    from nexus.daemon import control
    try:
        return control(req)
    except OSError:
        print("nexus: nexusd não está rodando (inicie com: nexus daemon)", file=sys.stderr)
        return None


def _print_resp(resp):
    if resp is None:
        return 1
    if not resp.get("ok"):
        print(f"nexus: {resp.get('error', 'falhou')}", file=sys.stderr)
        return 1
    return 0


def _cmd_pair(args):
    resp = _control({"cmd": "pair.pending"})
    if resp is None:
        return 1
    if args.pair_cmd == "pending":
        for p in resp["pending"]:
            print(f"{p['pid']}  {p['name']} ({p['kind']})  código {p['code']}  fp {p['fingerprint'][:16]}…  {p['expires_in']}s")
        return 0
    match = [p for p in resp["pending"] if p["pid"] == args.pid]
    if not match:
        print("nexus: pareamento não encontrado ou expirado", file=sys.stderr)
        return 2
    p = match[0]
    accept = args.pair_cmd == "confirm"
    if accept:
        # o usuário compara o código e o fingerprint com o que o telefone mostra
        print(f"{p['name']} ({p['kind']})\nfingerprint {p['fingerprint']}\ncódigo {p['code']}")
        if not args.yes and input("O código é o mesmo do telefone? [s/N] ").strip().lower() not in ("s", "sim", "y"):
            accept = False
    return _print_resp(_control({"cmd": "pair.confirm", "pid": args.pid, "accept": accept}))


def _cmd_devices(args):
    if args.dev_cmd == "list":
        resp = _control({"cmd": "devices.list"})
        if resp is None:
            return 1
        for d in resp["devices"]:
            on = "conectado" if d["device_id"] in resp["connected"] else "-"
            print(f"{d['device_id'][:16]}…  {d['name']} ({d['kind']})  {on}  permissões: {', '.join(d['permissions']) or 'nenhuma'}")
        return 0
    if args.dev_cmd == "revoke":
        return _print_resp(_control({"cmd": "devices.revoke", "device_id": _full_id(args.device_id)}))
    return _print_resp(_control({"cmd": "devices.allow", "device_id": _full_id(args.device_id), "action": args.action}))


def _cmd_mirror(args):
    resp = _control({"cmd": "devices.list"})
    if resp is None:
        return 1
    ids = [d["device_id"] for d in resp["devices"]]
    if args.device_id:
        ids = [i for i in ids if i.startswith(args.device_id)]
    else:  # padrão: um conectado (o IP dele permite achar o adb sem fio)
        ids = sorted(ids, key=lambda i: i not in resp["connected"])
    if not ids:
        print("nexus: nenhum dispositivo pareado", file=sys.stderr)
        return 2
    return _print_resp(_control({"cmd": "mirror", "device_id": ids[0]}))


def _full_id(prefix):
    """Aceita o prefixo mostrado em `devices list`, desde que seja único."""
    resp = _control({"cmd": "devices.list"}) or {"devices": []}
    ids = [d["device_id"] for d in resp["devices"] if d["device_id"].startswith(prefix.rstrip("…"))]
    return ids[0] if len(ids) == 1 else prefix
