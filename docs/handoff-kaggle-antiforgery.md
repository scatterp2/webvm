# Handoff: Kaggle anti-forgery token failure

## Objective and completion criteria

The anti-forgery failure is the **next step to solve**, not the final goal. The final goal is for the **real Kaggle page to visibly display inside WebVM through the Web Adjuster proxy**.

Completion is not established by a running proxy, an HTTP 200 alone, or a successful API call. The end-to-end acceptance test is that Kaggle loads in the WebVM browser and its page is visibly rendered.

## Ordered milestones

1. **Next: resolve the anti-forgery failure.** Preserve the correct browser session cookies and matching token; prevent session-dependent HTML from being reused across sessions; investigate required request headers only if needed.
2. **Verify the fix against the failing request.** Confirm Kaggle accepts the request. Do not treat a proposed patch as successful until this is observed.
3. **Final: display Kaggle in WebVM.** Navigate to Kaggle through the proxy and confirm the actual page visibly renders in the browser. If the anti-forgery error is gone but the page does not display, the task is still unfinished; continue debugging redirects, document loading, proxy rewriting, or browser errors as indicated by evidence.

## Status

A proposed fix is staged on branch `fix/kaggle-antiforgery-session` as a patch and handoff document. It is **not yet applied to the running WebVM and not verified against a live Kaggle request**. The branch is deliberately isolated from the current runtime branch.

## Where

- Target code: `examples/python3/web_adjuster/launcher.py`
- Patch artifact: `patches/kaggle-antiforgery-session.patch`
- This handoff: `docs/handoff-kaggle-antiforgery.md`

## What the proposed patch changes

1. **Do not cache HTML responses.** The current cache is keyed only by (method, URL). HTML can carry a session-bound anti-forgery token without a `Set-Cookie` header. Replaying cached HTML between sessions can pair a token from one session with another session's cookies.
2. **Bypass cache for requests carrying a Cookie header.** This prevents authenticated/session-bearing reads from reusing a URL-only cached response. The bypass must not remove or suppress cookies: the browser-provided Cookie header still needs to be forwarded upstream.
3. **Set upstream Origin on state-changing requests.** The browser sees the proxy origin, but upstream anti-forgery checks may expect the original target origin. For POST/PUT/PATCH/DELETE, send `Origin: <target origin>`; keep Referer on the target origin rather than leaking the proxy URL. Treat this as a hypothesis to verify.
4. **Do not disable anti-forgery validation or fabricate tokens.** Preserve the browser-provided session cookies and token/header/body fields; do not log cookie or token values.

## Why this is the first low-risk patch

The existing `upstream_fetch()` filters browser `Origin` and `Referer` in `PROXY_REVEAL_HEADERS`, then sets a generic upstream Referer but never restores Origin. Separately, its GET cache is keyed only by URL and can cache HTML if no `Set-Cookie` is returned. Both are plausible ways to break a session-bound CSRF flow. This patch addresses those concrete proxy behaviors without hard-coding Kaggle token names.

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
4. Confirm a state-changing request receives the expected upstream Origin and retains the browser-provided token fields and session cookie.
5. Confirm ordinary GET static assets can still use cache and no regression in WebVM proxy tests.
6. If still failing, add temporary metadata-only diagnostics (method, path, status, cookie-present boolean, anti-forgery header *names*); never log header/body values.
7. **Final acceptance test:** load Kaggle through Web Adjuster inside WebVM and verify that the actual page visibly displays. If it does not display, continue debugging; do not mark the task complete.

## Immediate next action

Review and apply the proposed patch to `launcher.py`, run tests, and test the exact failing request. Then continue through the end-to-end browser test until Kaggle visibly displays in WebVM. Do not merge or deploy based solely on this hypothesis.
