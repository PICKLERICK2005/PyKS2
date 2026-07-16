"""pyks2 command-line interface.

A headless companion to the library — full camera control, scriptable from a
terminal. This is the "runs anywhere" surface (and the fallback for the planned
web GUI).

    pyks2 ping
    pyks2 info
    pyks2 shoot [--af off]
    pyks2 settings [get|set av=8.0 sv=400]
    pyks2 browse [--limit N]
    pyks2 download DIR/FILE [-o out] [--size view|full]
    pyks2 liveview -o frame.jpg [--frames N]
    pyks2 watch                 # stream /v1/changes events
    pyks2 apis
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from .client import K_S2_WiFi
from .errors import KS2Error


def _cam(args) -> K_S2_WiFi:
    logger = (lambda m: print(f"  · {m}", file=sys.stderr)) if args.verbose else None
    return K_S2_WiFi(ip=args.ip, timeout=args.timeout, logger=logger)


# -- commands ---------------------------------------------------------------

def cmd_ping(args) -> int:
    cam = _cam(args)
    ok = cam.ping()
    print("OK" if ok else "unreachable")
    return 0 if ok else 1


def cmd_apis(args) -> int:
    for a in _cam(args).apis():
        print(a)
    return 0


def cmd_info(args) -> int:
    cam = _cam(args)
    dev = cam.get_device_info()
    st = {}
    try:
        st = cam.status("camera")
    except KS2Error:
        pass
    print(f"model      : {dev.model}")
    print(f"firmware   : {dev.firmware_version}")
    print(f"serial     : {dev.serial_no}")
    print(f"battery    : {dev.battery}%")
    print(f"state      : {st.get('state')}")
    for s in dev.storages:
        print(f"storage    : {s.get('name')} "
              f"remain={s.get('remain')} format={s.get('format')}")
    return 0


def cmd_shoot(args) -> int:
    cam = _cam(args)
    if args.wait:
        # capture() records the baseline BEFORE firing, so completion
        # detection can't be fooled by the pre-existing last image.
        info = cam.capture(af=args.af, timeout=args.wait_timeout,
                           download_to=args.download, size=args.size)
        print(f"captured: {info.path}")
        if args.download:
            print(f"downloaded -> {args.download}")
    else:
        res = cam.shoot(af=args.af)
        print(f"triggered (focused={res.focused})")
    return 0


def cmd_settings(args) -> int:
    cam = _cam(args)
    if not args.assignments:
        # GET
        cp = cam.get_camera_params()
        for k in ("av", "tv", "sv", "xv", "wb_mode", "shoot_mode",
                  "exposure_mode", "still_size", "effect", "filter"):
            print(f"{k:14}: {getattr(cp, k)}")
        return 0
    # SET
    kv = {}
    for a in args.assignments:
        if "=" not in a:
            print(f"bad assignment {a!r}, expected key=value", file=sys.stderr)
            return 2
        k, v = a.split("=", 1)
        kv[k] = v
    cam.set_camera_params(**kv)
    print("set:", ", ".join(f"{k}={v}" for k, v in kv.items()))
    return 0


def cmd_lists(args) -> int:
    cc = _cam(args).get_camera_constants()
    for name in ("av_list", "tv_list", "sv_list", "xv_list", "wb_mode_list",
                 "exposure_mode_list", "shoot_mode_list", "still_size_list",
                 "effect_list", "filter_list"):
        vals = getattr(cc, name)
        print(f"{name:20}: {', '.join(vals)}")
    return 0


def cmd_focus(args) -> int:
    res = _cam(args).focus(args.x, args.y)
    print(f"focus: focused={res.focused}")
    return 0


def cmd_browse(args) -> int:
    listing = _cam(args).list_photos(limit=args.limit)
    for e in listing:
        print(e.path)
    print(f"# {len(listing)} file(s)", file=sys.stderr)
    return 0


def cmd_download(args) -> int:
    cam = _cam(args)
    out = args.output or args.path.replace("/", "_")
    n = cam.download(args.path, out, size=args.size)
    print(f"downloaded {n:,} bytes -> {out}")
    return 0


def cmd_liveview(args) -> int:
    cam = _cam(args)
    n = 0
    for frame in cam.iter_liveview_frames(max_frames=args.frames):
        n += 1
        path = args.output if args.frames == 1 else f"{args.output}.{n:04d}"
        with open(path, "wb") as f:
            f.write(frame)
        print(f"frame {n}: {len(frame):,} bytes -> {path}")
    return 0


def cmd_bulb(args) -> int:
    cam = _cam(args)
    print(f"bulb exposure: {args.seconds}s (dial must be on B)...")
    info = cam.bulb_exposure(args.seconds, af=args.af)
    print(f"captured: {info.path}")
    return 0


def cmd_watch(args) -> int:
    cam = _cam(args)
    print("watching /v1/changes (Ctrl-C to stop)...", file=sys.stderr)
    try:
        with cam.events() as ev:
            for change in ev:
                print(f"changed: {change.changed}")
                if change.is_storage and args.resolve:
                    try:
                        print(f"  -> {cam.latest_info().path}")
                    except KS2Error:
                        pass
    except KeyboardInterrupt:
        print()
    return 0


# -- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyks2", description="Pentax K-S2 WiFi control (CLI).")
    p.add_argument("--version", action="version",
                   version=f"pyks2 {__version__}")
    p.add_argument("--ip", default="192.168.0.1", help="camera IP")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="check connectivity").set_defaults(func=cmd_ping)
    sub.add_parser("apis", help="list all endpoints").set_defaults(func=cmd_apis)
    sub.add_parser("info", help="device + status summary").set_defaults(func=cmd_info)
    sub.add_parser("lists", help="capability lists").set_defaults(func=cmd_lists)

    sp = sub.add_parser("shoot", help="fire the shutter")
    sp.add_argument("--af", choices=["auto", "on", "off"], default=None,
                    help="AF mode (default: auto-detect from AF/MF lever)")
    sp.add_argument("--wait", action="store_true", help="wait for capture")
    sp.add_argument("--wait-timeout", type=float, default=30.0)
    sp.add_argument("--download", metavar="OUT", help="download the shot after")
    sp.add_argument("--size", choices=["view", "full"], default="full")
    sp.set_defaults(func=cmd_shoot)

    sp = sub.add_parser("settings", help="get or set camera settings")
    sp.add_argument("assignments", nargs="*", metavar="key=value",
                    help="omit to GET; provide to SET (e.g. av=8.0 sv=400)")
    sp.set_defaults(func=cmd_settings)

    sp = sub.add_parser("focus", help="drive AF / set point")
    sp.add_argument("-x", type=int, default=52)
    sp.add_argument("-y", type=int, default=52)
    sp.set_defaults(func=cmd_focus)

    sp = sub.add_parser("browse", help="list photos on the card")
    sp.add_argument("--limit", type=int, default=None)
    sp.set_defaults(func=cmd_browse)

    sp = sub.add_parser("download", help="download a photo")
    sp.add_argument("path", help="DIR/FILE")
    sp.add_argument("-o", "--output")
    sp.add_argument("--size", choices=["view", "full"], default=None)
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("liveview", help="grab live-view frame(s)")
    sp.add_argument("-o", "--output", default="frame.jpg")
    sp.add_argument("--frames", type=int, default=1)
    sp.set_defaults(func=cmd_liveview)

    sp = sub.add_parser("bulb", help="timed bulb exposure (dial must be on B)")
    sp.add_argument("seconds", type=float, help="exposure length in seconds")
    sp.add_argument("--af", default="off", choices=["auto", "on", "off"])
    sp.set_defaults(func=cmd_bulb)

    sp = sub.add_parser("watch", help="stream /v1/changes events")
    sp.add_argument("--resolve", action="store_true",
                    help="on storage events, print the new file path")
    sp.set_defaults(func=cmd_watch)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KS2Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
