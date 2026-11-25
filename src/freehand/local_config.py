"""Machine-specific integration settings, kept outside the published source."""
import os
from pathlib import Path
import tomllib


def section(name):
    path = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'freehand/local.toml'
    try:
        with path.open('rb') as source:
            data = tomllib.load(source)
    except FileNotFoundError:
        return {}
    result = data.get(name, {})
    if not isinstance(result, dict):
        raise ValueError(f'[{name}] must be a TOML table in {path}')
    return result
