#!/usr/bin/env python3
"""Rebuild docs/llms-full.txt (plain markdown for LLM crawlers) from README.md, AGENTS.md and docs/TOOL_ROUTING.md.

Run from anywhere: python3 scripts/make_llms_full.py
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


readme = read("README.md")
readme = readme[readme.index("venice-bridge is a free, open-source"):]  # drop the banner, badges and tag chips
readme = re.sub(r"## Screenshots\n.*?(?=\n## )", "", readme, flags=re.S)  # images mean nothing in a text file
version = re.search(r'version = "([^"]+)"', read("pyproject.toml")).group(1)
out = ("# venice-bridge\n\n> " + readme.split("\n", 1)[0] + "\n\n"
       "Version %s · MIT · Repository: https://github.com/nsozturk/venice-bridge · "
       "Docs: https://nsozturk.github.io/venice-bridge/\n\n" % version
       + "---\n\n# README\n\n" + readme.strip() + "\n\n---\n\n"
       + read("AGENTS.md").strip() + "\n\n---\n\n" + read("docs", "TOOL_ROUTING.md").strip() + "\n")
with open(os.path.join(ROOT, "docs", "llms-full.txt"), "w", encoding="utf-8") as f:
    f.write(out)
print("docs/llms-full.txt: %d lines" % out.count("\n"))
