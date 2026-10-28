"""A docs/*.md dokumentumokból Jupyter notebookok (docs/notebooks/*.ipynb) a kényelmes olvasáshoz.

A forrás továbbra is az .md fájl; a notebook csak nézet, módosítás után újra kell generálni:
    python docs/build_notebooks.py
Minden „## ” fejezet külön Markdown-cella lesz (a kódblokkon belüli sorokat nem vágja szét).
A Mermaid-diagramokat a JupyterLab 4.1+ magától kirajzolja.
"""
import json
from pathlib import Path

DOCS = Path(__file__).resolve().parent
OUT = DOCS / "notebooks"


def split_sections(text):
    """A Markdown szöveg fejezetenkénti darabolása a „## ” címeknél, kódblokkon kívül.
    Hívja: build."""
    cells, current, in_fence = [], [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if line.startswith("## ") and not in_fence and current:
            cells.append(current)
            current = []
        current.append(line)
    if current:
        cells.append(current)
    return cells


def build(md_path):
    """Egy .md fájlból egy csak Markdown-cellákból álló notebook írása a docs/notebooks alá.
    Hívja: main."""
    cells = [{"cell_type": "markdown", "id": f"sec-{i:02d}", "metadata": {}, "source": lines}
             for i, lines in enumerate(split_sections(md_path.read_text(encoding="utf-8")))]
    notebook = {"cells": cells, "metadata": {"language_info": {"name": "markdown"}},
                "nbformat": 4, "nbformat_minor": 5}
    target = OUT / (md_path.stem + ".ipynb")
    target.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
    return target, len(cells)


def main():
    """Az összes docs/*.md átalakítása. Hívja: a parancssor."""
    OUT.mkdir(exist_ok=True)
    for md_path in sorted(DOCS.glob("*.md")):
        target, count = build(md_path)
        print(f"{target.relative_to(DOCS.parent)}  ({count} cella)")


if __name__ == "__main__":
    main()
