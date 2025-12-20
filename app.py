from fastapi import Body
from typing import Union
import re, os, tempfile, subprocess
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

app = FastAPI()

class Req(BaseModel):
    file_url: HttpUrl
    material: str = "PLA"
    quality: str = "standard"
    supports: bool = False
    copies: int = 1

import re

def parse_filament_g(gcode: str) -> float:
    patterns = [
        r"filament used \\[g\\] *= *([0-9.]+)",
        r"Filament used: *([0-9.]+) *g",
    ]

    for line in gcode.splitlines():
        for p in patterns:
            m = re.search(p, line)
            if m:
                return float(m.group(1))

    return 0.0


def parse_time_seconds(txt: str) -> int:
    m = re.search(r"estimated printing time.*=\s*([0-9hms\s]+)", txt, re.IGNORECASE)
    if not m:
        return -1
    s = m.group(1)
    h = int(re.search(r"(\d+)\s*h", s).group(1)) if re.search(r"(\d+)\s*h", s) else 0
    m_ = int(re.search(r"(\d+)\s*m", s).group(1)) if re.search(r"(\d+)\s*m", s) else 0
    se = int(re.search(r"(\d+)\s*s", s).group(1)) if re.search(r"(\d+)\s*s", s) else 0
    return h*3600 + m_*60 + se

def download(url: str, out_path: str):
    r = requests.get(url, timeout=90)
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

    # PrusaSlicer CLI: supports are enabled by adding --support-material (no value).
    # If supports=False, we simply do NOT include the flag. :contentReference[oaicite:1]{index=1}
    if supports:
        cmd += ["--support-material"]

    cmd += [
        "--export-gcode",
        f"--output={out_gcode}",
        model_path
    ]

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[:1200])



from fastapi import Request

@app.post("/estimate")
def estimate(payload: Union[Req, str] = Body(...)):
    try:
        # Allow sending just a URL string
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
            g = parse_filament_g(gcode)
            t = parse_time_seconds(gcode)

            if g < 0 or t < 0:
                raise RuntimeError("Failed to read slicer output")

            return {
                "print_time_seconds": t * max(1, req.copies),
                "filament_g": round(g * max(1, req.copies), 2)
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

        raise
    except Exception as e:
        raise HTTPException(500, str(e))
