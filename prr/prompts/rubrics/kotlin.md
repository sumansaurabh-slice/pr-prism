### Kotlin (Android)

- `GlobalScope`, or coroutines not tied to a lifecycle (`viewModelScope`, `lifecycleScope`). `runBlocking` on the main thread.
- The `!!` operator on values that can really be null.
- Compose:
  - `mutableStateOf` without `remember`;
  - heavy work or side effects inside a composable instead of `LaunchedEffect`;
  - unstable lambdas causing recomposition loops.
- Tokens or personal data in plain `SharedPreferences`. Use encrypted storage.
- Work on the main thread: network, disk or database.
- Context leaks: an `Activity` or `View` kept in a singleton or a long-lived object.
- A config change or process death losing user input (no `SavedStateHandle`).
