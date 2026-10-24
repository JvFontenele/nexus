from nexus.catalog import Action, Param, register


def _echo(params: dict) -> str:
    return params["text"]


def _version(params: dict) -> str:
    import nexus
    return nexus.__version__


register(Action(
    id="core.echo",
    description="Echo text",
    handler=_echo,
    params=(Param(name="text"),),
))

register(Action(
    id="core.version",
    description="Show version",
    handler=_version,
    params=(),
))
