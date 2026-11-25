"""Application overrides for button actions and pie menus."""

from dataclasses import asdict
import json
import logging
import subprocess

from .engine import Binding

log = logging.getLogger('freehand')


def active_class():
    try:
        result = subprocess.run(['hyprctl', '-j', 'activewindow'], capture_output=True,
                                text=True, check=True, timeout=0.3)
        return json.loads(result.stdout).get('class', '')
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return ''


class Layers:
    def __init__(self, raw, defaults, button_names, focus=active_class):
        self.focus = focus
        self.profiles = []
        for name, spec in raw.items():
            if set(spec) - {'class', 'buttons'} or not isinstance(spec.get('class'), str) or not spec['class']:
                raise ValueError(f'[applications.{name}] requires class and optional buttons')
            overrides = {}
            for button, patch in spec.get('buttons', {}).items():
                if button not in button_names:
                    raise ValueError(f'[applications.{name}] unknown button {button}')
                base = defaults.get(button, Binding())
                if button == 'siri' or base.mouse or 'mouse' in patch:
                    raise ValueError('application overrides do not support Siri/mouse bindings')
                if 'menu' in patch:
                    overrides[button] = Binding.from_dict(patch)
                    continue
                if base.menu:
                    overrides[button] = Binding.from_dict(patch)
                    continue
                merged = {k: v for k, v in asdict(base).items() if v is not None}
                merged.update(patch)
                overrides[button] = Binding.from_dict(merged)
            self.profiles.append((spec['class'], overrides))

    def resolve(self, name, default):
        candidates = [(cls, overrides[name]) for cls, overrides in self.profiles if name in overrides]
        if not candidates:
            return default
        focused = self.focus()
        for cls, binding in reversed(candidates):
            if cls == focused:
                return binding
        return default
