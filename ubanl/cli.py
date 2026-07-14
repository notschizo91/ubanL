"""Command line interface.

    ubanl info model.stl
    ubanl chop project.yaml
    ubanl chop model.stl --target-height 400 --build-volume 220x220x250 -o out/
    ubanl coupon --diameter 8 --length 8 -o coupon/
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np


def _cmd_info(args: argparse.Namespace) -> int:
    from .pipeline import load_mesh

    mesh = load_mesh(args.mesh)
    ext = mesh.extents
    print(f"file:        {args.mesh}")
    print(f"faces:       {len(mesh.faces):,}")
    print(f"extents:     {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm")
    print(f"watertight:  {mesh.is_watertight}")
    print(f"solid:       {mesh.is_volume}")
    if mesh.is_volume:
        print(f"volume:      {mesh.volume / 1000.0:.1f} cm3")
    obb = np.sort(mesh.bounding_box_oriented.primitive.extents)
    print(f"obb (sorted): {obb[0]:.1f} x {obb[1]:.1f} x {obb[2]:.1f} mm")
    return 0


def _parse_build_volume(text: str) -> tuple[float, float, float]:
    parts = text.lower().replace(",", "x").split("x")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("build volume must look like 220x220x250")
    return tuple(float(p) for p in parts)  # type: ignore[return-value]


def _cmd_chop(args: argparse.Namespace) -> int:
    from .config import ConfigError, config_from_dict, load_config
    from .pipeline import run

    target = Path(args.target)
    if target.suffix.lower() in (".yaml", ".yml"):
        cfg = load_config(target)
    else:
        raw: dict = {"input": str(target)}
        if args.target_height is not None:
            raw["target_height_mm"] = args.target_height
        if args.scale is not None:
            raw["scale_factor"] = args.scale
        cfg = config_from_dict(raw)
    # CLI flags override the config file
    if args.mode:
        cfg.planner.mode = args.mode
    if args.build_volume:
        cfg.printer.build_volume_mm = args.build_volume
    if args.clearance is not None:
        cfg.printer.clearance_mm = args.clearance
    if args.out:
        cfg.output.dir = args.out
    if args.no_connectors:
        cfg.connectors.enabled = False
    if args.no_labels:
        cfg.labels.enabled = False
    try:
        result = run(cfg)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    print(f"pieces:     {len(result.segmentation.pieces)}")
    print(f"connectors: {len(result.connectors.connectors)}")
    print(f"output:     {cfg.output.dir}")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _cmd_coupon(args: argparse.Namespace) -> int:
    from .config import ConnectorConfig
    from .coupon import clearance_hint, coupon_pin, coupon_plate

    cfg = ConnectorConfig(diameter_mm=args.diameter, length_mm=args.length)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    coupon_plate(cfg).export(out / "coupon_plate.stl")
    coupon_pin(cfg).export(out / "coupon_pin.stl")
    print(f"wrote {out / 'coupon_plate.stl'} and {out / 'coupon_pin.stl'}")
    print(clearance_hint())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ubanl",
        description="Segment large models into printable pieces with self-aligning connectors.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="print mesh statistics")
    p_info.add_argument("mesh")
    p_info.set_defaults(fn=_cmd_info)

    p_chop = sub.add_parser("chop", help="segment a model (config YAML or mesh + flags)")
    p_chop.add_argument("target", help="project .yaml, or a mesh file")
    p_chop.add_argument("--target-height", type=float, help="scale model to this height (mm)")
    p_chop.add_argument("--scale", type=float, help="uniform scale factor")
    p_chop.add_argument("--mode", choices=["auto", "grid", "manual"], help="planner")
    p_chop.add_argument(
        "--build-volume", type=_parse_build_volume, help="printer volume, e.g. 220x220x250"
    )
    p_chop.add_argument("--clearance", type=float, help="pin/socket clearance (mm)")
    p_chop.add_argument("-o", "--out", help="output directory")
    p_chop.add_argument("--no-connectors", action="store_true")
    p_chop.add_argument("--no-labels", action="store_true")
    p_chop.set_defaults(fn=_cmd_chop)

    p_coupon = sub.add_parser("coupon", help="generate a clearance calibration coupon")
    p_coupon.add_argument("--diameter", type=float, default=8.0)
    p_coupon.add_argument("--length", type=float, default=8.0)
    p_coupon.add_argument("-o", "--out", default="coupon")
    p_coupon.set_defaults(fn=_cmd_coupon)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
