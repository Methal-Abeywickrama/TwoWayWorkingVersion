#!/usr/bin/env python3
"""
embed_blocks.py - copies the code of the embedded Python blocks from their cdp_link_<name>.py files back into the
flowgraph (v6.9_cdp_link.grc), so GNU Radio Companion runs the same code as `cdp_daemon.py --direct`.

    python3 tools/embed_blocks.py epy_csma epy_block_0_0_1          (keeps v6.9_cdp_link.grc.bak)

Only the _source_code of the named blocks changes; the script checks that every other block is byte-identical
after loading, and that the new code compiles.
"""
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
GRC = ROOT / "v6.9_cdp_link.grc"


def span(lines, name):
    """-> (index of the '_source_code:' line, end index (exclusive), key indent) for block `name`"""
    i = lines.index(f"- name: {name}\n")
    while not lines[i].lstrip().startswith("_source_code:"):
        i += 1
    ind = len(lines[i]) - len(lines[i].lstrip())
    k = i + 1
    while k < len(lines) and (lines[k].strip() == "" or len(lines[k]) - len(lines[k].lstrip()) > ind):
        k += 1
    return i, k, ind


def scalar(code, ind):
    """the code as a double-quoted YAML scalar, laid out like GRC writes it"""
    out = yaml.dump(code, default_style='"', width=80, allow_unicode=True).rstrip("\n").split("\n")
    first = " " * ind + "_source_code: " + out[0] + "\n"
    rest = [" " * (ind + 2) + l.lstrip(" ") + "\n" for l in out[1:]]
    return [first] + rest


def main(names):
    text = GRC.read_text()
    before = {b["name"]: b["parameters"].get("_source_code") for b in yaml.safe_load(text)["blocks"]}
    lines = text.splitlines(keepends=True)
    want = {}
    for name in names:
        code = (ROOT / f"cdp_link_{name}.py").read_text()
        compile(code, name, "exec")
        a, b, ind = span(lines, name)
        lines[a:b] = scalar(code, ind)
        want[name] = code
    out = "".join(lines)
    after = {b["name"]: b["parameters"].get("_source_code") for b in yaml.safe_load(out)["blocks"]}
    for name, src in before.items():
        if name in want:
            assert after[name] == want[name], f"{name}: embedded code differs from the .py file"
        else:
            assert after[name] == src, f"{name} changed"
    shutil.copy2(GRC, GRC.with_name(GRC.name + ".bak"))
    GRC.write_text(out)
    print(f"{GRC.name}: code of {', '.join(names)} updated from the .py files; other blocks unchanged")


if __name__ == "__main__":
    main(sys.argv[1:] or ["epy_csma", "epy_block_0_0_1"])
