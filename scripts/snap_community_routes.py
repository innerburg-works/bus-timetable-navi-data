#!/usr/bin/env python3
"""
shapes.txt が欠如して直線接続（id末尾 _st）になっているコミュニティバス路線を、
ビルド時に OSRM 公開道路ルーティングで実道路沿いポリラインへスナップし、
Douglas-Peucker 間引きで軽量化したうえで routes/pref_XX.json へ反映するスクリプト。

アプリ側の実行時コスト・通信量はゼロ増（事前計算済み静的JSONのみ）。
"""

import argparse
import json
import math
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from build_tokyo_routes import simplify_dp


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(x), math.sqrt(max(0.0, 1.0 - x)))


def polyline_len_m(pts: List[Tuple[float, float]]) -> float:
    return sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def osrm_snap_chunk(pts: List[Tuple[float, float]], timeout: int = 20) -> Optional[List[Tuple[float, float]]]:
    if len(pts) < 2:
        return None
    coords = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in pts)
    url = f"https://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson&continue_straight=true"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BusTimetableNavi/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                data = json.loads(res.read().decode("utf-8"))
            if data.get("code") != "Ok":
                return None
            routes = data.get("routes") or []
            if not routes:
                return None
            geom = routes[0].get("geometry", {}).get("coordinates", [])
            if len(geom) < 2:
                return None
            snapped = [(float(lat), float(lng)) for lng, lat in geom]
            # 極端な迂回（直線距離の3.5倍超）が起きたチャンクは元座標を維持して破綻を防止
            orig_len = max(polyline_len_m(pts), 100.0)
            snap_len = polyline_len_m(snapped)
            if snap_len > orig_len * 3.5 + 1500.0:
                return None
            return snapped
        except Exception:
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
    return None


def snap_route_pts(raw_pts: List[List[float]], eps: float = 0.00010) -> Optional[List[List[float]]]:
    # 連続する同一座標を除去
    pts: List[Tuple[float, float]] = []
    for p in raw_pts:
        if len(p) < 2:
            continue
        t = (float(p[0]), float(p[1]))
        if not pts or haversine_m(pts[-1], t) >= 8.0:
            pts.append(t)
    if len(pts) < 2:
        return None

    # OSRM URL長制限・安定性のため 28 停留所ずつ重なり1点で分割スナップ
    CHUNK = 28
    merged: List[Tuple[float, float]] = []
    any_snapped = False
    i = 0
    while i < len(pts) - 1:
        end = min(len(pts), i + CHUNK)
        chunk = pts[i:end]
        snapped = osrm_snap_chunk(chunk)
        seg = snapped if snapped else chunk
        if snapped:
            any_snapped = True
        if merged and seg:
            merged.extend(seg[1:])
        else:
            merged.extend(seg)
        if end >= len(pts):
            break
        i = end - 1
        time.sleep(0.15)

    if not any_snapped:
        return None
    simp = simplify_dp(merged, eps=eps)
    if len(simp) < 2:
        return None
    return [[round(lat, 5), round(lng, 5)] for lat, lng in simp]


def process_pref_file(path: Path, eps: float = 0.00010, delay_sec: float = 0.15) -> Tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    routes = data.get("routes", [])
    targets = [r for r in routes if r.get("op_type") == "community" and str(r.get("id", "")).endswith("_st")]
    if not targets:
        return 0, 0
    snapped_ok = 0
    for r in targets:
        new_pts = snap_route_pts(r.get("pts", []), eps=eps)
        if new_pts:
            r["pts"] = new_pts
            lats = [p[0] for p in new_pts]
            lngs = [p[1] for p in new_pts]
            r["bbox"] = [round(min(lats), 5), round(min(lngs), 5), round(max(lats), 5), round(max(lngs), 5)]
            r["id"] = str(r["id"])[:-3] + "_osrm"
            snapped_ok += 1
        time.sleep(delay_sec)
    if snapped_ok > 0:
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return len(targets), snapped_ok


def main() -> None:
    root = SCRIPT_DIR.parent.parent
    parser = argparse.ArgumentParser(description="shapes.txt欠如のコミュニティバス路線をOSRM道路沿い化")
    parser.add_argument("--routes-dir", type=Path, default=root / "national-data-site" / "data" / "routes")
    parser.add_argument("--prefs", nargs="*", type=int, help="対象都道府県コード（省略時は全県）")
    parser.add_argument("--eps", type=float, default=0.00010)
    parser.add_argument("--sync-bundled-tokyo", action="store_true")
    args = parser.parse_args()

    files = sorted(args.routes_dir.glob("pref_*.json"))
    if args.prefs:
        allow = {f"pref_{p:02d}.json" for p in args.prefs}
        files = [f for f in files if f.name in allow]

    total_targets = 0
    total_ok = 0
    for f in files:
        t_cnt, ok_cnt = process_pref_file(f, eps=args.eps)
        if t_cnt > 0:
            sz_kb = f.stat().st_size / 1024.0
            print(f"[SNAP] {f.name}: {ok_cnt}/{t_cnt} snapped (file={sz_kb:.1f}KB)", flush=True)
            total_targets += t_cnt
            total_ok += ok_cnt

    if args.sync_bundled_tokyo:
        src = args.routes_dir / "pref_13.json"
        dst = root / "ios-app" / "BusTimetableNavi" / "Web" / "data" / "routes" / "pref_13.json"
        if src.exists() and dst.parent.exists():
            shutil.copy2(src, dst)
            print(f"Synced bundled routes/pref_13.json ({dst.stat().st_size/1024:.1f}KB)", flush=True)

    print(f"SNAP_ALL_DONE total_snapped={total_ok}/{total_targets}", flush=True)


if __name__ == "__main__":
    main()
