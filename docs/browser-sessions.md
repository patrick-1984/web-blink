# Record with a browser you already use

Blink can connect to a signed-in Chrome, Edge or Brave profile and record in a new work tab. Choose your
personal or admin profile without logging out of either. Run Blink on the computer containing those
profiles: a remote workspace does not see the browsers on your laptop.

## Choose a browser and profile

Tell your agent: **“Record this using my Brave Admin profile.”** It lists the local browser/profile names,
asks for any missing choice, checks setup and starts the recording. It does not read password or cookie
databases to discover profiles. A lock is only a hint that a browser is running, not proof of which profile
is open. The connection and the service's account identity are checked when used.

On the first recording, if local profiles are detected and you have not saved a preference, the agent offers
your existing login, the normal shared browser or a separate remembered Blink login. After you choose, it
reuses that preference without asking every time. You can still name another profile for a single recording.

From the CLI:

```sh
blink browser list
blink browser list --browser brave
blink browser setup
blink browser select --browser brave --profile 'Admin'
blink browser status
blink learn start --input '{"url":"https://app.example"}'
```

`setup` installs **@playwright/mcp@0.0.84** under private local state; it does not launch a browser or
download browser binaries. Node.js and npm are prerequisites. Connection startup uses that installed
version offline, rather than fetching an unpinned package. Install the
[Playwright extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm)
in every profile you want to use. The browser may ask you to approve the connection. Choose a spare blank
tab in the extension's picker; Blink then creates its own recording tab. Keep your other work tabs private.

Names such as `Admin`, directory names such as `Profile 2`, and exact profile IDs from `browser list` are
accepted. Duplicate names require the exact ID. The choice is remembered in private settings; it stores no
cookies or extension token. `--config-dir` and the normal `--dry-run`/approval preferences apply. Browser
connection setup never changes your approval policy.

For a one-off profile, supply `browser` in `learn_start` instead of changing the default:

```json
{"url":"https://app.example","browser":{"mode":"extension","browser":"brave","profile":"<profile ID>"}}
```

The result, saved recording and proposal carry that selector. Account activation inherits it from the
recording unless an explicit selector is supplied. Personal and admin accounts need distinct binding IDs.
Changing the default later affects new recordings, not existing bindings. Previously created bindings
without a selector continue using the shared browser. A tab already belonging to a binding cannot switch
profiles underneath it: close that Blink work tab, or use another binding name.

## Other connection modes

| Mode | CLI example | Login behavior |
|---|---|---|
| Shared browser | `blink browser select --mode shared` | Uses the existing shared Playwright MCP configuration. |
| Existing Chrome/Edge without an extension | `blink browser select --mode cdp --endpoint chrome` | Enable remote debugging at `chrome://inspect/#remote-debugging` and accept the browser prompt. Edge uses `msedge` and `edge://inspect/#remote-debugging`. Requires a browser supporting this flow. |
| Local debugging endpoint | `blink browser select --mode cdp --endpoint http://127.0.0.1:9222` | Reuses the session exposed by that endpoint. Only loopback endpoints without URL credentials are accepted. Blink does not scan ports or restart your browser. |
| Separate remembered login | `blink browser select --mode managed --browser brave --name admin` | Opens a separate persistent Blink-owned profile. Sign in once here; reuse it on later runs. Use another name for personal logins. |
| Credentials only | `blink browser select --mode credentials` | Selects the service-specific custody workflow; this mode does not start a recording. Native API keys/OAuth grants and eligible isolated custody remain supported through the existing `account`/`custody` commands. |

Extension mode targets an exact discovered profile. A CDP channel/endpoint selects a browser connection,
not a proven profile identity: use extension mode when exact profile targeting matters. Blink retains its
normal account/tenant checks before service operations.

The separate remembered-login mode does **not** copy a personal profile. Two browser instances cannot
reliably share the same profile directory, and Chrome restricts the traditional debugging flags on its
normal data directory. Exporting cookies is not a general substitute: session storage, refresh rotation,
device binding and additional authentication steps can prevent browser-free reuse. Blink does not label
a live personal profile as an isolated/exclusive credential family.

## Firefox, platforms and discovery limits

Chrome, Edge, Brave and Firefox profile discovery covers standard Windows, macOS and Linux locations.
Firefox includes `profiles.ini`, absolute profile paths, new profile directories and standard Linux
Snap/Flatpak locations. A new Firefox profile's directory name is used when its display name is unavailable.
Custom Chromium data roots and portable installations may not be discovered; use an explicit endpoint.
An extension directory indicates installation only; disabled extensions or browser policy can still block
the connection. No mode silently falls back to another browser or account when connection fails.

**Stock Firefox live attachment is not implemented.** Its protocols differ from Playwright's Chromium
connection. In Firefox, open Developer Tools → Network, enable persistent logs, demonstrate the workflow,
export the HAR with the request details needed for learning, then import it:

```sh
blink learn import --input '{"path":"/absolute/path/recording.har","origins":["https://app.example"]}'
```

HAR imports have no Blink screenshots or live recording status. Keep the exported file private; it can
contain login material. The current importer creates a metadata-only proposal input and does not import
the HAR as detached credential custody.

**The recording daemon currently requires macOS or Linux.** Windows can discover profiles and save a
selection, but a native Windows recording transport remains unavailable. Running Blink inside Linux/WSL
does not automatically discover or control Windows profiles.

## Evidence and validation

The provider is the existing Playwright MCP, so Blink keeps its owned-tab recorder, account probes and
credential custody boundaries. Browser discovery and routing are covered by synthetic profile fixtures,
including ten Brave profiles, ambiguous names, stale selections, separate account routes and private
settings. Browser-specific OS/version tests need the matching local installation; discovery does not
claim a live desktop attachment was tested merely because metadata was found.

Primary implementation references (checked 2026-11-03):

- [Playwright MCP browser connections](https://playwright.dev/mcp/configuration/browser-extension)
- [Playwright extension setup, custom executable and profile selection](https://github.com/microsoft/playwright/tree/main/packages/extension)
- [Chrome's connection permission flow](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)
- [Chrome's default data-directory restriction](https://developer.chrome.com/blog/remote-debugging-port)
- [Firefox remote protocols](https://firefox-source-docs.mozilla.org/remote/)
- [Firefox profile storage changes](https://firefox-source-docs.mozilla.org/toolkit/profile/changes.html)
- [Brave's Chrome extension support](https://brave.com/learn/using-chrome-extensions-in-brave/)
