#!/usr/bin/env python3
"""
東京都（都営バス＋gtfs-data.jp東京都フィード）の道路沿い路線ポリライン（shapes.txt / stop_times.txt）を抽出し、
軽量化（Douglas-Peucker 間引き）して Web/data/routes/pref_13.json を生成するスクリプト。
"""

import csv
import io
import json
import math
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


def _perp_dist(pt: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    if a == b:
        return math.hypot(pt[0] - a[0], pt[1] - a[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0.0, min(1.0, ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    proj = (a[0] + t * dx, a[1] + t * dy)
    return math.hypot(pt[0] - proj[0], pt[1] - proj[1])


def simplify_dp(pts: List[Tuple[float, float]], eps: float = 0.00008) -> List[Tuple[float, float]]:
    if len(pts) <= 2:
        return pts
    # iterative Douglas-Peucker (recursion-safe)
    keep = [False] * len(pts)
    keep[0] = True
    keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        s, e = stack.pop()
        if e - s <= 1:
            continue
        max_d = -1.0
        idx = -1
        a, b = pts[s], pts[e]
        for i in range(s + 1, e):
            d = _perp_dist(pts[i], a, b)
            if d > max_d:
                max_d = d
                idx = i
        if max_d > eps and idx != -1:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return [pts[i] for i, k in enumerate(keep) if k]


def read_csv_from_zip(zf: zipfile.ZipFile, name: str) -> List[Dict[str, str]]:
    if name not in zf.namelist():
        return []
    raw = zf.read(name).decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(raw)))


# 都営バスの路線色バリエーション（視認しやすい都バスグリーン系＋青緑系）
TOEI_PALETTE = ["#009e41", "#00875a", "#0d9488", "#0284c7", "#059669", "#16a34a"]


def extract_routes_from_gtfs(
    zip_path: Path,
    op_type: str,
    op_short: str,
    op_name_default: str,
    id_prefix: str,
    eps: float = 0.00008,
) -> List[Dict]:
    if not zip_path.exists():
        return []
    with zipfile.ZipFile(zip_path, "r") as zf:
        routes_rows = read_csv_from_zip(zf, "routes.txt")
        trips_rows = read_csv_from_zip(zf, "trips.txt")
        shapes_rows = read_csv_from_zip(zf, "shapes.txt")

        route_info: Dict[str, Dict[str, str]] = {}
        for r in routes_rows:
            rid = r.get("route_id", "")
            if not rid:
                continue
            # GTFS route_type=4（高速船・フェリー）はバス路線図から除外
            if (r.get("route_type") or "3").strip() == "4":
                continue
            rname = (r.get("route_short_name") or r.get("route_long_name") or rid).strip()
            rlong = (r.get("route_long_name") or "").strip()
            route_info[rid] = {"name": rname, "long": rlong}

        # route_id -> shape_id 出現回数
        route_shapes: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for t in trips_rows:
            rid = t.get("route_id", "")
            sid = (t.get("shape_id") or "").strip()
            if rid and rid in route_info and sid:
                route_shapes[rid][sid] += 1

        # shapes.txt の読み込み
        shapes_pts: Dict[str, List[Tuple[int, float, float]]] = defaultdict(list)
        for s in shapes_rows:
            sid = (s.get("shape_id") or "").strip()
            if not sid:
                continue
            try:
                lat = float(s["shape_pt_lat"])
                lng = float(s["shape_pt_lon"])
                seq = int(s.get("shape_pt_sequence", 0))
            except (KeyError, ValueError):
                continue
            shapes_pts[sid].append((seq, lat, lng))

        out_routes: List[Dict] = []

        if shapes_pts and route_shapes:
            used_shape_ids = set()
            for r_idx, (rid, sid_counts) in enumerate(route_shapes.items()):
                # 各系統で代表的な上位1〜2本のshapeだけ採用（重複描画による重さを防止）
                top_sids = sorted(sid_counts.keys(), key=lambda x: (-sid_counts[x], -len(shapes_pts.get(x, []))))[:2]
                r_meta = route_info.get(rid, {"name": rid, "long": ""})
                color = (
                    TOEI_PALETTE[r_idx % len(TOEI_PALETTE)]
                    if op_short == "都営"
                    else "#d97706" if op_type == "community" else "#1d4ed8"
                )
                for s_i, sid in enumerate(top_sids):
                    if sid in used_shape_ids or sid not in shapes_pts:
                        continue
                    used_shape_ids.add(sid)
                    pts_sorted = [(lat, lng) for _, lat, lng in sorted(shapes_pts[sid], key=lambda x: x[0])]
                    simp = simplify_dp(pts_sorted, eps=eps)
                    if len(simp) < 2:
                        continue
                    lats = [p[0] for p in simp]
                    lngs = [p[1] for p in simp]
                    out_routes.append({
                        "id": f"{id_prefix}{rid}_{s_i}",
                        "name": r_meta["name"],
                        "long": r_meta["long"],
                        "op_type": op_type,
                        "op_short": op_short,
                        "op_name": op_name_default,
                        "color": color,
                        "bbox": [round(min(lats), 5), round(min(lngs), 5), round(max(lats), 5), round(max(lngs), 5)],
                        "pts": [[round(lat, 5), round(lng, 5)] for lat, lng in simp],
                    })
        else:
            # shapes.txt がない事業者は stop_times.txt + stops.txt の停車順で簡易ライン生成
            stops_rows = read_csv_from_zip(zf, "stops.txt")
            stop_coords: Dict[str, Tuple[float, float]] = {}
            for st in stops_rows:
                try:
                    stop_coords[st["stop_id"]] = (float(st["stop_lat"]), float(st["stop_lon"]))
                except (KeyError, ValueError):
                    pass
            stop_times_rows = read_csv_from_zip(zf, "stop_times.txt")
            trip_to_route = {
                t["trip_id"]: t.get("route_id", "")
                for t in trips_rows
                if t.get("trip_id") and t.get("route_id", "") in route_info
            }
            trip_stops: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
            for st in stop_times_rows:
                tid = st.get("trip_id", "")
                if tid in trip_to_route:
                    try:
                        trip_stops[tid].append((int(st.get("stop_sequence", 0)), st["stop_id"]))
                    except (KeyError, ValueError):
                        pass
            best_trip_for_route: Dict[str, List[Tuple[int, str]]] = {}
            for tid, seq_list in trip_stops.items():
                rid = trip_to_route[tid]
                if rid not in best_trip_for_route or len(seq_list) > len(best_trip_for_route[rid]):
                    best_trip_for_route[rid] = seq_list

            for r_idx, (rid, seq_list) in enumerate(best_trip_for_route.items()):
                pts = [stop_coords[sid] for _, sid in sorted(seq_list) if sid in stop_coords]
                if len(pts) < 2:
                    continue
                r_meta = route_info.get(rid, {"name": rid, "long": ""})
                color = "#d97706" if op_type == "community" else "#0d9488"
                lats = [p[0] for p in pts]
                lngs = [p[1] for p in pts]
                out_routes.append({
                    "id": f"{id_prefix}{rid}_st",
                    "name": r_meta["name"],
                    "long": r_meta["long"],
                    "op_type": op_type,
                    "op_short": op_short,
                    "op_name": op_name_default,
                    "color": color,
                    "bbox": [round(min(lats), 5), round(min(lngs), 5), round(max(lats), 5), round(max(lngs), 5)],
                    "pts": [[round(lat, 5), round(lng, 5)] for lat, lng in pts],
                })

        return out_routes


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    raw_dir = root / "data-pipeline" / "raw"
    out_path = root / "ios-app" / "BusTimetableNavi" / "Web" / "data" / "routes" / "pref_13.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_routes: List[Dict] = []

    # 1. 都営バス
    toei_zip = raw_dir / "ToeiBus-GTFS.zip"
    toei_routes = extract_routes_from_gtfs(toei_zip, "public", "都営", "東京都交通局（都営バス）", "toei_")
    all_routes.extend(toei_routes)
    print(f"Toei routes extracted: {len(toei_routes)}")

    # 2. 東京都のコミュニティバス・市営/村営バス（gtfs_repo/pref13_*.zip）
    repo_dir = raw_dir / "gtfs_repo"
    if repo_dir.exists():
        for idx, zp in enumerate(sorted(repo_dir.glob("pref13_*.zip"))):
            fname = zp.name
            is_village = any(k in fname.lower() for k in ["ogasawara", "kozushima"])
            op_type = "public" if is_village else "community"
            op_short = "公営" if is_village else "コミュ"
            rlist = extract_routes_from_gtfs(zp, op_type, op_short, zp.stem, f"p13_{idx}_")
            all_routes.extend(rlist)
            print(f"  {zp.name}: {len(rlist)} routes")

    payload = {"pref": 13, "name": "東京都", "routes": all_routes}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    size_kb = round(out_path.stat().st_size / 1024, 1)
    print(f"TOKYO_ROUTES_OK count={len(all_routes)} size_kb={size_kb} path={out_path}")


if __name__ == "__main__":
    main()
