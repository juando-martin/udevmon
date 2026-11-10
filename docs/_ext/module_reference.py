"""The ``udevmon-modules`` directive: autodoc for every module no other page documents.

It scans the top-level ``docs/*.rst`` pages for ``.. automodule::`` lines and documents
the rest of ``src/udevmon`` (subpackages included, ``__main__`` never imported).
"""

import re
from pathlib import Path

from docutils import nodes
from docutils.statemachine import StringList
from sphinx.util.docutils import SphinxDirective


class RemainingModules(SphinxDirective):
    """Expand to an ``automodule`` entry for each module not documented elsewhere."""

    def run(self):
        source = Path(self.env.srcdir)
        package = source.parent / "src" / "udevmon"
        documented = set()
        for page in source.glob("*.rst"):
            self.env.note_dependency(str(page))
            documented.update(re.findall(r"^\.\. automodule:: ([\w.]+)", page.read_text(),
                                         re.MULTILINE))
        lines = []
        for path in sorted(package.rglob("*.py")):
            if path.name in ("__main__.py", "_version.py"):
                continue
            parts = path.relative_to(package.parent).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            module = ".".join(parts)
            if module not in documented:
                lines.append(f".. automodule:: {module}")
                if path.name != "__init__.py":
                    lines.append("   :members:")
                lines.append("")
        container = nodes.container()
        self.state.nested_parse(StringList(lines, source=str(source / "modules.rst")),
                                self.content_offset, container)
        return container.children


def setup(app):
    app.add_directive("udevmon-modules", RemainingModules)
    return {"version": "1", "parallel_read_safe": True}
