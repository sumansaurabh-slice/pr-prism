### Every language

- **Access control:** when code loads a record by an id that came from a request (an order, a user, an account), check the caller is allowed to see or change it. A missing owner check lets one user read or edit another user's data.
- **Money and counts:** money must not use floating point. Look for rounding, overflow and unit mix-ups (paise vs rupees, cents vs dollars).
- **Errors:**
  - Errors must not be swallowed (`catch {}` with no handling).
  - Retries must have a limit and a backoff.
  - Cleanup (closing files and connections, releasing locks) must also run when something fails.
- **Concurrency:** check state shared between threads or requests, and check-then-act races (reading a value, then writing based on it, without a lock or an atomic update). Message handlers must be safe to run twice on the same message.
- **Database:**
  - Look for queries inside loops (N+1).
  - Look for missing indexes on new query patterns.
  - A row read "FOR UPDATE" must be locked and written on the same transaction or connection.
  - Lock several rows in the same order everywhere, so two requests can't deadlock.
- **External calls:** HTTP and RPC calls need a timeout. A failure of the other service must be handled. Don't keep a database transaction open while waiting on the network.
- **Input:** validate size and format at the edge. Build SQL, shell commands and file paths from parameters or safe APIs, never by pasting strings together.
- **Secrets and personal data:** never hard-code them, and never write them to logs, errors or analytics.
- **Contracts:** changing an API field, an event, a config key or a DB column can break callers that are not in this PR. Check that old callers still work, or that the PR says how they move.
- **Tests:** risky branches (errors, limits, permission denied, retries) need tests, not only the happy path.
- **Don't flag style.** Formatting, naming taste and lint-level issues are for linters.
