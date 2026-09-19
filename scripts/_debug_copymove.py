import sys
sys.path.insert(0, ".")

import cv2
import numpy as np

from app.services.tampering.ela import analyze_ela
from app.services.tampering.noise import analyze_noise
from app.services.tampering.compression import analyze_compression
from app.services.tampering.engine import run_tampering_analysis


def _pristine_document(w=900, h=600, seed=7):
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 235, dtype=np.uint8)
    img[:, :, 0] = img[:, :, 0] - 10
    for i in range(14):
        y = 40 + i * 36
        cv2.putText(img, f"Printed line {i} THE QUICK BROWN FOX 0123456789",
                    (40, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (35, 35, 35), 1, cv2.LINE_AA)
    cv2.rectangle(img, (w - 260, 40), (w - 60, 240), (200, 200, 200), -1)
    cv2.circle(img, (w - 160, 120), 35, (150, 150, 150), -1)
    grain = rng.normal(0, 2.0, (h, w)).astype(np.float32)
    img = np.clip(img.astype(np.float32) + grain[:, :, None], 0, 255).astype(np.uint8)
    return img


pristine = _pristine_document()
h, w = pristine.shape[:2]
edited = pristine.copy()
rng = np.random.default_rng(107)
patch = rng.normal(150, 28, (120, 200, 3)).astype(np.float32)
patch = np.clip(patch, 0, 255).astype(np.uint8)
edited[300:420, 60:260] = patch

cv2.imwrite("data/_dbg_pristine.jpg", pristine, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
cv2.imwrite("data/_dbg_edited.jpg", edited, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

for name in ("data/_dbg_pristine.jpg", "data/_dbg_edited.jpg"):
    img = cv2.imread(name)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    print(f"=== {name} ===")
    n = analyze_noise(gray.astype(np.float32) / 255.0)
    print(f"  noise: fired={n['fired']} sev={n.get('severity')} conf={n.get('confidence')} "
          f"median={n.get('global_median_noise')} usable={n.get('usable_fraction')}")
    print(f"    outliers={len(n.get('outliers') or [])}")
    if n.get("outliers"):
        print(f"    first={n['outliers'][0]}")
    e = analyze_ela(img, reference_quality=90)
    print(f"  ela: mean={e.mean_error:.2f} bright={e.bright_fraction:.3f} gate={e.global_gate}")
    p = run_tampering_analysis(name)
    print(f"  verdict={p['verdict']} risk={p['risk_score']}")
    for ind in p["indicators"]:
        print(f"    ind: {ind['type']} {ind['severity']} conf={ind['confidence']}")
    print()
