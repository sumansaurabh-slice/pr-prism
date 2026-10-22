### Java and Kotlin (JVM services)

- `@Transactional` on a private method, or called from the same class (self-invocation), does nothing because no proxy is involved. Also look for a wrong propagation or isolation level for the job.
- Money stored as `double` or `float`, or `BigDecimal` compared with `equals`, which also compares scale. Use `compareTo` and an explicit `RoundingMode`.
- JPA:
  - lazy loading outside the transaction;
  - N+1 on collections;
  - Lombok `@Data` or `equals`/`hashCode` on entities;
  - a missing `@Version` where two writers can race.
- `@Async` or an executor without a bounded pool. Thread-locals (MDC, security context) lost across threads.
- Entity loaded by id from a request with no owner or permission check. Return 404, not 403, so ids can't be probed.
- `Optional.get()` without a check. Nulls returned from Kotlin platform types.
- `synchronized` on a shared or interned object. A double-checked lock without `volatile`.
- Exceptions caught and logged, then the method carries on with half-done work.
- Resources not closed: use try-with-resources or Kotlin `use`.
