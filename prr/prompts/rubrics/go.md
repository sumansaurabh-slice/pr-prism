### Go

- `SELECT … FOR UPDATE` run on the pool or `db` instead of the `sql.Tx` that later writes. The lock is lost straight away.
- `defer` inside a loop, which runs only when the function returns. `rows.Close()` or `resp.Body.Close()` missing.
- `fmt.Errorf` without `%w` loses the wrapped error for `errors.Is` and `errors.As`.
- Goroutines: `WaitGroup.Add` called inside the goroutine, a loop variable captured in old Go versions, and goroutines with no way to stop (no `ctx` cancellation).
- `http.Client{}` without a `Timeout`, or a request without a `ctx`.
- Maps written from several goroutines without a mutex. A `sync.Mutex` copied by value.
- An error compared with `==` instead of `errors.Is`. An ignored error from `Close`, `Commit` or `Write`.
- A nil interface that holds a nil pointer (`err != nil` is true).
- `context.Background()` used inside a request path, which drops the caller's deadline.
