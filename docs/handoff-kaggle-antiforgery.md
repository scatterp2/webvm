# Handoff: Kaggle anti-forgery token failure

## Status
A proposed fix is staged on branch `fix/kaggle-antiforgery-session` as a patch and handoff document. It is **not yet applied to the running WebVM and not verified against a live Kaggle request**. The branch is deliberately isolated from the current runtime branch.

## Where
- Target code: `examples/python3/web_adjuster/launcher.py`
- Patch artifact: `patches/kaggle-antiforgery-session.patch`
- This handoff: `docs/handoff-kaggle-antiforgery.md`

## What the patch changes
1. **Do not cache HTML responses.** The current cache is keyed only by (method, URL). HTML can carry a session-bound anti-forgery token without a `Set-Cookie` header. Replaying cached HTML between sessions can pair a token from one session with another session's cookies.
2. **Bypass cache for requests carrying a Cookie header.** This prevents authenticated/session-bearing reads from reusing a URL-only cached response.
3. **Set upstream Origin on state-changing requests.** The browser sees the proxy origin, but upstream anti-forgery checks may expect the original target origin. For POST/PUT/PATCH/DELETE, send `Origin: <target origin>`; keep Referer on the target origin rather than leaking the proxy URL.
4. **Do not disable anti-forgery validation or fabricate tokens.** Cookies and token headers/body fields are forwarded as supplied by the browser; this patch does not log token or cookie values.

## Why this is the first low-risk patch
The existing `upstream_fetch()` filters browser `Origin` and `Referer` in `PROXY_REVEAL_HEADERS`, then sets a generic upstream Referer but never restores Origin. Separately, its GET cache is keyed only by URL and can cache HTML if no `Set-Cookie` is returned. Both are plausible ways to break a session-bound CSRF flow. This patch addresses those two concrete proxy behaviors without hard-coding Kaggle token names.

## Important limitation
This is a hypothesis-driven first patch, not proof of the exact failure. If Kaggle still says “An anti-forgery token…”, inspect one failing request in DevTools and compare **header names/status only**:
- HTTP method and target path
- response status and a short redacted error message
- whether a Cookie header was sent (never copy its value)
- names of anti-forgery headers, if any (never copy values)
- whether the preceding HTML response set cookies (names only)
- whether the request is POST/PUT/PATCH/DELETE

If cookies and token are both sent but rejected, verify they originate from the same browser session and inspect the upstream response for a rotated token. Do not share cookie/token values.

## Validation plan
1. Run existing proxy tests and Python syntax checks.
2. Clear the browser's site data for the proxy origin once, reload the proxied Kaggle page, and reproduce from a fresh session.
3. Confirm the initial HTML response is not served from cache.
4. Confirm a state-changing request receives the expected upstream Origin and retains the browser-provided token header/body and session cookie.
5. Confirm ordinary GET static assets can still use cache and no regression in the WebVM proxy tests.
6. If still failing, add temporary metadata-only diagnostics (method, path, status, cookie-present boolean, anti-forgery header *names*); never log header/body values.

## Suggested next step
Review the patch, apply it to `launcher.py`, run tests, then test against the exact failing request. Do not merge or deploy based solely on this hypothesis.
