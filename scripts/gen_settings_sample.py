#!/usr/bin/env python3
"""Generate settings.conf.sample from config.py.

The sample has to list every setting an operator may change, with its default
and the explanation that sits beside it in config.py. Written by hand it would
drift the moment somebody adds a setting - which is the failure this project
has already had twice, with PRESERVE_RUNTIME and with the two import lists.

So it is generated, and tests/test_settings_file.py regenerates it and compares
against the committed file. Adding a setting and forgetting the sample fails
the build, and the fix is to run this script.

    python scripts/gen_settings_sample.py
"""

import ast
import io
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import settings_file  # noqa: E402

HEADER = """\
# =====================================================================
# DCCore - settings.conf.sample
# =====================================================================
# Copy this file to settings.conf and uncomment the lines you want to
# change. Anything left commented out keeps its default, shown here.
#
#     cp settings.conf.sample settings.conf
#
# settings.conf is gitignored, so your values never show up as a diff
# and are never overwritten by an update.
#
# GENERATED FILE - do not edit by hand. It is produced from config.py by
# scripts/gen_settings_sample.py, and a test fails if the two disagree.
#
# NOTES
#   * The [section] headers are for reading convenience only. They are
#     flattened away when the file is read, so a setting works wherever
#     you put it - and a setting may not appear twice.
#   * Yes/no settings accept true/false, yes/no, on/off, 1/0.
#   * List settings are comma-separated.
#   * A '%' is an ordinary character here; it needs no escaping.
#   * If you already have a admin_config.py it still works exactly as
#     before. This file is applied on top of it, so you can migrate a
#     few settings at a time, or ignore this file entirely.
#   * The mIRC colour codes (C_*) are deliberately not listed: they are
#     raw protocol bytes rather than preferences.
"""


def assignment_parts(node):
    """(targets, value_node) for a module-level assignment, else (None, None).

    `MAX_DCC_SLOTS = 3` parses to ast.Assign, but `MAX_DCC_SLOTS: int = 3`
    parses to ast.AnnAssign - a different node type, with `.target` rather
    than `.targets`. Matching only Assign makes every annotated setting
    invisible, which for this generator means silently emitting a sample with
    nothing in it.

    `value_node` is None for a bare annotation (`NICKNAME: str`), which
    declares a name's type without giving it a value.
    """
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target], node.value
    return None, None


def _render(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(part) for part in value)
    if value is None:
        return ""
    return str(value)


def _doc_lines(source_lines, node):
    """The comment block immediately above a setting, plus its inline comment."""
    doc = []
    index = node.lineno - 2                  # the line above, 0-based
    block = []
    while index >= 0:
        stripped = source_lines[index].strip()
        if stripped.startswith("#") and not stripped.startswith("# ---") \
                and not stripped.startswith("# ==="):
            block.append(stripped.lstrip("#").strip())
            index -= 1
            continue
        break
    doc.extend(reversed(block))

    # Look for the inline comment only AFTER the value ends. Splitting the
    # whole line on "#" cuts inside a string literal, so
    #     CHANNEL = "#example-one,#example-two,..."
    # produced a junk comment line reading `example-one,...#example-three"` above
    # every channel-valued setting in the generated sample.
    own = source_lines[node.lineno - 1]
    if node.end_lineno == node.lineno:
        tail = own[node.end_col_offset:]
        if "#" in tail:
            inline = tail.split("#", 1)[1].strip()
            if inline:
                doc.append(inline)
    return doc


def build():
    path = os.path.join(REPO_ROOT, "defaults.py")
    with io.open(path, encoding="utf-8") as handle:
        source = handle.read()
    lines = source.split("\n")
    tree = ast.parse(source)

    import defaults as config

    section = "general"
    out = [HEADER]
    current = None

    for node in tree.body:
        # Pick up the numbered section headers as they go past.
        for offset in range(max(0, node.lineno - 5), node.lineno):
            text = lines[offset - 1] if offset >= 1 else ""
            head = text.lstrip("# ").strip()
            if text.startswith("# ") and head[:2].rstrip(".").isdigit() and "." in head:
                section = head.split(".", 1)[1].strip().lower()

        targets, value_node = assignment_parts(node)
        if targets is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if not hasattr(config, name):
                continue
            # Read the default from the SOURCE, not from the imported
            # module. The live module reflects whatever admin_config.py,
            # settings.conf or a test has already applied, which would make
            # the generated sample depend on the machine that ran it.
            try:
                default = ast.literal_eval(value_node)
            except (ValueError, SyntaxError):
                default = getattr(config, name)
            if not settings_file.is_overridable(name, default):
                continue

            if section != current:
                out.append(f"\n[{section}]\n")
                current = section

            for line in _doc_lines(lines, node):
                out.append(f"# {line}\n")
            # settings_file.REQUIRED (issue #170's RFC): shipping a real value
            # here is exactly the defect this exists to close - a copy-paste
            # install that never edits a REQUIRED line still gets a working,
            # wrong bot (somebody else's channel, nickname, admin authority).
            # Blank leaves nothing to accidentally rely on.
            shown = "" if name in settings_file.REQUIRED else _render(default)
            if name in settings_file.REQUIRED:
                out.append(f"# REQUIRED - the daemon refuses to start until this is set.\n")
            out.append(f"#{name} = {shown}\n\n")

    return "".join(out).rstrip("\n") + "\n"


def main():
    target = os.path.join(REPO_ROOT, "settings.conf.sample")
    content = build()
    with io.open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    count = content.count("\n#") - content.count("\n# ")
    print(f"Wrote {os.path.basename(target)} ({count} settings).")


if __name__ == "__main__":
    main()
