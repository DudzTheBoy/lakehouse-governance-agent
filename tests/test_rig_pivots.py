"""Every animated part of the mascot must pivot on its own box.

`transform-origin` percentages resolve against whatever `transform-box` says, and the
initial value for an SVG element is `view-box` -- the whole drawing. A rule that sets
a percentage origin without also setting `transform-box: fill-box` therefore does not
pivot the element around itself: it pivots it around a point in the drawing, and the
part swings away from the body.

That is what happened to the head. It had the origin and not the box, so `look`
rotated it about the origin of the drawing, roughly the feet, and it detached from
the helmet by about seven units -- small in the source, obvious on screen. The limbs
escaped it only because they share a class that carries the property.
"""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "build_site.py"
RULE = re.compile(r"^([.#][\w\-.,\s#>]*?)\s*\{([^}]*)\}", re.MULTILINE)

# Selectors that pivot on the drawing origin on purpose: an orbit revolves around the
# centre of its system, which is the origin of the group it sits in.
DRAWING_ORIGIN = {".rev", ".comet.b g"}

# Parts that inherit the box from another class on the same element, rather than
# declaring it themselves. Each entry is the class that actually carries fill-box.
INHERITS_BOX = {".arm-l": ".limb", ".arm-r": ".limb", ".leg-l": ".limb", ".leg-r": ".limb"}


def rules():
    text = SRC.read_text(encoding="utf-8")
    for m in RULE.finditer(text):
        yield m.group(1).strip(), m.group(2)


def declares_fill_box() -> set[str]:
    return {sel for sel, body in rules() if "fill-box" in body}


def percentage_origin_selectors() -> set[str]:
    out = set()
    for sel, body in rules():
        if "transform-origin" not in body:
            continue
        value = body.split("transform-origin")[1].split(";")[0]
        if "%" in value:
            out.add(sel)
    return out


def test_the_scan_finds_something():
    assert percentage_origin_selectors(), "no percentage origins found -- regex is stale"
    assert declares_fill_box(), "no fill-box rules found -- regex is stale"


def test_percentage_origins_pivot_on_their_own_box():
    boxed = declares_fill_box()
    offenders = []
    for selector in percentage_origin_selectors() - DRAWING_ORIGIN:
        parts = [p.strip() for p in selector.split(",")]
        for part in parts:
            covered = part in boxed or INHERITS_BOX.get(part) in boxed
            if not covered:
                offenders.append(part)
    assert not offenders, (
        "these set a percentage transform-origin with no transform-box, so they pivot "
        f"around the drawing instead of around themselves: {sorted(set(offenders))}"
    )


@pytest.mark.parametrize("selector", [".rig", ".head", ".limb", ".bubble"])
def test_rigged_parts_declare_fill_box(selector):
    text = SRC.read_text(encoding="utf-8")
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", text)
    assert match, f"{selector} rule not found"
    assert "fill-box" in match.group(1), (
        f"{selector} animates and must pivot on its own box, not on the drawing"
    )
