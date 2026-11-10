"""Locate bundled contracts in a source plugin or an installed CLI wheel."""
import os
from pathlib import Path


def plugin_root(selected=None):
    if selected is not None or os.environ.get('BLINK_PLUGIN_ROOT'):
        return Path(selected or os.environ['BLINK_PLUGIN_ROOT']).expanduser().resolve()
    source = Path(__file__).resolve().parents[2]
    if (source / 'schemas/index.json').is_file():
        return source
    return Path(__file__).resolve().parent / 'data/plugin'
