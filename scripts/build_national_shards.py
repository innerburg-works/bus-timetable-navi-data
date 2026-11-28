#!/usr/bin/env python3
"""
全国47都道府県（GTFSデータリポジトリ全公開フィード＋都営バス）一括取得＆2層シャードビルドスクリプト

出力先（デフォルト: national-data-site/data/）:
  - manifest.json
  - index/pref_01.json .. pref_47.json
  - timetables/pref_XX/b00.json .. b31.json
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_sharded_data import load_poles_from_js_or_json
from fetch_pref_gtfs import infer_operator_meta
from gtfs_to_app_data import convert_gtfs
from toei_trip_calls import attach_community_calls

API_URL = "https://api.gtfs-data.jp/v2/files"
TOEI_URL = "https://api-public.odpt.org/api/v4/files/Toei/data/ToeiBus-GTFS.zip"

PREF_NAMES = {
    1: "北海道", 2: "青森県", 3: "岩手県", 4: "宮城県", 5: "秋田県", 6: "山形県", 7: "福島県",
    8: "茨城県", 9: "栃木県", 10: "群馬県", 11: "埼玉県", 12: "千葉県", 13: "東京都", 14: "神奈川県",
    15: "新潟県", 16: "富山県", 17: "石川県", 18: "福井県", 19: "山梨県", 20: "長野県", 21: "岐阜県",
    22: "静岡県", 23: "愛知県", 24: "三重県", 25: "滋賀県", 26: "京都府", 27: "大阪府", 28: "兵庫県",
    29: "奈良県", 30: "和歌山県", 31: "鳥取県", 32: "島根県", 33: "岡山県", 34: "広島県", 35: "山口県",
    36: "徳島県", 37: "香川県", 38: "愛媛県", 39: "高知県", 40: "福岡県", 41: "佐賀県", 42: "長崎県",
    43: "熊本県", 44: "大分県", 45: "宮崎県", 46: "鹿児島県", 47: "沖縄県",
}


def fetch_json(url: str) -> Dict:
    req = urllib.request.Request(url, headers={"User-Agent": "BusTimetableNavi/1.0"})
    with urllib.request.urlopen(req, timeout=25) as res:
        raw = res.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


def catalog_items(response: object) -> List[Dict]:
    if isinstance(response, dict):
        if str(response.get("code", 200)) != "200":
            raise ValueError("GTFSデータリポジトリがエラーを返しました")
        response = response.get("body")
    if not isinstance(response, list) or not response or not all(isinstance(row, dict) for row in response):
        raise ValueError("GTFSデータリポジトリの一覧が空か、形式が不正です")
    return response


def download_zip(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 500:
        try:
            with zipfile.ZipFile(dest, "r") as zf:
                if zf.namelist():
                    return True
        except Exception:
            dest.unlink(missing_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BusTimetableNavi/1.0"})
        with urllib.request.urlopen(req, timeout=25) as res:
            data = res.read()
        dest.write_bytes(data)
        with zipfile.ZipFile(dest, "r") as zf:
            if not zf.namelist():
                return False
        return True
    except Exception:
        dest.unlink(missing_ok=True)
        return False


def bucket_for_pole(pole_id: str, num_buckets: int) -> str:
    h = int(hashlib.md5(pole_id.encode("utf-8")).hexdigest()[:8], 16)
    return f"b{h % num_buckets:02d}"


# 公開リポジトリ(GitHub Pages)へ再配布可能な標準オープンライセンスのホワイトリスト
ALLOWED_PUBLIC_CATALOG_LICENSES = {
    "CC BY 4.0",
    "CC0 1.0",
    "CC BY 2.1 JP",
}
# 自治体独自URLを伴う CC-BY 表記のうち、二次利用・再配布許諾を一次確認済みのURL
VERIFIED_CC_BY_LICENSE_URLS = {
    "https://www.city.kakegawa.shizuoka.jp/gyosei/docs/383694.html",
}


def is_allowed_public_catalog_license(item: Dict) -> Tuple[bool, str, str]:
    """gtfs-data.jp カタログ項目のライセンスが公開再配布可能かを判定する。"""
    lic = str(item.get("feed_license_id") or item.get("feed_license") or "").strip()
    lic_url = str(item.get("feed_license_url") or "").strip()
    if lic in ALLOWED_PUBLIC_CATALOG_LICENSES:
        return True, lic, lic_url
    if lic == "CC-BY" and lic_url in VERIFIED_CC_BY_LICENSE_URLS:
        return True, "CC-BY (掛川市オープンデータ)", lic_url
    return False, lic, lic_url


def operator_label(org_name: str, feed_name: str) -> str:
    return f"{org_name}（{feed_name}）" if feed_name and feed_name not in org_name else org_name


# GTFSデータリポジトリ掲載フィードのうち、公開GTFS-RT（VehiclePosition / TripUpdate）と突合可能な事業者
PUBLIC_RT_FEED_BY_ORG_FEED = {
    # 群馬県 (pref_10)
    ("nagai-unyu", "Nagaibus"): "nagai_bus",
    # 千葉県 (pref_12)
    ("keisei-transitbus", "keiseitransitbus"): "keisei_chiba_west",
    # 高知県 (pref_39)
    ("tosaden-kotsu", "GTFS-Tosadentraffic_Regularbus"): "tosaden_regular",
    ("tosaden-kotsu", "GTFS-Tosadentraffic_Airportbus"): "tosaden_airport",
    ("kochi-seinan-kotsu", "GTFS-Seinantraffic_Localbus"): "kochi_seinan",
    ("kochi-koryo-kotsu", "GTFS-Koryotraffic_Localbus"): "kochi_koryo",
    ("khkhokubu-kotsu", "GTFS-Hokubutraffic_Localbus"): "kenko_hokubu",
    ("kochi-suzakicity", "GTFS-Susakicity_Bus"): "susaki_city",
    ("tosacity", "GTFS-Tosacity_Bus"): "tosa_dragon",
    # 熊本県 (pref_43)
    ("sankobus", "sankobus"): "kumamoto_sanko",
    ("kumamoto-toshibus", "kumamoto-toshibus"): "kumamoto_toshi",
    ("kumamotodentetsu", "kumamotodentetsu-bus"): "kumamoto_dentetsu",
    ("kuma-bus", "kuma-bus"): "kumamoto_kumabus",
}


def process_feed(task: Tuple[int, int, Dict, Path]) -> Tuple[int, str, List[Dict], Optional[str]]:
    pref, idx, item, raw_dir = task
    org_id = (item.get("organization_id") or "").strip()
    org_name = (item.get("organization_name") or f"org_{idx}").strip()
    feed_name = (item.get("feed_name") or "").strip()
    feed_id = (item.get("feed_id") or f"f{idx}").strip().replace("/", "_")
    file_url = item.get("file_url")
    if not file_url:
        return (pref, org_name, [], "no_url")

    zip_path = raw_dir / f"p{pref:02d}_{idx:03d}_{feed_id}.zip"
    if not download_zip(file_url, zip_path):
        return (pref, org_name, [], "download_failed")

    try:
        op_type, op_short, badge_color = infer_operator_meta(org_name, feed_name)
        rt_feed = PUBLIC_RT_FEED_BY_ORG_FEED.get((org_id, feed_id))
        converted = convert_gtfs(
            gtfs_zip_path=zip_path,
            operator_type=op_type,
            operator_short=op_short,
            supports_realtime=bool(rt_feed),
            default_timetable_url=item.get("organization_web_url") or item.get("feed_page_url") or "",
            badge_color=badge_color,
        )
        prefix = f"nat_p{pref:02d}_{idx:03d}_"
        poles = converted.get("poles", [])
        attach_community_calls(poles, zip_path, prefix)
        op_full = operator_label(org_name, feed_name)
        _, lic_label, lic_url = is_allowed_public_catalog_license(item)
        for p in poles:
            p["pole_id"] = prefix + p["pole_id"]
            p["operator_name"] = op_full
            if lic_label:
                p["feed_license"] = lic_label
            if lic_url:
                p["feed_license_url"] = lic_url
            if rt_feed:
                p["rt_feed"] = rt_feed
            for r in p.get("routes", []):
                r["route_id"] = prefix + r["route_id"]
                if r.get("prev_pole_id"):
                    r["prev_pole_id"] = prefix + r["prev_pole_id"]
                if r.get("next_pole_id"):
                    r["next_pole_id"] = prefix + r["next_pole_id"]
        return (pref, org_name, poles, None)
    except Exception as e:
        return (pref, org_name, [], f"convert_err:{type(e).__name__}:{e}")


def write_pref_shards(pref_code: int, poles: List[Dict], out_data_dir: Path) -> Optional[Dict]:
    if not poles:
        return None
    pref_name = PREF_NAMES.get(pref_code, f"Pref{pref_code:02d}")
    # のりば数に応じてバケット数を自動調整（1ファイル1MB未満・gzip時150KB前後に収める）
    num_buckets = (
        128 if len(poles) >= 10000
        else 64 if len(poles) >= 3500
        else 32 if len(poles) >= 1500
        else 16 if len(poles) >= 400
        else 8 if len(poles) >= 100
        else 4
    )

    index_entries: List[Dict] = []
    buckets: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    min_lat, max_lat = 90.0, -90.0
    min_lng, max_lng = 180.0, -180.0

    for p in poles:
        lat, lng = float(p.get("lat", 0)), float(p.get("lng", 0))
        # 日本国内の妥当な緯度経度範囲のみ採用
        if not (20.0 <= lat <= 46.5 and 122.0 <= lng <= 154.5):
            continue
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lng = min(min_lng, lng)
        max_lng = max(max_lng, lng)

        bname = bucket_for_pole(p["pole_id"], num_buckets)
        routes = p.get("routes") or []
        if len(routes) >= 2:
            sub_label = f"{len(routes)}系統"
        elif len(routes) == 1:
            r0 = routes[0]
            dest = r0.get("destination_stop_name") or ""
            sub_label = f"{r0.get('route_name', '')} {dest}".strip()
        else:
            sub_label = ""

        route_names = sorted({str(r.get("route_name") or "").strip() for r in routes if str(r.get("route_name") or "").strip()})
        dest_names: List[str] = []
        if len(routes) >= 2:
            for r in routes:
                dn = str(r.get("destination_stop_name") or "").strip()
                if dn and dn not in dest_names:
                    dest_names.append(dn)
        index_entry = {
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
        }
        route_ids = sorted({str(r["gtfs_route_id"]) for r in routes if r.get("gtfs_route_id")})
        if route_ids:
            index_entry["rids"] = route_ids
        if len(route_names) >= 2:
            index_entry["rns"] = route_names
        if len(routes) >= 2:
            dests = list(dict.fromkeys(
                str(r.get("destination_stop_name") or "").strip()
                for r in routes
                if str(r.get("destination_stop_name") or "").strip()
            ))
            if dests:
                index_entry["dests"] = dests[:12]
        if dest_names:
            index_entry["dests"] = dest_names[:4]
        if p.get("platform_code"):
            index_entry["pf"] = p["platform_code"]
        if p.get("vehicle_type") in ("ferry", "tram", "subway", "rail"):
            index_entry["vt"] = p["vehicle_type"]
        if p.get("booking_required"):
            btypes = p.get("booking_types") or [2]
            index_entry["bk"] = btypes[0] if len(btypes) == 1 else True
        elif p.get("booking_partial"):
            index_entry["bk"] = "partial"
        index_entries.append(index_entry)
        buckets[bname][p["pole_id"]] = p

    if not index_entries:
        return None

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

    return {
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


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="全国47都道府県シャードJSON一括生成")
    parser.add_argument("--raw-dir", type=Path, default=root / "data-pipeline" / "raw" / "national_gtfs")
    parser.add_argument("--out-site-dir", type=Path, default=root / "national-data-site")
    parser.add_argument("--toei-js", type=Path, default=root / "ios-app" / "BusTimetableNavi" / "Web" / "toei_gtfs_data.js")
    parser.add_argument("--download-toei", action="store_true", help="都営バス公式GTFSを毎回最新ダウンロードしてマージ")
    parser.add_argument("--catalog-file", type=Path, help="事前取得済みのGTFS一覧JSON")
    parser.add_argument("--strict", action="store_true", help="フィード取得・変換の失敗があればシャードを公開しない")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    out_data_dir = args.out_site_dir / "data"
    out_data_dir.mkdir(parents=True, exist_ok=True)

    print("Fetching nationwide feed catalog from gtfs-data.jp...", flush=True)
    resp = json.loads(args.catalog_file.read_text(encoding="utf-8")) if args.catalog_file else fetch_json(API_URL)
    items = catalog_items(resp)
    print(f"Total feeds in catalog: {len(items)}", flush=True)

    tasks = []
    skipped_unverified_license = []
    for idx, it in enumerate(items):
        raw_pref = it.get("feed_pref_id")
        try:
            pref = int(raw_pref)
        except (TypeError, ValueError):
            continue
        if not (1 <= pref <= 47):
            continue
        lic_ok, lic_label, lic_url = is_allowed_public_catalog_license(it)
        if not lic_ok:
            skipped_unverified_license.append(
                f"pref={pref:02d} {it.get('organization_id')}/{it.get('feed_id')} license={lic_label!r} url={lic_url!r}"
            )
            continue
        tasks.append((pref, it.get("_slot", idx), it, args.raw_dir))

    if skipped_unverified_license:
        print(
            f"[LICENSE-SKIP] Excluded {len(skipped_unverified_license)} feeds with unverified/non-public license:",
            flush=True,
        )
        for row in skipped_unverified_license[:20]:
            print(f"  - {row}", flush=True)

    if not tasks:
        raise RuntimeError("国内のGTFSフィードが一覧にありません")

    poles_by_pref: Dict[int, Dict[str, Dict]] = defaultdict(dict)
    ok_feeds = 0
    fail_feeds = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for done_i, (pref, org_name, poles, err) in enumerate(ex.map(process_feed, tasks), 1):
            if err:
                fail_feeds += 1
                print(f"[SKIP] pref={pref:02d} {org_name}: {err}", flush=True)
            else:
                ok_feeds += 1
                for p in poles:
                    poles_by_pref[pref][p["pole_id"]] = p
            if done_i % 50 == 0 or done_i == len(tasks):
                print(f"Progress: {done_i}/{len(tasks)} feeds (ok={ok_feeds}, fail={fail_feeds})", flush=True)

    if args.strict and fail_feeds:
        raise RuntimeError(f"{fail_feeds} 件のGTFSフィードを取得・変換できませんでした")

    # 東京都(13)に都営バスGTFSをマージ（--download-toei時は最新公式ZIPを取得）
    if args.download_toei:
        toei_zip = args.raw_dir / "ToeiBus-GTFS.zip"
        if download_zip(TOEI_URL, toei_zip):
            toei_conv = convert_gtfs(
                gtfs_zip_path=toei_zip,
                operator_type="public",
                operator_short="都営",
                supports_realtime=True,
                default_timetable_url="https://tobus.jp/",
                badge_color="#15803d",
            )
            toei_poles = toei_conv.get("poles", [])
            if args.strict and not toei_poles:
                raise RuntimeError("ODPTの都営バスGTFSから停留所を生成できませんでした")
            from toei_trip_calls import build_trip_calls
            onward = build_trip_calls(toei_zip)
            for p in toei_poles:
                if p["pole_id"] in onward:
                    p["onward"] = onward[p["pole_id"]]
                p["operator_name"] = "東京都交通局（都営バス）"
                p["feed_license"] = "CC BY 4.0"
                poles_by_pref[13][p["pole_id"]] = p
            print(f"Downloaded & merged live Toei Bus poles into pref 13: {len(toei_poles)}", flush=True)
        elif args.strict:
            raise RuntimeError("ODPTの都営バスGTFSを取得できませんでした")
    elif args.toei_js.exists():
        toei_poles = load_poles_from_js_or_json(args.toei_js)
        for p in toei_poles:
            poles_by_pref[13][p["pole_id"]] = p
        print(f"Merged Toei Bus poles into pref 13: {len(toei_poles)}", flush=True)

    manifest_prefs: List[Dict] = []
    total_poles = 0
    for pref_code in sorted(poles_by_pref.keys()):
        entry = write_pref_shards(pref_code, list(poles_by_pref[pref_code].values()), out_data_dir)
        if entry:
            manifest_prefs.append(entry)
            total_poles += entry["pole_count"]

    manifest_path = out_data_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": "2.0", "prefs": manifest_prefs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # GitHub Pages 高速配信用 .nojekyll & 簡易トップ
    (args.out_site_dir / ".nojekyll").write_text("", encoding="utf-8")
    (args.out_site_dir / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>BusTimetableNavi Data CDN</title><p>BusTimetableNavi sharded open transit data.</p>",
        encoding="utf-8",
    )

    total_bytes = 0
    max_file_bytes = 0
    max_file_name = ""
    file_count = 0
    for f in out_data_dir.rglob("*.json"):
        sz = f.stat().st_size
        total_bytes += sz
        file_count += 1
        if sz > max_file_bytes:
            max_file_bytes = sz
            max_file_name = str(f.relative_to(out_data_dir))

    summary = {
        "prefectures": len(manifest_prefs),
        "ok_feeds": ok_feeds,
        "fail_feeds": fail_feeds,
        "total_poles": total_poles,
        "json_files": file_count,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "max_file_mb": round(max_file_bytes / (1024 * 1024), 2),
        "max_file": max_file_name,
    }
    print("NATIONAL_BUILD_DONE", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
