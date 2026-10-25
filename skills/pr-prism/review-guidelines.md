# My review guidelines

Edit this file freely. The `/pr-prism` skill injects it into every analysis prompt, so Claude weighs these when it rates risk, writes notes and drafts suggested comments.

## Always check

- **Correctness at boundaries:** check nil/None/empty inputs, off-by-one errors, timezones, encodings, and overflow on money or counters.
- **Error handling:** errors are not swallowed, retries are bounded, and cleanup runs on failure paths.
- **Concurrency:** check shared state, locks, idempotency of handlers and consumers, and ordering assumptions.
- **Data:**
  - migrations are reversible and safe to run on a large table while it is live;
  - backfills are batched;
  - no unexpected change to a wire or storage format.
- **Security:**
  - authn/authz is enforced at the right layer;
  - inputs are validated;
  - no secrets or PII appear in logs;
  - queries are parameterized.
- **API contracts:** changes are backwards compatible, or the break is called out with a migration path for callers.
- **Observability:** new failure modes are logged or metered at the right level.
- **Tests:** tests cover the risky branches, not just the happy path, and assert behavior, not implementation.

## Style preferences

- Prefer small, explicit functions over clever abstractions.
- Do not flag formatting or lint-level issues. CI handles those.

## Team context

<!-- Add domain context Claude should know: e.g. "Payments code must be idempotent", "We deploy DB migrations before app code". -->
