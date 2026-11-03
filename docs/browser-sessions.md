# Browser connections and profile choice

Tell Blink which browser/profile to use, or choose a numbered row. You do not need to run setup commands,
copy profile paths or edit configuration. For example: “Record using my Edge work profile.”

Blink shows the actual discovery result as a table:

| ID | Browser | Profile |
|---|---|---|
| #1 | Chrome | Personal |
| #2 | Edge | Alex (Work) |
| #6 | Brave | Administrator |

“Use #6” selects that exact profile. Numbers are stored locally and retained after a display-name change;
a deleted number is not reused for a new account. Clearing the index manually also clears those assignments.
A remembered selection is reused without asking on every recording.

## Names and the local index

Directory names such as `Profile 2` differ from the browser's visible label. Blink reads Chromium's
`Local State` plus whitelisted profile/account label fields in `Preferences` and `Secure Preferences`.
It collects display-name aliases, including composed account/local names and punctuation/case variants.
Firefox names come from `profiles.ini` and the newer `Profile Groups/*.sqlite` Profiles table, opened read-only.
Only names, aliases, paths, capabilities and file timestamps are indexed; cookies, passwords, tokens and
entire preference documents are not persisted in the index.

The private index is `~/.web-blink/cache/browser-profiles.json` (`BLINK_HOME` selects another home).
It is shared between Windows's Node-only discovery and the Python runtime. A changed metadata file causes
an immediate refresh; entries are also refreshed after five minutes. A missing name triggers one fresh
scan before Blink asks you to choose by number. It does not repeatedly ask you for `edge://version` paths.
`blink_admin` with `action: browsers` returns `profileRows`, `profileTable` and the index location/status.
`refresh: true` refreshes immediately. A cache write failure preserves discovery as an in-memory result.

## Connection choices

| Choice | Behavior |
|---|---|
| Existing live profile (`native`) | PW connects to the existing browser through its native interface. Blink verifies the selected profile before visiting the service. It can open that profile's native permission page; you approve access in the browser. |
| Launch this existing profile through PW (`profile`) | Uses the selected real data root, directory and installed binary. It retains that profile's logins. It creates no substitute profile and copies no credentials. A busy shared profile needs live attachment or the optional extension; Blink does not restart its shared main process. |
| Optional PW extension (`extension`) | Uses the extension in that exact browser profile. Blink opens its installation page when chosen; you confirm Add/Enable. This is optional, not a mandatory default. |
| A new browser launched by PW (`playwright`) | Blink obtains Chromium, Firefox or WebKit and keeps a separate persistent profile named e.g. work/admin. This genuinely new profile needs its initial login, which subsequent uses retain. |
| Shared browser (`shared`) | Uses the already configured shared Playwright provider. |
| Credentials only (`credentials`) | Uses supported native API-key/OAuth or explicitly eligible isolated custody. Direct reuse depends on the service; a generic personal-profile cookie decryption/export adapter is not provided. |

Multiple browser connections can run at the same time. Different account bindings keep their own selector.
A different default affects future recordings, not an existing account binding. Changing a display label
or switching connection methods must never silently substitute another profile/account.

PW can launch several independent browser instances with distinct data roots. A second command against a
live Chromium profile usually forwards to its existing instance; it does not create an independent writer
to the same files or automatically enable debugging. Blink does not remove profile locks or copy active
personal profiles to sidestep this restriction.

## Preparation and confirmations

On a user-ordered connection/recording, Blink prepares missing local components itself. It can obtain a
pinned, SHA-256-verified uv and download Python/libraries into its own runtime folder. The prepared cache is
also used for ordinary offline startup, rather than requiring access to a blocked global uv cache. Healthy
existing runtimes continue working. Browser tools and a selected PW engine are prepared only when needed.
No preparation runs from a session/lifecycle hook or an unattended job.

The host's tool permissions and the browser's own consent remain in force. A preparation confirmation means
“download components for Blink on this computer”; it does not grant access to browser logins. Blink never
changes Codex's sandbox or permission settings. Its approval preference is still controlled by the user.

Shared-profile restart is blocked. Chromium can host windows from multiple profiles in one main process,
so matching its user-data root or PID does not prove it is safe to close just one profile. Blink refuses
before any close/signal action. It does not retry a stale restart. This fixes the unsafe 0.10.0 path, which
could close other profiles and then stop before reopening when the browser changed its background process.

The narrow `blink_connect_browser` tool tries safe live connection and, where possible, cold PW launch of
the same profile without closing anything. `blink_restore_browser` is a recovery action: it asks the normal
browser to reopen the selected profile and restore its saved session, closes nothing and reports that the
restored tabs have not been verified. It cannot promise recovery of every previously closed tab.

The host may separately request permission to run an MCP tool. This belongs to Codex/Claude and cannot be
disabled by the plugin. A user-controlled Always allow choice can be scoped to the connection-only tool,
without authorizing the broader account/write action tool. Mutating tools remain truthfully annotated.

An existing profile launched through PW has a dedicated PW host; the MCP client attaches to it. Disconnecting
Blink's own work tabs does not deliberately close that profile's other windows. Closing the browser itself
ends its host. The selected profile path is checked in a new owned tab before service navigation.

## Real platform/browser limits

Chrome protects the normal data root from direct remote-debugging startup. Its native connection requires
the browser's opt-in/permission flow; the optional extension is another supported route. Blink opens the
correct consent page rather than dumping configuration instructions. Edge/Brave direct profile launch is
attempted only through the selected binary/root and is subject to browser version and enterprise policy.
A refusal produces concrete alternatives (`nextActions`), never a silent switch to another account.

Stock Firefox speaks BiDi, while Playwright uses its patched Firefox build. Existing stock Firefox profile
names are indexed, but this adapter does not live-control them through PW. A separate persistent PW Firefox
browser and the existing metadata-only HAR import remain available. Disk access also does not guarantee a
service's auth can be reused outside its browser: encryption, rotation and binding must be checked per service.

Windows uses authenticated local named pipes. Profile discovery there needs Node only (Node 22+ reads the
new Firefox SQLite metadata). macOS/Linux use Unix sockets. Discovery and attachment happen on the computer
running Blink; a cloud workspace cannot see a laptop's profiles automatically.

## Advanced CLI

These commands are optional interfaces, not setup tasks for an agent to hand to the user:

```sh
blink browser list
blink browser list --refresh
blink browser select --mode native --profile '#6'
blink browser select --mode profile --profile '#6'
blink browser select --mode playwright --browser firefox --name work
blink browser status
```

Prefer `blink_connect_browser` for ordinary connection and `blink_restore_browser` for recovery. The legacy
`browser_restart` action is refused before it can close windows. Local permission/extension-page actions
remain available through `blink_request_approval`. `runtime_setup` performs explicit
native preparation when needed. Tool results provide the actual state and next actions. They do not claim
an authenticated account merely because a profile exists.

Primary references checked 2026-11-03:

- [Chromium profile display-name composition](https://github.com/chromium/chromium/blob/main/chrome/browser/profiles/profile_attributes_entry.cc)
- [Firefox profile datastore](https://github.com/mozilla-firefox/firefox/blob/main/toolkit/profile/ProfilesDatastoreService.sys.mjs)
- [Playwright browser connections](https://playwright.dev/mcp/configuration/browser-extension)
- [Chrome native connection consent](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)
- [Chrome default-profile debugging restriction](https://developer.chrome.com/blog/remote-debugging-port)
- [Microsoft Edge DevTools Protocol](https://learn.microsoft.com/en-us/microsoft-edge/devtools/protocol/)
- [Firefox BiDi](https://firefox-source-docs.mozilla.org/remote/Security.html)
