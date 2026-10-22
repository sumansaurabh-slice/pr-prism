### Dart and Flutter

- Using `BuildContext` after an `await` without checking `mounted`.
- Heavy work, network calls or object creation inside `build()`.
- Controllers, streams or subscriptions not disposed.
- A BLoC or other screen state registered as an app-wide singleton, so state leaks between screens.
- `setState` after `dispose`. Futures created in `build` and re-run on every rebuild.
- Platform channels or plugins not handled on web or desktop when the app targets them.
