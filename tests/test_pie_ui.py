"""Exercise the overlay's message handler without requiring a Wayland display."""
import ast
from pathlib import Path
from types import SimpleNamespace


def test_empty_result_finishes_loading():
    source = Path(__file__).parents[1] / 'src/freehand/pie_ui.py'
    tree = ast.parse(source.read_text())
    window = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PieWindow')
    handler = next(n for n in window.body if isinstance(n, ast.FunctionDef) and n.name == 'handle')
    namespace = {'load_colors': lambda: {}}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), 'exec'), namespace)
    ui = SimpleNamespace(area=SimpleNamespace(queue_draw=lambda: None), set_visible=lambda _: None)
    handle = namespace['handle']
    handle(ui, {'cmd': 'show', 'title': 'Chats', 'items': [None] * 8,
                'loading': True, 'empty_text': 'No unread chats'})
    assert ui.loading
    handle(ui, {'cmd': 'items', 'items': [None] * 8})
    assert not ui.loading
    assert ui.empty_text == 'No unread chats'
    handle(ui, {'cmd': 'show', 'title': 'Apps', 'items': []})
    assert not ui.loading
    assert ui.empty_text == 'Empty'
