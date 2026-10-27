#!/usr/bin/env python3
"""Render the canonical vector family with librsvg; no external artwork/fonts."""
import argparse
from pathlib import Path
import subprocess


def render(repo, destination):
    for name in ("writer", "sheets", "pdf", "slides"):
        for suffix in ("", "-document"):
            source = repo / "branding/source" / f"greyward-{name}{suffix}.svg"
            for size in (16, 24, 32, 48, 64, 128, 256, 512):
                output = destination / (name + suffix) / f"{size}.png"
                output.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size),
                                "-o", str(output), str(source)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.repo, args.output)
