# Altium Generic Library

A collection of generic schematic symbols for Altium Designer, organized by component type.

Symbol metadata (display name + category) is maintained in [symbols.yaml](symbols.yaml) — the single source of truth.

## Browse

**[Browse & import on Sideband →](https://getsideband.com/libraries/altium-generic)**

Preview every symbol, then import the ones you need straight into Sideband.

## Publishing

This repo **self-publishes** to the Sideband catalog. On every push to `main`, the
[Publish workflow](.github/workflows/publish.yml) sends the raw `.SchLib` files and the
`symbols.yaml` metadata to Sideband, which renders all previews server-side and publishes
the catalog entry. The repo itself does no rendering (no altium-monkey, no SVGs).

To publish manually:

```bash
pip install -r requirements.txt

export SIDEBAND_API_URL=https://…          # Sideband API
export SIDEBAND_CATALOG_URL=https://getsideband.com
export SIDEBAND_PUBLISH_TOKEN=…            # publish token
python publish.py
```

Add a symbol by dropping its `.SchLib` under `symbols/` and adding an entry to
`symbols.yaml`; the next push republishes the catalog entry.
