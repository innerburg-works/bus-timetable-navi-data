#!/usr/bin/env python3
"""
GTFSデータリポジトリ (api.gtfs-data.jp/v2) から指定都道府県の全公開GTFS-JPを一括取得し、
「バス時刻表ナビ」アプリ用の単一JS/JSONへ統合変換するスクリプト（全国47都道府県対応）。

使用例（東京都: pref=13）:
  python3 data-pipeline/fetch_pref_gtfs.py \
    --pref 13 \
    --js-var TOKYO_EXTRA_GTFS \
    --output ios-app/BusTimetableNavi/Web/tokyo_extra_gtfs_data.js
"""

import argparse
import gzip
import json
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gtfs_to_app_data import convert_gtfs


API_BASE = "https://api.gtfs-data.jp/v2/files"

# GTFSデータリポジトリ未登録だが東京都オープンデータカタログ等で認証不要公開されている公式GTFS-JP
EXTRA_DIRECT_FEEDS_BY_PREF: Dict[int, List[Dict[str, str]]] = {
    13: [
        {
            "organization_name": "板橋区",
            "feed_name": "コミュニティバス「りんりん号」",
            "feed_id": "itabashi_rinringo",
            "file_url": "https://www.city.itabashi.tokyo.jp/_res/projects/default_project/_page_/001/006/732/rinringo_gtfs_20260401asshuku.zip",
            "feed_detail_url": "https://www.city.itabashi.tokyo.jp/bousai/koutsu/bus/1006732.html",
            "feed_license_id": "CC BY 4.0",
        }
    ]
}


def fetch_json(url: str) -> Dict:
    req = urllib.request.Request(url, headers={"User-Agent": "BusTimetableNavi/1.0"})
    with urllib.request.urlopen(req, timeout=20) as res:
        raw = res.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def download_file(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "BusTimetableNavi/1.0"})
    with urllib.request.urlopen(req, timeout=30) as res:
        dest.write_bytes(res.read())


def infer_operator_meta(org_name: str, feed_name: str) -> Tuple[str, str, str]:
    """組織名・フィード名から (operator_type, operator_short, badge_color) を自動判定"""
    combined = f"{org_name} {feed_name}"
    if any(k in combined for k in ["村営", "市営", "都営", "町営", "公営", "交通局"]):
        return ("public", "公営", "#15803d")
    if any(k in combined for k in ["コミュニティ", "デマンド", "ワゴン", "Aバス", "さくら", "くにっこ", "くるりん", "ハチ公", "Bーぐる", "ちぃばす", "ムーバス"]):
        return ("community", "コミュ", "#d97706")
    # 自治体が直接公開している場合はコミュニティバス扱い、それ以外は私営
    if any(org_name.endswith(s) for s in ["区", "市", "町", "村"]):
        return ("community", "コミュ", "#d97706")
    return ("private", "私営", "#1d4ed8")


def main() -> None:
    parser = argparse.ArgumentParser(description="GTFSデータリポジトリから都道府県別に一括取得・変換")
    parser.add_argument("--pref", type=int, default=13, help="都道府県コード（東京都=13、全国1〜47）")
    parser.add_argument("--raw-dir", type=Path, default=Path(__file__).resolve().parent / "raw" / "gtfs_repo")
    parser.add_argument("--js-var", default="TOKYO_EXTRA_GTFS", help="出力JSのグローバル変数名")
    parser.add_argument("--output", required=True, type=Path, help="出力先ファイルパス")
    args = parser.parse_args()

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    resp = fetch_json(f"{API_BASE}?pref={args.pref}")
    items = list(resp.get("body", resp) if isinstance(resp, dict) else resp)
    items.extend(EXTRA_DIRECT_FEEDS_BY_PREF.get(args.pref, []))

    all_poles: List[Dict] = []
    feeds_summary: List[Dict] = []

    for idx, item in enumerate(items):
        org_name = item.get("organization_name") or item.get("org_name") or f"org_{idx}"
        feed_name = item.get("feed_name") or ""
        feed_id = item.get("feed_id") or f"feed_{idx}"
        file_url = item.get("file_url")
        if not file_url:
            continue

        safe_name = f"pref{args.pref}_{idx}_{feed_id}.zip".replace("/", "_")
        zip_path = args.raw_dir / safe_name

        try:
            download_file(file_url, zip_path)
            op_type, op_short, badge_color = infer_operator_meta(org_name, feed_name)
            converted = convert_gtfs(
                gtfs_zip_path=zip_path,
                operator_type=op_type,
                operator_short=op_short,
                supports_realtime=False,
                default_timetable_url=item.get("feed_detail_url") or "https://gtfs-data.jp/",
                badge_color=badge_color,
            )
            prefix = f"repo_p{args.pref}_{idx}_"
            for p in converted["poles"]:
                # 他事業者との pole_id 衝突を防止
                p["pole_id"] = prefix + p["pole_id"]
                p["operator_name"] = f"{org_name}（{feed_name}）" if feed_name and feed_name not in org_name else org_name
                for r in p["routes"]:
                    r["route_id"] = prefix + r["route_id"]
                    if r.get("prev_pole_id"):
                        r["prev_pole_id"] = prefix + r["prev_pole_id"]
                    if r.get("next_pole_id"):
                        r["next_pole_id"] = prefix + r["next_pole_id"]
                all_poles.append(p)

            feeds_summary.append({
                "org_name": org_name,
                "feed_name": feed_name,
                "operator_type": op_type,
                "license": item.get("feed_license_id"),
                "pole_count": converted["pole_count"],
            })
            print(f"[OK] {org_name} / {feed_name}: {converted['pole_count']} poles")
        except Exception as exc:
            print(f"[SKIP] {org_name} / {feed_name}: {exc}")

    payload = {
        "schema_version": "1.0",
        "pref_code": args.pref,
        "feed_count": len(feeds_summary),
        "pole_count": len(all_poles),
        "feeds": feeds_summary,
        "poles": all_poles,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if args.js_var:
        args.output.write_text(f"window.{args.js_var} = {serialized};\n", encoding="utf-8")
    else:
        args.output.write_text(serialized, encoding="utf-8")

    print(f"DONE: {len(feeds_summary)} feeds, {len(all_poles)} poles -> {args.output}")


if __name__ == "__main__":
    main()
