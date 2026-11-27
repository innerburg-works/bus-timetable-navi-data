#!/usr/bin/env python3
"""
全国47都道府県対応：2層データ分割（シャード化）ビルドスクリプト

11MB超の単一JS一括ロードを廃止し、以下の2層構造へ分割します:
  1. Web/data/manifest.json
       都道府県ごとの緯度経度バウンディングボックスとインデックスパスを管理
  2. Web/data/index/pref_<CODE>.json
       地図ピン描画に必要な最小情報のみ（時刻表なし・約1/25サイズ）
  3. Web/data/timetables/pref_<CODE>/b<00..31>.json
       pole_id の安定ハッシュで32分割した詳細・時刻表チャンク（タップ時にオンデマンド読込）

これにより、全国47都道府県へ拡張してもアプリ起動時のメモリ消費・読込時間を最小限に保てます。
"""

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


NUM_BUCKETS = 32


def bucket_for_pole(pole_id: str) -> str:
    h = int(hashlib.md5(pole_id.encode("utf-8")).hexdigest()[:8], 16)
    return f"b{h % NUM_BUCKETS:02d}"


def load_poles_from_js_or_json(path: Path) -> List[Dict]:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("window."):
        m = re.match(r"^window\.[A-Za-z0-9_]+\s*=\s*", text)
        if m:
            text = text[m.end():].rstrip(";\n\r ")
    data = json.loads(text)
    return data.get("poles", [])


def build_pref_shards(pref_code: int, pref_name: str, input_files: List[Path], out_data_dir: Path) -> Dict:
    poles_by_id: Dict[str, Dict] = {}
    for f in input_files:
        if not f.exists():
            continue
        for p in load_poles_from_js_or_json(f):
            poles_by_id[p["pole_id"]] = p

    poles = list(poles_by_id.values())
    index_entries: List[Dict] = []
    buckets: Dict[str, Dict[str, Dict]] = defaultdict(dict)

    min_lat, max_lat = 90.0, -90.0
    min_lng, max_lng = 180.0, -180.0

    for p in poles:
        lat, lng = float(p["lat"]), float(p["lng"])
        if lat == 0 or lng == 0:
            continue
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lng = min(min_lng, lng)
        max_lng = max(max_lng, lng)

        bname = bucket_for_pole(p["pole_id"])
        routes = p.get("routes") or []
        if len(routes) >= 2:
            sub_label = f"{len(routes)}系統"
        elif len(routes) == 1:
            r0 = routes[0]
            dest = r0.get("destination_stop_name") or ""
            sub_label = f"{r0.get('route_name', '')} {dest}".strip()
        else:
            sub_label = ""

        index_entries.append({
            "pole_id": p["pole_id"],
            "pref": pref_code,
            "b": bname,
            "stop_name": p["stop_name"],
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "arrow": p.get("direction_arrow", "↗"),
            "op_type": p.get("operator_type", "public"),
            "op_short": p.get("operator_short", "公営"),
            "rt": bool(p.get("supports_realtime")),
            "rc": len(routes),
            "sub": sub_label,
        })
        buckets[bname][p["pole_id"]] = p

    index_dir = out_data_dir / "index"
    tt_dir = out_data_dir / "timetables" / f"pref_{pref_code:02d}"
    index_dir.mkdir(parents=True, exist_ok=True)
    tt_dir.mkdir(parents=True, exist_ok=True)

    index_path = index_dir / f"pref_{pref_code:02d}.json"
    index_path.write_text(
        json.dumps({"pref": pref_code, "name": pref_name, "poles": index_entries}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    for bname, bucket_poles in buckets.items():
        (tt_dir / f"{bname}.json").write_text(
            json.dumps(bucket_poles, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    # manifest.json の更新（既存都道府県を保持しつつマージ）
    manifest_path = out_data_dir / "manifest.json"
    manifest = {"schema_version": "2.0", "prefs": []}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    pref_entry = {
        "pref": pref_code,
        "name": pref_name,
        "pole_count": len(index_entries),
        "index_url": f"data/index/pref_{pref_code:02d}.json",
        "timetable_base": f"data/timetables/pref_{pref_code:02d}",
        "bounds": {
            "min_lat": round(min_lat, 5),
            "max_lat": round(max_lat, 5),
            "min_lng": round(min_lng, 5),
            "max_lng": round(max_lng, 5),
        },
    }
    manifest["prefs"] = [x for x in manifest.get("prefs", []) if x.get("pref") != pref_code]
    manifest["prefs"].append(pref_entry)
    manifest["prefs"].sort(key=lambda x: x["pref"])
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "pref": pref_code,
        "pole_count": len(index_entries),
        "index_bytes": index_path.stat().st_size,
        "buckets": len(buckets),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="全国対応2層シャードデータ生成")
    parser.add_argument("--pref", type=int, default=13)
    parser.add_argument("--pref-name", default="東京都")
    parser.add_argument("--inputs", nargs="+", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    res = build_pref_shards(args.pref, args.pref_name, args.inputs, args.out_dir)
    print("SHARD_BUILD_OK", json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
