from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "index.html"

CSS_TAG = '<link rel="stylesheet" href="/static/nomad-ui/nomad-ui.css" data-nomad-modern="true">'
JS_TAG = '<script type="module" src="/static/nomad-ui/nomad-ui.js" data-nomad-modern="true"></script>'

text = TEMPLATE.read_text(encoding="utf-8")

# Idempotent: never duplicate the foundation tags during repeated builds.
if 'data-nomad-modern="true"' not in text:
    marker = "</head>"
    if marker not in text:
        raise SystemExit("templates/index.html has no </head> marker")
    text = text.replace(marker, f"{CSS_TAG}\n{JS_TAG}\n{marker}", 1)
    TEMPLATE.write_text(text, encoding="utf-8")
    print("Integrated NOMAD modern frontend foundation.")
else:
    print("NOMAD modern frontend foundation already integrated.")
