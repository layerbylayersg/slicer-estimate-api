from fastapi import FastAPI, HTTPException, Body
from typing import Union
from pydantic import BaseModel, HttpUrl

import math
import re
import os
import tempfile
import subprocess
import requests

app = FastAPI()


class Req(BaseModel):
    file_url: HttpUrl
    material: str = "PLA"
    quality: str = "standard"
    supports: bool = False
    copies: int = 1


def _calc_grams_from_length_mm(length_mm: float, material: str, filament_diameter_mm: float = 1.75) -> float:
    # Density (g/cm³)
    mat = str(material).upper()
    density = 1.24  # PLA default
    if mat == "PETG":
        density = 1.27

    radius_mm = filament_diameter_mm / 2.0
    area_mm2 = math.pi * (radius_mm ** 2)
    volume_mm3 = area_mm2 * length_mm
    volume_cm3 = volume_mm3 / 1000.0  # 1000 mm^3 = 1 cm^3
    return volume_cm3 * density


def _extrusion_length_mm_from_e_axis(gcode: str) -> float:
    """
    Compute filament length from the E axis (mm of filament).
    Supports:
      - M82 absolute extrusion
      - M83 relative extrusion
      - G92 E... resets
      - E.20855 (no leading 0)
      - lowercase e
      - inline comments
    """
    absolute = True
    e_pos = 0.0
    total = 0.0

    # Case-insensitive E/e
    e_re = re.compile(r"[Ee](-?\d*\.?\d+)")

    for raw in gcode.splitlines():
        # remove inline comments
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue

        # mode changes
        if line.startswith("M82"):
            absolute = True
            continue
        if line.startswith("M83"):
            absolute = False
            continue

        # reset extruder
        if line.startswith("G92"):
            m = e_re.search(line)
            if m:
                e_pos = float(m.group(1))
            continue

        # only consider move commands
        if not (line.startswith("G0") or line.startswith("G1")):
            continue

        m = e_re.search(line)
        if not m:
            continue

        e_val = float(m.group(1))

        if absolute:
            delta = e_val - e_pos
            e_pos = e_val
            if delta > 0:
                total += delta
        else:
            if e_val > 0:
                total += e_val

    return max(0.0, total)




def parse_filament_g(gcode: str, material: str = "PLA", filament_diameter_mm: float = 1.75) -> float:
    # 1) Try grams directly (if slicer included it)
    gram_patterns = [
        r"filament used \[g\]\s*=\s*([0-9.]+)",
        r"filament used\s*=\s*([0-9.]+)\s*g",
        r"Filament used:\s*([0-9.]+)\s*g",
    ]
    for line in gcode.splitlines():
        for p in gram_patterns:
            m = re.search(p, line, re.IGNORECASE)
            if m:
                return float(m.group(1))

    # 2) Try length summary (mm or m), then convert to grams
    length_mm = None
    length_patterns = [
        (r"filament used \[mm\]\s*=\s*([0-9.]+)", "mm"),
        (r"filament used\s*=\s*([0-9.]+)\s*mm", "mm"),
        (r"filament used\s*=\s*([0-9.]+)\s*m\b", "m"),
    ]
    for line in gcode.splitlines():
        for p, unit in length_patterns:
            m = re.search(p, line, re.IGNORECASE)
            if m:
                val = float(m.group(1))
                length_mm = val * 1000.0 if unit == "m" else val
                break
        if length_mm is not None:
            break

    if length_mm is not None:
        return _calc_grams_from_length_mm(length_mm, material, filament_diameter_mm)

    # 3) Final fallback: compute length from E axis values
    e_length_mm = _extrusion_length_mm_from_e_axis(gcode)
    if e_length_mm <= 0:
        return 0.0

    return _calc_grams_from_length_mm(e_length_mm, material, filament_diameter_mm)


def parse_time_seconds(txt: str) -> int:
    m = re.search(r"estimated printing time.*=\s*([0-9hms\s]+)", txt, re.IGNORECASE)
    if not m:
        return -1
    s = m.group(1)
    h = int(re.search(r"(\d+)\s*h", s).group(1)) if re.search(r"(\d+)\s*h", s) else 0
    m_ = int(re.search(r"(\d+)\s*m", s).group(1)) if re.search(r"(\d+)\s*m", s) else 0
    se = int(re.search(r"(\d+)\s*s", s).group(1)) if re.search(r"(\d+)\s*s", s) else 0
    return h * 3600 + m_ * 60 + se


def download(url: str, out_path: str):
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=120)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)


def slice_with_prusa(model_path: str, out_gcode: str, material: str, quality: str, supports: bool):
    base = "profiles/base.ini"
    mat = f"profiles/{material.lower()}.ini"
    qual = f"profiles/{quality}.ini"

    if not (os.path.exists(base) and os.path.exists(mat) and os.path.exists(qual)):
        raise RuntimeError("Missing profile files")

    cmd = [
        "prusa-slicer",
        "--slice",
        "--load", base,
        "--load", mat,
        "--load", qual,
    ]

    # Enable supports only by adding the flag (no 0/1 values)
    if supports:
        cmd += ["--support-material"]

    cmd += [
        "--export-gcode",
        f"--output={out_gcode}",
        model_path,
    ]

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[:1200])


@app.post("/estimate")
def estimate(payload: Union[Req, str] = Body(...)):
    try:
        if isinstance(payload, str):
            req = Req(file_url=payload)
        else:
            req = payload

        with tempfile.TemporaryDirectory() as tmp:
            name = str(req.file_url).split("?")[0].split("/")[-1]
            if not (name.lower().endswith(".stl") or name.lower().endswith(".3mf")):
                raise HTTPException(400, "Only STL/3MF supported")

            model_path = os.path.join(tmp, name)
            out_gcode = os.path.join(tmp, "out.gcode")

            download(str(req.file_url), model_path)
            slice_with_prusa(model_path, out_gcode, req.material, req.quality, req.supports)

            gcode = open(out_gcode, "r", encoding="utf-8", errors="ignore").read()

            g = parse_filament_g(gcode, req.material)
            t = parse_time_seconds(gcode)
            
            # HARD fallback: if parser returns 0 but we can detect extrusion, compute grams from E axis
            e_len = _extrusion_length_mm_from_e_axis(gcode)
            if g == 0 and e_len > 0:
                g = _calc_grams_from_length_mm(e_len, req.material)


            if t < 0:
                raise RuntimeError("Failed to read slicer output")

            resp = {
                "print_time_seconds": t * max(1, req.copies),
                "filament_g": round(g * max(1, req.copies), 2),
            }

            if g == 0:
                resp["debug_header"] = gcode.splitlines()[:60]
                resp["debug_e_length_mm"] = _extrusion_length_mm_from_e_axis(gcode)


            return resp

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
