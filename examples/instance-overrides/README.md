# WordPress base and installation-specific quirks

All URLs, organization values and names here are fictional. Keep real private child definitions and
configuration outside shared repositories. These examples are outside the bundled discovery catalog.

| File | Purpose |
| --- | --- |
| `wordpress-base.json` | Reusable WordPress schema with an unspecified `site` address and two public reads. |
| `acme.instance.json` | Thin child referencing the sibling base; custom article route/query/response, new health read and disabled single-post read. |
| `acme-embedded.instance.json` | Equivalent standalone transport with the pinned base embedded. Choose one child form when installing. |
| `private-inputs.example.json` | Fictional organization value used by the customized article operation. |
| `weblink.json` | Package descriptor with independently hashed primary and `base-openapi` artifacts. |

The thin child pins the reusable base and fixes it to `https://acme.example/blog`. Another site needs its
own child identity and target; it does not inherit these quirks implicitly. The embedded and thin forms
have the same operations, but distinct authored-definition hashes and review candidates.

Install the thin child with:

```sh
blink human private_set --input examples/instance-overrides/private-inputs.example.json --yes
blink install examples/instance-overrides/acme.instance.json --source local --id wordpress-acme --yes
```

Review and approve its exact staged digest, then bind and grant the intended account. The public-read
base does not provide a private site's authentication recipe. See the
[instance guide](../../docs/instance-overrides.md) for pinning, native MCP input, source packages and learning.
