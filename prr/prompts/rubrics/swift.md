### Swift (iOS)

- Force unwrap `!`, `try!` or `as!` on values that can fail at runtime.
- Escaping closures that capture `self` strongly, causing retain cycles. Use `[weak self]`.
- UIKit or SwiftUI state changed off the main thread.
- Secrets or tokens in `UserDefaults`. Use the Keychain.
- `async` tasks not cancelled when the screen goes away. Also look for actor isolation warnings that are silenced.
- Auto Layout: constraints added twice, or conflicting priorities.
- Codable models that crash or drop data when the server adds or removes a field.
