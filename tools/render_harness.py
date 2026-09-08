"""Render per-harness skill and agent files from skills-src/ and agents-src/.

Usage: python tools/render_harness.py [--check]
Tokens: {{invoke:<skill>}} {{spawn_tool}} {{model:small}} {{model:mid}}
        {{Model:small}} {{Model:mid}} (title-cased) {{harness_name}}
        {{harness:<name>}}...{{/harness}} (not nestable)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from teammate_comms import harness  # noqa: E402

SKILLS_SRC = REPO / "skills-src"
AGENTS_SRC = REPO / "agents-src"
CODEX_SKILLS = ("teammate-comms",)

_BLOCK = re.compile(r"\{\{harness:([a-z-]+)\}\}(.*?)\{\{/harness\}\}", re.DOTALL)
_TOKEN = re.compile(r"\{\{([A-Za-z_]+)(?::([a-z-]+))?\}\}")


class RenderError(Exception):
    pass


def render_text(text: str, hz: harness.Harness) -> str:
    def block(m: re.Match) -> str:
        if "{{harness:" in m.group(2):
            raise RenderError("nested harness blocks are not supported")
        return m.group(2) if m.group(1) == hz.name else ""

    out = _BLOCK.sub(block, text)
    if "{{/harness}}" in out:
        raise RenderError("unmatched {{/harness}}")

    def token(m: re.Match) -> str:
        kind, arg = m.group(1), m.group(2)
        if kind == "invoke" and arg:
            return hz.skill_invoke(arg)
        if kind == "spawn_tool":
            return hz.spawn_tool
        if kind == "harness_name":
            return hz.display_name
        if kind in ("model", "Model") and arg in ("small", "mid"):
            value = hz.default_models[arg]
            if value is None:
                raise RenderError(f"{hz.name}: no default model for tier {arg!r}")
            return value.title() if kind == "Model" else value
        raise RenderError(f"unknown token {m.group(0)}")

    out = _TOKEN.sub(token, out)
    if "{{" in out or "}}" in out:
        raise RenderError("unconsumed token marker in output")
    return out


def targets() -> list[tuple[Path, Path, harness.Harness]]:
    items: list[tuple[Path, Path, harness.Harness]] = []
    claude = harness.HARNESSES[harness.CLAUDE_CODE]
    codex = harness.HARNESSES[harness.CODEX]
    for src in sorted(SKILLS_SRC.glob("*/SKILL.md")):
        name = src.parent.name
        items.append((src, REPO / "skills" / name / "SKILL.md", claude))
        if name in CODEX_SKILLS:
            items.append((src, REPO / "adapters" / "codex" / "skills" / name / "SKILL.md", codex))
    for src in sorted(AGENTS_SRC.glob("*.md")):
        items.append((src, REPO / "agents" / src.name, claude))
    return items


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", newline="").replace("\r\n", "\n")


def render_all(check: bool) -> list[str]:
    problems: list[str] = []
    for src, dst, hz in targets():
        rendered = render_text(_read(src), hz)
        if check:
            current = _read(dst) if dst.exists() else None
            if current != rendered:
                problems.append(f"drift: {dst.relative_to(REPO)} (source {src.relative_to(REPO)}, harness {hz.name})")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(rendered, encoding="utf-8", newline="\n")
    return problems


def main(argv: list[str]) -> int:
    check = "--check" in argv
    problems = render_all(check)
    for p in problems:
        print(p)
    if not check:
        print(f"rendered {len(targets())} files")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
