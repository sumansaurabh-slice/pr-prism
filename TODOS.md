# TODOS

## CI for PR Prism itself
Deferred in the 2026-10-24 CEO review (D11).

Add `.github/workflows/ci.yml` to run on every PR and on pushes to `main`. It should do three things:
- run `pytest` on Python 3.12 and 3.14;
- type-check and build the web app (`npm ci`, then `npm run build` in `web/`);
- never call Claude or write to GitHub, with `PRR_GITHUB_DRY_RUN=1` and no `claude` on PATH.

Show the status on PRs, but don't make it a required check.

Why later: tests run locally before each PR today, and the repo is public, so CI would be free.

Pick it up when either of these happens:
- the plugin install (E6) ships and other people depend on `main`;
- a merged PR breaks the build.

Once CI exists, the plugin's prebuilt web app can be built in CI instead of by the local release script.

## DESIGN.md for the review app
Proposed in the 2026-10-25 design review.

- **What:** write `DESIGN.md` for the web app. It should cover:
  - the Primer-like parts in `web/src/styles.css` (`Box`, `Label`, `flash`, `btn`, toast) and when to use each;
  - the rules the design review set: one primary button per view, every status icon has text next to it, and `flash` warn is only for an outside author and an over-limit cost.
- **Why:** there's no written design system, so each new screen decides these again. The design review had to calibrate against the CSS file directly.
- **Pros:** later reviews and the Wave 3 and 4 screens check against one source, with fewer one-off styles.
- **Cons:** about an hour of writing, and it has to stay in step with `styles.css`.
- **Context:** the design review mapped every new piece to an existing part, and needed new CSS only for the sticky action bar and the re-place banner. `/design-consultation` can draft the file.
- **Depends on:** nothing. Best done before Wave 3, which adds the most UI.
