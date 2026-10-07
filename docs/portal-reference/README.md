# Portal HTML reference

Snapshots of the captive-portal shell page for future debugging.

## What is here

- `PortalMain-2026-10-07.html` — decoded `view-source:https://172.22.2.6/connect/PortalMain`
  (Cisco ISE-style portal shell, 132 KB, 4374 lines).

## Key finding (2026-10-07)

The `PortalMain` document is only an **empty shell**: it contains **zero**
`<input>` and **zero** `<button>` elements. The real login form is injected
later via AJAX (`viewManager.gotoNextState()` → `GetViewData` →
`Authentication`/`Challenge` view into `#LoginSequencePage_Content` /
`#portal_main_view`), and the RSA state arrives via a separate `RSASettings`
request (`window.cpRSAobj`).

Consequences for the auto-login script:

1. Do not expect the username/password fields immediately after `load` — poll
   for them (the script already polls up to `WIFI_LOGIN_TIMEOUT`).
2. The submit control is rendered inside `#usercheck_ok_div` /
   `#LoginSequencePage_Content`, so selectors must cover those containers, not
   just top-level `input[type=submit]`.
3. `oAuthentication.submitActiveForm()` only exists after the AJAX view
   renders — the script must wait for it and keep the CSS-click / Enter-key /
   JS-form-submit fallbacks.
4. Success may arrive without navigation (AJAX view swap to `Final`, or
   `cpRSAobj.isAuthenticated === true`), so URL-change alone is not enough.

## Refreshing the snapshot

1. While on campus WiFi, open `https://172.22.2.6/connect/PortalMain`.
2. Save `view-source:` of the page (this keeps the AJAX shell).
3. Additionally, after the login form appears, use DevTools → Elements →
   right-click `<html>` → Copy → Outer HTML, and save that as
   `PortalMain-form-YYYY-MM-DD.html` — that file *does* contain the real
   `<input>` IDs the selectors must match.
4. Note any changed input IDs / button text in the commit message.
