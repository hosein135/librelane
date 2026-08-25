#!/usr/bin/env python3
"""Export a layout view as vector SVG (geometry paths, zoom-friendly)."""

from typing import Tuple

import click
import pya


def _shape_to_path(shape: pya.Shape, dbu: float) -> str | None:
    if shape.is_polygon():
        poly = shape.polygon
        hull = poly.each_point_hull()
        if not hull:
            return None
        parts = []
        for i, pt in enumerate(hull):
            x = pt.x / dbu
            y = -pt.y / dbu
            parts.append(f"{'M' if i == 0 else 'L'}{x:.4f},{y:.4f}")
        parts.append("Z")
        return " ".join(parts)
    if shape.is_box():
        box = shape.box
        x1, y1 = box.left / dbu, -box.top / dbu
        x2, y2 = box.right / dbu, -box.bottom / dbu
        return f"M{x1:.4f},{y1:.4f} L{x2:.4f},{y1:.4f} L{x2:.4f},{y2:.4f} L{x1:.4f},{y2:.4f} Z"
    if shape.is_path():
        path = shape.path
        pts = path.get_points()
        if not pts:
            return None
        parts = []
        for i, pt in enumerate(pts):
            x = pt.x / dbu
            y = -pt.y / dbu
            parts.append(f"{'M' if i == 0 else 'L'}{x:.4f},{y:.4f}")
        return " ".join(parts)
    return None


def _layer_color(layer_index: int) -> str:
    palette = [
        "#1f77b4",
        "#ff7f0e",
        "#2ca02c",
        "#d62728",
        "#9467bd",
        "#8c564b",
        "#e377c2",
        "#7f7f7f",
        "#bcbd22",
        "#17becf",
    ]
    return palette[layer_index % len(palette)]


@click.command()
@click.option("-o", "--output", required=True)
@click.option("-l", "--input-lef", "input_lefs", multiple=True)
@click.option("-T", "--lyt", required=True, help="KLayout .lyt file")
@click.option("-P", "--lyp", required=True, help="KLayout .lyp file")
@click.option("-M", "--lym", required=True, help="KLayout .map (LEF/DEF layer map) file")
@click.argument("input")
def render_svg(
    input_lefs: Tuple[str, ...],
    output: str,
    lyt: str,
    lyp: str,
    lym: str,
    input: str,
):
    try:
        gds = input.endswith(".gds")

        tech = pya.Technology()
        tech.load(lyt)

        layout_options = None
        if not gds:
            layout_options = tech.load_layout_options
            layout_options.lefdef_config.map_file = lym
            layout_options.lefdef_config.macro_resolution_mode = 1
            layout_options.lefdef_config.read_lef_with_def = False
            layout_options.lefdef_config.lef_files = list(input_lefs)

        lv = pya.LayoutView()
        if gds:
            lv.load_layout(input)
        else:
            lv.load_layout(input, layout_options, lyt)

        lv.max_hier()
        lv.timer()

        cv = lv.active_cellview()
        layout = cv.layout()
        top = layout.top_cell()
        dbu = layout.dbu
        bbox = top.dbbox()
        if bbox.empty():
            raise RuntimeError("Layout bounding box is empty")

        x0 = bbox.left / dbu
        y0 = -bbox.top / dbu
        width = bbox.width() / dbu
        height = bbox.height() / dbu
        pad = max(width, height) * 0.02
        view_x = x0 - pad
        view_y = y0 - pad
        view_w = width + 2 * pad
        view_h = height + 2 * pad

        paths: list[str] = []
        for layer_index in layout.layer_indices():
            color = _layer_color(layer_index)
            info = layout.get_info(layer_index)
            layer_name = f"{info.layer}/{info.datatype}"
            siter = top.begin_shapes_rec(layer_index)
            while not siter.at_end():
                d = _shape_to_path(siter.shape(), dbu)
                if d:
                    paths.append(
                        f'<path d="{d}" fill="{color}" fill-opacity="0.35" '
                        f'stroke="{color}" stroke-width="0.08" '
                        f'data-layer="{layer_name}"/>'
                    )
                siter.next()

        svg = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="{view_x:.4f} {view_y:.4f} {view_w:.4f} {view_h:.4f}">\n'
            f'<rect x="{view_x:.4f}" y="{view_y:.4f}" width="{view_w:.4f}" height="{view_h:.4f}" fill="#ffffff"/>\n'
            + "\n".join(paths)
            + "\n</svg>\n"
        )

        with open(output, "w", encoding="utf-8") as f:
            f.write(svg)

    except Exception as e:
        print(e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    render_svg()
