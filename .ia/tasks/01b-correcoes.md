# T01b — correções da T01

Altere SÓ estes arquivos: `nexus/catalog.py`, `nexus/actions/__init__.py`, `tests/test_cli.py`, `tests/test_catalog.py`.

## Defeitos
1. `nexus/catalog.py`, função `run`: a linha `error = type().__name__` está errada (`type()` sem argumento lança TypeError). Troque por `except Exception as e:` e `error = type(e).__name__`.
2. `nexus/catalog.py`, função `run`: o `with concurrent.futures.ThreadPoolExecutor(...)` espera a thread acabar, então o timeout não retorna na hora. Troque por:
   ```python
   executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
   try:
       future = executor.submit(action.handler, params)
       result = future.result(timeout=action.timeout)
   except concurrent.futures.TimeoutError:
       raise ActionTimeout("Action timed out")
   finally:
       executor.shutdown(wait=False)
   ```
3. `nexus/catalog.py`, função `run`: o log tem que ser gravado SEMPRE, inclusive em erro, NeedsConfirmation, PreconditionFailed e timeout. Reestruture `run` assim: `start = time.monotonic()`, `error = None`, e um único `try:` envolvendo TUDO depois de `get(action_id)` (a checagem de dangerous, a precondition e a execução). Use `except Exception as e: error = type(e).__name__; raise` e, no `finally:`, chame `log_event(dict(action=action_id, ok=error is None, error=error, duration_ms=..., params=sorted(params.keys())))`.
4. `nexus/actions/__init__.py`: remova o `if REGISTRY: return` e o import de REGISTRY. `load_all()` só faz `import nexus.actions.core`.
5. `tests/test_cli.py`, no `setUp`: chame `load_all()` (de `nexus.actions`) ANTES de `self.saved_registry = dict(REGISTRY)`.
6. `tests/test_catalog.py`: acrescente 2 testes: (a) handler que levanta `RuntimeError` → o `run` repassa `RuntimeError` e o log registra `"error": "RuntimeError"` e `"ok": false`; (b) ação `dangerous` sem confirm → o log registra `"error": "NeedsConfirmation"`. Para timeout, confirme que `run` retorna em menos de 0,5 s (handler dorme 1 s, timeout 0,1).

## Aceite
```
python3 -m unittest discover -s tests -v && python3 -m nexus run core.echo text=oi
```
