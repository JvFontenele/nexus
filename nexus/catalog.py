import concurrent.futures
import datetime
import re
import time
from pathlib import Path
from typing import Callable, Any

from nexus.log import log_event

REGISTRY: dict[str, Any] = {}

ACTION_ID_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$")


class ActionError(Exception):
    pass


class UnknownAction(ActionError):
    pass


class InvalidParams(ActionError):
    pass


class PreconditionFailed(ActionError):
    pass


class NeedsConfirmation(ActionError):
    pass


class ActionTimeout(ActionError):
    pass


from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    name: str
    type: str = "str"
    required: bool = True
    default: object = None
    help: str = ""


@dataclass(frozen=True)
class Action:
    id: str
    description: str
    handler: Callable[[dict], Any]
    params: tuple = ()
    precondition: Callable[[], str | None] | None = None
    dangerous: bool = False
    phone_safe: bool = False
    timeout: float = 30.0


def register(action: Action) -> Action:
    if not ACTION_ID_RE.match(action.id):
        raise ValueError(f"Invalid action id: {action.id}")
    if action.id in REGISTRY:
        raise ValueError(f"Duplicate action id: {action.id}")
    if action.phone_safe and action.dangerous:
        raise ValueError("phone_safe and dangerous cannot both be true")
    REGISTRY[action.id] = action
    return action


def get(action_id: str) -> Action:
    if action_id not in REGISTRY:
        raise UnknownAction(action_id)
    return REGISTRY[action_id]


def all_actions() -> list[Action]:
    return sorted(REGISTRY.values(), key=lambda a: a.id)


def parse_params(action: Action, pairs: list[str]) -> dict:
    expected = dict()
    for p in action.params:
        expected[p.name] = p
    used = dict()

    for item in pairs:
        if "=" not in item:
            raise InvalidParams(f"Missing = in: {item}")
        key, val = item.split("=", 1)
        if key not in expected:
            raise InvalidParams(f"Unknown param: {key}")
        if key in used:
            raise InvalidParams(f"Duplicate param: {key}")
        used[key] = val

    result = dict()
    for pname, p in expected.items():
        if pname in used:
            result[pname] = _convert(p.type, used[pname])
        elif p.required:
            raise InvalidParams(f"Missing required param: {pname}")
        else:
            result[pname] = p.default

    return result


def _convert(type_, value):
    if type_ == "str":
        return value
    if type_ == "int":
        try:
            return int(value)
        except ValueError:
            raise InvalidParams(f"Invalid int: {value}")
    if type_ == "bool":
        low = value.lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
        raise InvalidParams(f"Invalid bool: {value}")
    if type_ == "path":
        p = Path(value).expanduser()
        if not p.exists():
            raise InvalidParams(f"Path not found: {value}")
        return p
    raise InvalidParams(f"Unknown type: {type_}")


def run(action_id, params, *, confirm=False):
    start = time.monotonic()
    error = None
    try:
        action = get(action_id)

        if action.dangerous and not confirm:
            raise NeedsConfirmation(f"Action is dangerous. Use --yes to confirm.")

        if action.precondition is not None:
            reason = action.precondition()
            if reason is not None:
                raise PreconditionFailed(reason)

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(action.handler, params)
            result = future.result(timeout=action.timeout)
        except concurrent.futures.TimeoutError:
            raise ActionTimeout("Action timed out")
        finally:
            executor.shutdown(wait=False)
    except Exception as e:
        error = type(e).__name__
        raise
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        sorted_keys = sorted(params.keys())
        log_event(dict(
            action=action_id,
            ok=error is None,
            error=error,
            duration_ms=duration_ms,
            params=sorted_keys,
        ))
    return result

