# Local source configuration examples

- `sources.json` configures the fictional `team` Git source.
- `sources.lock.json` demonstrates latest source metadata and independent per-install snapshot provenance.
  Commit IDs are synthetic. Catalog/descriptor hashes match the actual plugin-root catalog and `blinks/` files.
- The complete authored distribution is [../blink-catalog.json](../blink-catalog.json) plus
  [../blinks/](../blinks/README.md).

`git.example` and `banana.example` are fictional endpoints. These files are fixtures, not an installed
integration or a fetched Git repository.

Validate from the plugin directory:

```sh
uv run --with jsonschema==4.25.1 python tools/check_web_link_contract.py --self-test
```
