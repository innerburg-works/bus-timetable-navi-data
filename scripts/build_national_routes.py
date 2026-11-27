#!/usr/bin/env python3
"""
全国40都道府県（national_gtfs/*.zip ＋ 都営バス）の道路沿い路線ポリラインを一括抽出し、
national-data-site/data/routes/pref_XX.json を生成するスクリプト。
"""

import argparse
import concurrent.futures
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_tokyo_routes import extract_routes_from_gtfs
from fetch_pref_gtfs import infer_operator_meta


def _extract_one(task: Tuple[int, int, Path]) -> Tuple[int, List[Dict]]:
    pref, idx, zp = task
    stem = zp.stem
    op_type, op_short, _ = infer_operator_meta(stem, stem)
    # ファイル名に市営/村営/町営/都営/交通局がなくても、公営系キーワードを補完
    low = stem.lower()
    if any(k in low for k in ["shiei", "toei", "kotsu", "municipal", "citybus", "village"]):
        op_type, op_short = "public", "公営"
    elif any(k in low for k in ["comm", "community", "abus", "kururin", "sakura", "kunikko", "aoyagikko", "wagon", "loop"]):
        op_type, op_short = "community", "コミュ"
    try:
        routes = extract_routes_from_gtfs(
            zip_path=zp,
            op_type=op_type,
            op_short=op_short,
            op_name_default=stem,
            id_prefix=f"r_p{pref:02d}_{idx:03d}_",
            eps=0.00010,  # 約10m精度で軽量化
        )
        return (pref, routes)
    except Exception:
        return (pref, [])


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="全国47都道府県 路線図ポリラインJSON一括生成")
    parser.add_argument("--raw-dir", type=Path, default=root / "data-pipeline" / "raw" / "national_gtfs")
    parser.add_argument("--toei-zip", type=Path, default=root / "data-pipeline" / "raw" / "ToeiBus-GTFS.zip")
    parser.add_argument("--out-routes-dir", type=Path, default=root / "national-data-site" / "data" / "routes")
    parser.add_argument("--catalog-file", type=Path, help="事前取得済みのGTFS一覧JSON（_slotと厳密一致させる）")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    args.out_routes_dir.mkdir(parents=True, exist_ok=True)
    tasks: List[Tuple[int, int, Path]] = []
    if args.catalog_file and args.catalog_file.exists():
        raw_cat = json.loads(args.catalog_file.read_text(encoding="utf-8"))
        items = raw_cat.get("body", []) if isinstance(raw_cat, dict) else raw_cat
        for idx, it in enumerate(items):
            try:
                pref = int(it.get("feed_pref_id"))
            except (TypeError, ValueError):
                continue
            if not (1 <= pref <= 47):
                continue
            slot = int(it.get("_slot", idx))
            feed_id = (it.get("feed_id") or f"f{slot}").strip().replace("/", "_")
            zp = args.raw_dir / f"p{pref:02d}_{slot:03d}_{feed_id}.zip"
            if zp.exists():
                tasks.append((pref, slot, zp))
    else:
        for fallback_idx, zp in enumerate(sorted(args.raw_dir.glob("p*_*.zip"))):
            m = re.match(r"^p(\d{2})_(\d{3})_", zp.name)
            if m:
                tasks.append((int(m.group(1)), int(m.group(2)), zp))
                continue
            m2 = re.match(r"^p(\d{2})_", zp.name)
            if m2:
                tasks.append((int(m2.group(1)), fallback_idx, zp))

    routes_by_pref: Dict[int, List[Dict]] = defaultdict(list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for pref, rlist in ex.map(_extract_one, tasks):
            if rlist:
                routes_by_pref[pref].extend(rlist)

    # 東京都(13)に都営バスを追加
    if args.toei_zip.exists():
        toei_routes = extract_routes_from_gtfs(
            zip_path=args.toei_zip,
            op_type="public",
            op_short="都営",
            op_name_default="東京都交通局（都営バス）",
            id_prefix="toei_",
            eps=0.00008,
        )
        routes_by_pref[13].extend(toei_routes)

    total_routes = 0
    total_bytes = 0
    max_kb = 0.0
    for pref in sorted(routes_by_pref.keys()):
        rlist = routes_by_pref[pref]
        if not rlist:
            continue
        out_file = args.out_routes_dir / f"pref_{pref:02d}.json"
        out_file.write_text(
            json.dumps({"pref": pref, "routes": rlist}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        sz = out_file.stat().st_size
        total_routes += len(rlist)
        total_bytes += sz
        max_kb = max(max_kb, sz / 1024.0)

    print(
        "NATIONAL_ROUTES_OK",
        json.dumps({
            "prefectures": len(routes_by_pref),
            "total_routes": total_routes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "max_file_kb": round(max_kb, 1),
        }),
    )


if __name__ == "__main__":
    main()
