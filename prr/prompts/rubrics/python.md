### Python

- A mutable default argument (`def f(x=[])`, `={}`) is shared between calls.
- Blocking work inside `async def`: `requests`, `time.sleep`, sync DB drivers or file I/O freeze the event loop. Also look for a missing `await` on a coroutine, which silently never runs.
- `except Exception: pass` or a bare `except:` hides real failures. It also catches `KeyboardInterrupt` and `SystemExit`.
- Unsafe loading: `yaml.load` without `SafeLoader`, `pickle.loads` or `eval` on outside data.
- Time: naive `datetime.now()` mixed with timezone-aware values. Use `datetime.now(timezone.utc)` for stored times.
- ORM: missing `select_related` / `prefetch_related` (N+1). Queryset or session work done outside the transaction it needs.
- Pydantic or dataclass models: optional fields whose default is wrong, or validation skipped with `construct`.
- An `Optional` value that can be `None` and is used without a check.
- Float maths on money. Use `Decimal` with an explicit rounding mode.
