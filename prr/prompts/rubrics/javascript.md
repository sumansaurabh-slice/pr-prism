### JavaScript and TypeScript (Node and web)

- A missing `await` (fire-and-forget promises whose errors vanish), or an unhandled rejection. `forEach` with an `async` callback does not wait.
- Unbounded `Promise.all` over user-sized lists. Batch it or limit concurrency.
- Injection:
  - string-built SQL;
  - a Mongo query object built from request JSON (`$where`, `$ne` operator injection);
  - `child_process.exec` with input.
- `express.json()` / body parsers without a size limit. CORS set to `*` with credentials.
- XSS: `dangerouslySetInnerHTML`, `innerHTML` or `v-html` with data that isn't sanitised.
- React:
  - a missing dependency in `useEffect` / `useMemo` / `useCallback`;
  - state updated after unmount;
  - `key` set to the array index on lists that change order.
- TypeScript: `any` or `as` casts that hide a real type mismatch. Non-null `!` on values that can really be null.
- Dates and time zones: `new Date(string)` parsing, and local-time maths on server code.
- Secrets or tokens kept in `localStorage`, or shipped in the frontend bundle.
