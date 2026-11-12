# Multiple deployment templates

These examples are routing templates, outside the bundled discovery catalog. All domains are example
values; replace them only in your private local configuration.

| File | Purpose |
| --- | --- |
| `wordpress.openapi.json` | Public WordPress posts at an unspecified private installation, including a subdirectory base URL. |
| `test-environments.openapi.json` | Separate API and web slots, an original API discovery hint and a fixed login origin. |
| `*.private-patch.example.json` | Example `private_set` payloads with named deployment address sets. |

Save a private patch with `blink human private_set --input FILE --yes`. Stage the corresponding shared
manifest with `blink install FILE --source staging --id ID --deployment staging --yes`, inspect the
concrete candidate, and approve its exact digest. The WordPress template only reads public posts; record
and review an authentication recipe before using authenticated content.

See [the deployment guide](../../docs/deployments.md) for simultaneous environments, replacement,
source-package staging and the address contract.
