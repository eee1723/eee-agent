"""Real headless-browser smoke for the HTML sketch quality gate.

Runs with the project Python, not hython. It renders a small static Three.js
scene from a pinned CDN URL through the production ChromeSketchRenderer and
requires the strict PNG/non-blank inspection to pass. No provider, Runtime,
Houdini, user profile, or persistent output directory is used.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from eee_agent.sketch.chrome import ChromeSketchRenderer, resolve_browser


_HTML = """\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#182033}
    canvas{display:block}
  </style>
  <script type="importmap">
    {"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js"}}
  </script>
</head>
<body>
<script type="module">
import * as THREE from "three";
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x182033);
const camera = new THREE.PerspectiveCamera(35, 1440/900, 0.1, 100);
camera.position.set(4, 3, 5);
camera.lookAt(0, 0.4, 0);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(1440, 900);
renderer.setPixelRatio(1);
document.body.appendChild(renderer.domElement);
const tabletop = new THREE.Mesh(
  new THREE.BoxGeometry(2.8, 0.25, 1.4),
  new THREE.MeshStandardMaterial({color:0xd98b3a,roughness:0.55})
);
tabletop.position.y = 1.5;
tabletop.name = "tabletop";
scene.add(tabletop);
for (const [x,z,name] of [
  [-1.15,-0.5,"leg_fl"],[1.15,-0.5,"leg_fr"],
  [-1.15,0.5,"leg_bl"],[1.15,0.5,"leg_br"]
]) {
  const leg = new THREE.Mesh(
    new THREE.BoxGeometry(0.18, 1.5, 0.18),
    new THREE.MeshStandardMaterial({color:0x4b2f22,roughness:0.7})
  );
  leg.position.set(x,0.75,z);
  leg.name = name;
  scene.add(leg);
}
scene.add(new THREE.HemisphereLight(0xffffff,0x34405c,2.2));
const key = new THREE.DirectionalLight(0xffffff,3.0);
key.position.set(4,6,3);
scene.add(key);
const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(12,12),
  new THREE.MeshStandardMaterial({color:0x69758c,roughness:0.9})
);
floor.rotation.x = -Math.PI/2;
floor.name = "floor";
scene.add(floor);
renderer.render(scene,camera);
</script>
</body>
</html>
"""


async def _run() -> dict[str, object]:
    browser = resolve_browser()
    if browser is None:
        raise RuntimeError("Chrome/Edge is unavailable")
    with tempfile.TemporaryDirectory(prefix="eee-sketch-smoke-") as raw:
        renderer = ChromeSketchRenderer(Path(raw))
        result = dict(
            await renderer.render_sketch(
                html_content=_HTML,
                sketch_name="threejs-table-smoke",
            )
        )
        if result.get("ok") is not True:
            code = result.get("code", "unknown")
            raise RuntimeError(f"render failed closed ({code})")
        evidence = {
            "browser": Path(browser).name,
            "image_bytes": result["image_bytes"],
            "image_width": result["image_width"],
            "image_height": result["image_height"],
            "pixel_channel_span": result["pixel_channel_span"],
            "png_nonblank": True,
        }
        return evidence


def main() -> int:
    try:
        evidence = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - bounded smoke failure
        print(
            f"SKETCH CHROME SMOKE FAIL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print("SKETCH CHROME SMOKE OK")
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
