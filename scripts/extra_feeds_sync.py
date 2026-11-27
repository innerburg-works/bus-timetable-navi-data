#!/usr/bin/env python3
"""
全国時刻表の追加公式フィード同期・期限保護・ライセンス分離パイプライン。

主な機能:
  1. --mode public:
     認証不要・公開再配布可能 (CC BY 4.0 / CC0 / CC BY 2.1 JP) な追加フィード
     (ODPT公開26件 + 鳥取2社 + 広島電鉄 + 宮崎日向市) を一時ディレクトリへ安全マージし、
     路線図・多言語辞書・manifest事業者メタデータを生成する。
     取得失敗・破損ZIP時は前回公開版の有効期間内データのみ維持し、期限切れは誤案内しない。
  2. --mode bundled-odpt:
     公共交通オープンデータ基本ライセンス全社 (京都・山口・愛媛・首都圏私鉄/市営等) を
     アプリ同梱用 raw-deflate (.json.z) 形式で Web/data/bundled_odpt/ へ生成する。
  3. --mode verify-preservation:
     公開版と新規生成版を事業者・フィード単位で比較し、想定外の消失を検出する。
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from build_national_i18n import extract_i18n_from_zip
from build_national_shards import PREF_NAMES, bucket_for_pole, write_pref_shards
from build_tokyo_routes import extract_routes_from_gtfs
from extra_feeds_registry import (
    EXTRA_PUBLIC_FEEDS,
    NON_PUBLIC_POLE_PREFIXES,
    ODPT_BASIC_LICENSE_FEEDS,
)
from gtfs_to_app_data import convert_gtfs

JST = timezone(timedelta(hours=9))


def today_jst_ymd() -> str:
    return datetime.now(JST).strftime("%Y%m%d")


def raw_deflate_bytes(data: bytes) -> bytes:
    comp = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return comp.compress(data) + comp.flush()


def raw_inflate_bytes(data: bytes) -> bytes:
    return zlib.decompress(data, -zlib.MAX_WBITS)


def write_json_z(path: Path, obj: object) -> Tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    comp = raw_deflate_bytes(raw)
    path.write_bytes(comp)
    return len(raw), len(comp)


def inspect_gtfs_zip_validity(zip_path: Path, ref_ymd: Optional[str] = None) -> Dict:
    """ZIP内の feed_info.txt / calendar.txt / calendar_dates.txt から有効期間と運行日数を直接読む。"""
    ref = ref_ymd or today_jst_ymd()
    info = {
        "valid_from": "",
        "valid_to": "",
        "feed_version": "",
        "publisher": "",
        "active_on_ref": False,
        "expired_on_ref": False,
        "not_yet_started": False,
        "trip_count_on_ref": 0,
        "has_shapes": False,
        "has_translations": False,
    }
    if not zip_path.exists():
        return info

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        info["has_shapes"] = "shapes.txt" in names
        info["has_translations"] = "translations.txt" in names

        def read_csv(name: str) -> List[Dict[str, str]]:
            if name not in names:
                return []
            return list(csv.DictReader(io.StringIO(zf.read(name).decode("utf-8-sig", errors="replace"))))

        fi_rows = read_csv("feed_info.txt")
        if fi_rows:
            fi = fi_rows[0]
            info["feed_version"] = (fi.get("feed_version") or "").strip()
            info["publisher"] = (fi.get("feed_publisher_name") or "").strip()

        cal_rows = read_csv("calendar.txt")
        cal_date_rows = read_csv("calendar_dates.txt")
        trips_rows = read_csv("trips.txt")

        starts: List[str] = []
        ends: List[str] = []
        cals: Dict[str, Dict] = {}
        day_keys = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        for r in cal_rows:
            sid = (r.get("service_id") or "").strip()
            if not sid:
                continue
            s = (r.get("start_date") or "").strip()
            e = (r.get("end_date") or "").strip()
            if len(s) == 8 and s.isdigit():
                starts.append(s)
            if len(e) == 8 and e.isdigit():
                ends.append(e)
            bits = "".join("1" if r.get(k) == "1" else "0" for k in day_keys)
            cals[sid] = {"d": bits, "s": s, "e": e, "a": set(), "r": set()}

        for r in cal_date_rows:
            sid = (r.get("service_id") or "").strip()
            d = (r.get("date") or "").strip()
            ex = (r.get("exception_type") or "").strip()
            if not sid or len(d) != 8 or not d.isdigit() or ex not in ("1", "2"):
                continue
            if sid not in cals:
                cals[sid] = {"d": "0000000", "s": "", "e": "", "a": set(), "r": set()}
            if ex == "1":
                cals[sid]["a"].add(d)
                starts.append(d)
                ends.append(d)
            else:
                cals[sid]["r"].add(d)

        if fi_rows:
            fs = (fi_rows[0].get("feed_start_date") or "").strip()
            fe = (fi_rows[0].get("feed_end_date") or "").strip()
            if not starts and len(fs) == 8 and fs.isdigit():
                starts.append(fs)
            if not ends and len(fe) == 8 and fe.isdigit():
                ends.append(fe)

        info["valid_from"] = min(starts) if starts else ""
        info["valid_to"] = max(ends) if ends else ""

        # 指定日 ref の有効 service_id と便数を計算
        try:
            ref_dow = datetime.strptime(ref, "%Y%m%d").weekday()  # 0=Mon..6=Sun
        except ValueError:
            ref_dow = 0
        active_sids: Set[str] = set()
        for sid, c in cals.items():
            if ref in c["a"]:
                active_sids.add(sid)
                continue
            if ref in c["r"]:
                continue
            if c["s"] and ref < c["s"]:
                continue
            if c["e"] and ref > c["e"]:
                continue
            if len(c["d"]) == 7 and c["d"][ref_dow] == "1":
                active_sids.add(sid)

        if trips_rows and active_sids:
            info["trip_count_on_ref"] = sum(
                1 for t in trips_rows if (t.get("service_id") or "").strip() in active_sids
            )
        info["active_on_ref"] = bool(active_sids)
        if info["valid_to"] and ref > info["valid_to"]:
            info["expired_on_ref"] = True
        if info["valid_from"] and ref < info["valid_from"]:
            info["not_yet_started"] = True
    return info


def resolve_bodik_zip_url(package_id: str) -> Optional[str]:
    api_url = f"https://data.bodik.jp/api/3/action/package_show?id={urllib.parse.quote(package_id)}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "BusTimetableNavi/1.0"})
        with urllib.request.urlopen(req, timeout=15) as res:
            doc = json.loads(res.read().decode("utf-8"))
        for r in doc.get("result", {}).get("resources", []):
            fmt = str(r.get("format") or "").upper()
            url = str(r.get("url") or "").strip()
            if url and (fmt == "ZIP" or url.lower().endswith(".zip")):
                return url
    except Exception:
        return None
    return None


def download_zip_with_auth(urls: List[str], dest: Path, api_key: str = "", timeout: int = 35) -> Tuple[Optional[str], str]:
    last_err = ""
    for base_url in urls:
        full_url = base_url
        if "api.odpt.org/" in base_url:
            if not api_key:
                last_err = f"ODPT_API_KEY未設定 ({base_url})"
                continue
            sep = "&" if "?" in base_url else "?"
            full_url = f"{base_url}{sep}{urllib.parse.urlencode({'acl:consumerKey': api_key})}"
        tmp = dest.with_suffix(".zip.tmp")
        try:
            req = urllib.request.Request(full_url, headers={"User-Agent": "BusTimetableNavi/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                data = res.read()
            tmp.write_bytes(data)
            with zipfile.ZipFile(tmp, "r") as zf:
                names = set(zf.namelist())
                if not names or "stops.txt" not in names or "stop_times.txt" not in names:
                    tmp.unlink(missing_ok=True)
                    last_err = f"invalid_gtfs_zip ({base_url})"
                    continue
            tmp.replace(dest)
            return base_url, ""
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            last_err = f"HTTP {e.code} ({base_url})"
        except Exception as e:
            tmp.unlink(missing_ok=True)
            last_err = f"{type(e).__name__}: {e} ({base_url})"
    return None, last_err


def acquire_feed_zip(
    feed: Dict,
    raw_dir: Path,
    api_key: str = "",
    ref_ymd: Optional[str] = None,
    use_cached_raw: bool = False,
    cached_raw_dir: Optional[Path] = None,
    simulate_fail_ids: Optional[Set[str]] = None,
) -> Tuple[Optional[Path], str, Dict, str]:
    """フィードZIPを取得（必要ならBODIK動的解決・未来データ切替・キャッシュ再利用）して検証情報を返す。"""
    fid = feed["id"]
    ref = ref_ymd or today_jst_ymd()
    if simulate_fail_ids and fid in simulate_fail_ids:
        return None, "", {}, "simulated_download_failure"

    dest = raw_dir / f"extra_{fid}.zip"

    # キャッシュ再利用モード（ローカル試験・オフライン構築用）
    if use_cached_raw and cached_raw_dir:
        candidates = [
            cached_raw_dir / f"extra_{fid}.zip",
            cached_raw_dir / f"odpt_{fid}.zip",
            cached_raw_dir / f"odpt_pub_{fid}.zip",
        ]
        for cand in candidates:
            if cand.exists() and cand.stat().st_size > 500:
                try:
                    val = inspect_gtfs_zip_validity(cand, ref_ymd=ref)
                    if cand != dest:
                        shutil.copy2(cand, dest)
                    return dest, f"cache:{cand.name}", val, ""
                except Exception:
                    pass

    urls = list(feed.get("urls", []))
    if feed.get("bodik_package_id"):
        dynamic_url = resolve_bodik_zip_url(feed["bodik_package_id"])
        if dynamic_url and dynamic_url not in urls:
            urls.insert(0, dynamic_url)

    matched_url, err = download_zip_with_auth(urls, dest, api_key=api_key)
    cur_val = inspect_gtfs_zip_validity(dest, ref_ymd=ref) if matched_url else {}

    # 広島電鉄など「現在データ」と「未来データ」が分離されているフィードの改正日自動切替
    future_urls = feed.get("future_urls") or []
    if future_urls:
        fut_dest = raw_dir / f"extra_{fid}_future.zip"
        fut_url, _ = download_zip_with_auth(future_urls, fut_dest, api_key=api_key)
        if fut_url:
            fut_val = inspect_gtfs_zip_validity(fut_dest, ref_ymd=ref)
            use_future = False
            if not matched_url:
                use_future = True
            elif fut_val.get("valid_from") and fut_val["valid_from"] <= ref and not fut_val.get("expired_on_ref"):
                if cur_val.get("expired_on_ref") or (
                    cur_val.get("valid_from") and fut_val["valid_from"] > cur_val["valid_from"]
                ):
                    use_future = True
            if use_future:
                shutil.copy2(fut_dest, dest)
                return dest, fut_url, fut_val, ""

    if not matched_url:
        return None, "", {}, err
    return dest, matched_url, cur_val, ""


def is_duplicate_in_catalog(feed: Dict, catalog_items_list: List[Dict]) -> Optional[str]:
    """将来、同じ事業者が gtfs-data.jp 全国カタログに登録された際の二重掲載を防止する。"""
    keywords = [k.strip() for k in (feed.get("dedup_org_keywords") or []) if k.strip()]
    if not keywords or not catalog_items_list:
        return None
    pref = int(feed["pref"])
    for item in catalog_items_list:
        try:
            ipref = int(item.get("feed_pref_id"))
        except (TypeError, ValueError):
            continue
        if ipref != pref:
            continue
        hay = f"{item.get('organization_name', '')} {item.get('feed_name', '')}"
        for kw in keywords:
            if kw in hay:
                return f"{item.get('organization_id')}/{item.get('feed_id')} ({hay.strip()})"
    return None


def load_pref_poles_plain(data_dir: Path, pref_code: int) -> Dict[str, Dict]:
    tt_dir = data_dir / "timetables" / f"pref_{pref_code:02d}"
    poles: Dict[str, Dict] = {}
    if not tt_dir.exists():
        return poles
    for f in sorted(tt_dir.glob("b*.json")):
        try:
            poles.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return poles


def load_pref_poles_z(data_dir: Path, pref_code: int) -> Dict[str, Dict]:
    tt_dir = data_dir / "timetables" / f"pref_{pref_code:02d}"
    poles: Dict[str, Dict] = {}
    if not tt_dir.exists():
        return poles
    for f in sorted(tt_dir.glob("b*.json.z")):
        try:
            poles.update(json.loads(raw_inflate_bytes(f.read_bytes()).decode("utf-8")))
        except Exception:
            pass
    return poles


def poles_calendar_range(poles: List[Dict]) -> Tuple[str, str]:
    starts: List[str] = []
    ends: List[str] = []
    for p in poles:
        for cal in (p.get("svc_cal") or {}).values():
            s = str(cal.get("s") or "")
            e = str(cal.get("e") or "")
            if len(s) == 8 and s.isdigit():
                starts.append(s)
            if len(e) == 8 and e.isdigit():
                ends.append(e)
            for d in cal.get("a") or []:
                if len(d) == 8 and d.isdigit():
                    starts.append(d)
                    ends.append(d)
    return (min(starts) if starts else "", max(ends) if ends else "")


def load_pref_routes(data_dir: Path, pref_code: int, compressed: bool = False) -> List[Dict]:
    p = data_dir / "routes" / (f"pref_{pref_code:02d}.json.z" if compressed else f"pref_{pref_code:02d}.json")
    if not p.exists():
        return []
    try:
        raw = raw_inflate_bytes(p.read_bytes()).decode("utf-8") if compressed else p.read_text(encoding="utf-8")
        doc = json.loads(raw)
        return doc.get("routes", []) if isinstance(doc, dict) else []
    except Exception:
        return []


def summarize_pref_operators(poles: List[Dict], feed_meta_by_prefix: Dict[str, Dict]) -> List[Dict]:
    """都道府県内の事業者別サマリ（件数・有効期間・ライセンス・出典・エリア）を生成する。"""
    by_op: Dict[str, Dict] = {}
    for p in poles:
        pid = p.get("pole_id", "")
        matched_feed = None
        for pref_key, fmeta in feed_meta_by_prefix.items():
            if pid.startswith(pref_key):
                matched_feed = fmeta
                break
        op_name = p.get("operator_name") or (matched_feed["label"] if matched_feed else "不明")
        key = matched_feed["id"] if matched_feed else op_name
        rec = by_op.get(key)
        if not rec:
            rec = {
                "id": key,
                "name": op_name,
                "pole_count": 0,
                "rt": False,
                "starts": [],
                "ends": [],
                "area": matched_feed.get("area", "") if matched_feed else "",
                "area_en": matched_feed.get("area_en", "") if matched_feed else "",
                "license": p.get("feed_license") or (matched_feed.get("license", "") if matched_feed else ""),
                "attribution": p.get("feed_attribution") or (matched_feed.get("attribution", "") if matched_feed else ""),
                "official_url": p.get("feed_official_url") or (matched_feed.get("official_url", "") if matched_feed else ""),
            }
            by_op[key] = rec
        rec["pole_count"] += 1
        if p.get("supports_realtime"):
            rec["rt"] = True
        for cal in (p.get("svc_cal") or {}).values():
            s = str(cal.get("s") or "")
            e = str(cal.get("e") or "")
            if len(s) == 8 and s.isdigit():
                rec["starts"].append(s)
            if len(e) == 8 and e.isdigit():
                rec["ends"].append(e)

    out: List[Dict] = []
    for rec in sorted(by_op.values(), key=lambda x: (-x["pole_count"], x["name"])):
        item = {
            "id": rec["id"],
            "name": rec["name"],
            "pole_count": rec["pole_count"],
            "rt": rec["rt"],
        }
        if rec["starts"]:
            item["valid_from"] = min(rec["starts"])
        if rec["ends"]:
            item["valid_to"] = max(rec["ends"])
        if rec["area"]:
            item["area"] = rec["area"]
        if rec["area_en"]:
            item["area_en"] = rec["area_en"]
        if rec["license"]:
            item["license"] = rec["license"]
        if rec["attribution"]:
            item["attribution"] = rec["attribution"]
        if rec["official_url"]:
            item["official_url"] = rec["official_url"]
        out.append(item)
    return out


def write_pref_shards_z(pref_code: int, poles: List[Dict], out_dir: Path) -> Optional[Dict]:
    """アプリ内同梱用の raw-deflate (.json.z) 2層シャードを書き出す。"""
    if not poles:
        return None
    pref_name = PREF_NAMES.get(pref_code, f"Pref{pref_code:02d}")
    num_buckets = (
        64 if len(poles) >= 3500
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
        if not (20.0 <= lat <= 46.5 and 122.0 <= lng <= 154.5):
            continue
        min_lat, max_lat = min(min_lat, lat), max(max_lat, lat)
        min_lng, max_lng = min(min_lng, lng), max(max_lng, lng)

        bname = bucket_for_pole(p["pole_id"], num_buckets)
        routes = p.get("routes") or []
        if len(routes) >= 2:
            sub_label = f"{len(routes)}系統"
        elif len(routes) == 1:
            r0 = routes[0]
            sub_label = f"{r0.get('route_name', '')} {r0.get('destination_stop_name') or ''}".strip()
        else:
            sub_label = ""

        route_names = sorted({str(r.get("route_name") or "").strip() for r in routes if str(r.get("route_name") or "").strip()})
        entry = {
            "pole_id": p["pole_id"],
            "pref": pref_code,
            "b": bname,
            "bo": 1,
            "stop_name": p["stop_name"],
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "arrow": p.get("direction_arrow", "↗"),
            "op_type": p.get("operator_type", "private"),
            "op_short": p.get("operator_short", "私営"),
            "rt": bool(p.get("supports_realtime")),
            "rc": len(routes),
            "sub": sub_label,
        }
        if len(route_names) >= 2:
            entry["rns"] = route_names
        if len(routes) >= 2:
            dests = list(dict.fromkeys(
                str(r.get("destination_stop_name") or "").strip()
                for r in routes
                if str(r.get("destination_stop_name") or "").strip()
            ))
            if dests:
                entry["dests"] = dests[:12]
        if p.get("platform_code"):
            entry["pf"] = p["platform_code"]
        if p.get("vehicle_type") in ("ferry", "tram", "subway", "rail"):
            entry["vt"] = p["vehicle_type"]
        if p.get("booking_required"):
            btypes = p.get("booking_types") or [2]
            entry["bk"] = btypes[0] if len(btypes) == 1 else True
        elif p.get("booking_partial"):
            entry["bk"] = "partial"
        index_entries.append(entry)
        buckets[bname][p["pole_id"]] = p

    if not index_entries:
        return None

    tt_dir = out_dir / "timetables" / f"pref_{pref_code:02d}"
    if tt_dir.exists():
        for old in tt_dir.glob("b*.json*"):
            old.unlink()
    write_json_z(
        out_dir / "index" / f"pref_{pref_code:02d}.json.z",
        {"pref": pref_code, "name": pref_name, "poles": index_entries},
    )
    for bname, bpoles in buckets.items():
        write_json_z(tt_dir / f"{bname}.json.z", bpoles)

    return {
        "pref": pref_code,
        "name": pref_name,
        "pole_count": len(index_entries),
        "index_url": f"data/bundled_odpt/index/pref_{pref_code:02d}.json",
        "timetable_base": f"data/bundled_odpt/timetables/pref_{pref_code:02d}",
        "bounds": {
            "min_lat": round(min_lat, 5),
            "max_lat": round(max_lat, 5),
            "min_lng": round(min_lng, 5),
            "max_lng": round(max_lng, 5),
        },
    }


def run_public_sync(
    staging_data_dir: Path,
    published_data_dir: Optional[Path],
    raw_dir: Path,
    catalog_file: Optional[Path] = None,
    ref_ymd: Optional[str] = None,
    use_cached_raw: bool = False,
    cached_raw_dir: Optional[Path] = None,
    simulate_fail_ids: Optional[Set[str]] = None,
    strip_non_public: bool = True,
) -> Dict:
    """公開再配布可能 (CC BY / CC0) な追加フィードを staging_data_dir へ安全マージする。"""
    ref = ref_ymd or today_jst_ymd()
    raw_dir.mkdir(parents=True, exist_ok=True)
    catalog_items_list: List[Dict] = []
    if catalog_file and catalog_file.exists():
        raw_cat = json.loads(catalog_file.read_text(encoding="utf-8"))
        catalog_items_list = raw_cat.get("body", []) if isinstance(raw_cat, dict) else raw_cat

    feed_meta_by_prefix = {f["pole_prefix"]: f for f in EXTRA_PUBLIC_FEEDS}
    report: Dict = {"mode": "public", "ref_ymd": ref, "feeds": [], "stripped_non_public": {}}

    # 1. まず公開ディレクトリから非公開（基本ライセンス・汚染JSON-LD）プレフィックスを除外
    touched_prefs: Set[int] = set()
    poles_by_pref: Dict[int, Dict[str, Dict]] = {}
    routes_by_pref: Dict[int, List[Dict]] = {}
    i18n_by_pref: Dict[int, Dict[str, str]] = {}

    def ensure_pref_loaded(pref_code: int) -> None:
        if pref_code not in poles_by_pref:
            poles_by_pref[pref_code] = load_pref_poles_plain(staging_data_dir, pref_code)
            routes_by_pref[pref_code] = load_pref_routes(staging_data_dir, pref_code, compressed=False)
            i18n_p = staging_data_dir / "i18n" / f"pref_{pref_code:02d}.json"
            i18n_by_pref[pref_code] = json.loads(i18n_p.read_text(encoding="utf-8")) if i18n_p.exists() else {}

    if strip_non_public:
        for idx_p in sorted((staging_data_dir / "index").glob("pref_*.json")):
            m = re.match(r"pref_(\d{2})\.json$", idx_p.name)
            if not m:
                continue
            pcode = int(m.group(1))
            idx_doc = json.loads(idx_p.read_text(encoding="utf-8"))
            bad_ids = [
                e["pole_id"] for e in idx_doc.get("poles", [])
                if e.get("pole_id", "").startswith(NON_PUBLIC_POLE_PREFIXES)
            ]
            if bad_ids:
                ensure_pref_loaded(pcode)
                for pid in bad_ids:
                    poles_by_pref[pcode].pop(pid, None)
                routes_by_pref[pcode] = [
                    r for r in routes_by_pref[pcode]
                    if not str(r.get("id", "")).startswith(NON_PUBLIC_POLE_PREFIXES)
                ]
                touched_prefs.add(pcode)
                report["stripped_non_public"][f"pref_{pcode:02d}"] = len(bad_ids)

    # 2. 各公開追加フィードを取得・変換・安全マージ
    for feed in EXTRA_PUBLIC_FEEDS:
        fid = feed["id"]
        pref = int(feed["pref"])
        prefix = feed["pole_prefix"]

        dup = is_duplicate_in_catalog(feed, catalog_items_list)
        if dup:
            report["feeds"].append({
                "id": fid, "pref": pref, "label": feed["label"],
                "status": "skipped_duplicate_in_catalog", "detail": dup, "poles": 0,
            })
            print(f"[DEDUP] pref_{pref:02d} {feed['label']}: catalog重複のためスキップ ({dup})")
            continue

        ensure_pref_loaded(pref)
        # 既に別プレフィックス (例: 並行作業で入った nat_p31_001_ / nat_p34_001_) で同一事業者が収録済みなら
        # 二重掲載を防ぎつつ既存ID互換性を保つため、そのプレフィックスを再利用して更新する
        effective_prefix = prefix
        replaces_extra_prefixes: Set[str] = set()
        dedup_kws = [k.strip() for k in (feed.get("dedup_org_keywords") or []) if k.strip()]
        if dedup_kws:
            for existing_pid, existing_pole in list(poles_by_pref[pref].items()):
                if existing_pid.startswith(prefix):
                    continue
                m_nat = re.match(rf"^(nat_p{pref:02d}_\d{{3}}_)", existing_pid)
                if not m_nat:
                    continue
                op_nm = str(existing_pole.get("operator_name") or "")
                if any(kw in op_nm for kw in dedup_kws):
                    effective_prefix = m_nat.group(1)
                    replaces_extra_prefixes.add(prefix)
                    break

        zip_path, matched_url, validity, err = acquire_feed_zip(
            feed,
            raw_dir=raw_dir,
            api_key="",
            ref_ymd=ref,
            use_cached_raw=use_cached_raw,
            cached_raw_dir=cached_raw_dir,
            simulate_fail_ids=simulate_fail_ids,
        )

        new_poles: List[Dict] = []
        route_lines: List[Dict] = []
        i18n_map: Dict[str, str] = {}
        conv_err = ""
        if zip_path:
            try:
                conv = convert_gtfs(
                    gtfs_zip_path=zip_path,
                    operator_type=feed["operator_type"],
                    operator_short=feed["operator_short"],
                    supports_realtime=bool(feed.get("rt_feed")),
                    default_timetable_url=feed["default_url"],
                    badge_color=feed["badge_color"],
                    allowed_route_types=feed.get("allowed_route_types"),
                )
                new_poles = conv.get("poles", [])
                if new_poles:
                    route_lines = extract_routes_from_gtfs(
                        zip_path=zip_path,
                        op_type=feed["operator_type"],
                        op_short=feed["operator_short"],
                        op_name_default=feed["label"],
                        id_prefix=effective_prefix,
                        eps=0.00010,
                        allowed_route_types=feed.get("allowed_route_types"),
                    )
                    i18n_map = extract_i18n_from_zip(zip_path)
            except Exception as exc:
                conv_err = f"{type(exc).__name__}: {exc}"
                new_poles = []

        if new_poles:
            touched_prefs.add(pref)
            # 同一プレフィックスおよび重複旧プレフィックスを置換（再実行でも重複させない）
            clear_prefixes = {effective_prefix, prefix} | replaces_extra_prefixes
            for old_pid in [k for k in poles_by_pref[pref].keys() if any(k.startswith(cp) for cp in clear_prefixes)]:
                del poles_by_pref[pref][old_pid]
            for p in new_poles:
                p["pole_id"] = effective_prefix + p["pole_id"]
                p["operator_name"] = feed["label"]
                p["feed_source_id"] = fid
                p["feed_license"] = feed["license"]
                p["feed_attribution"] = feed["attribution"]
                p["feed_official_url"] = feed["official_url"]
                if validity.get("valid_from"):
                    p["feed_valid_from"] = validity["valid_from"]
                if validity.get("valid_to"):
                    p["feed_valid_to"] = validity["valid_to"]
                if validity.get("feed_version"):
                    p["feed_version"] = validity["feed_version"]
                if feed.get("rt_feed"):
                    p["rt_feed"] = feed["rt_feed"]
                for r in p.get("routes", []):
                    r["route_id"] = effective_prefix + r["route_id"]
                    if r.get("prev_pole_id"):
                        r["prev_pole_id"] = effective_prefix + r["prev_pole_id"]
                    if r.get("next_pole_id"):
                        r["next_pole_id"] = effective_prefix + r["next_pole_id"]
                poles_by_pref[pref][p["pole_id"]] = p

            rt_clear_prefixes = set(clear_prefixes)
            if effective_prefix.startswith("nat_"):
                rt_clear_prefixes.add("r_" + effective_prefix[4:])
            routes_by_pref[pref] = [
                r for r in routes_by_pref[pref]
                if not any(str(r.get("id", "")).startswith(cp) for cp in rt_clear_prefixes)
            ] + route_lines
            for k, v in i18n_map.items():
                i18n_by_pref[pref].setdefault(k, v)

            rec = {
                "id": fid, "pref": pref, "label": feed["label"], "status": "ok",
                "prefix": effective_prefix,
                "poles": len(new_poles), "routes": len(route_lines), "i18n": len(i18n_map),
                "valid_from": validity.get("valid_from", ""), "valid_to": validity.get("valid_to", ""),
                "feed_version": validity.get("feed_version", ""), "source_url": matched_url,
                "license": feed["license"],
            }
            report["feeds"].append(rec)
            print(
                f"[OK] pref_{pref:02d} {feed['label']} ({effective_prefix}): poles={len(new_poles)}, "
                f"routes={len(route_lines)}, i18n={len(i18n_map)}, valid={rec['valid_from']}..{rec['valid_to']}"
            )
            continue

        # 取得失敗または0件時: 前回公開版から有効期間内のみ安全フォールバック
        fallback_poles: Dict[str, Dict] = {}
        fallback_routes: List[Dict] = []
        if published_data_dir and published_data_dir.exists():
            prev_all = load_pref_poles_plain(published_data_dir, pref)
            fallback_poles = {
                k: v for k, v in prev_all.items()
                if k.startswith(effective_prefix) or k.startswith(prefix)
            }
            prev_routes = load_pref_routes(published_data_dir, pref, compressed=False)
            fallback_routes = [
                r for r in prev_routes
                if str(r.get("id", "")).startswith(effective_prefix) or str(r.get("id", "")).startswith(prefix)
            ]

        if fallback_poles:
            vf, vt = poles_calendar_range(list(fallback_poles.values()))
            is_expired = bool(vt and ref > vt)
            touched_prefs.add(pref)
            for pid, p in fallback_poles.items():
                if is_expired:
                    p["feed_expired"] = True
                poles_by_pref[pref][pid] = p
            routes_by_pref[pref] = [
                r for r in routes_by_pref[pref] if not str(r.get("id", "")).startswith(prefix)
            ] + fallback_routes
            status = "fallback_expired" if is_expired else "fallback_valid"
            report["feeds"].append({
                "id": fid, "pref": pref, "label": feed["label"], "status": status,
                "poles": len(fallback_poles), "routes": len(fallback_routes),
                "valid_from": vf, "valid_to": vt, "error": conv_err or err,
            })
            print(
                f"[{status.upper()}] pref_{pref:02d} {feed['label']}: "
                f"前回版 {len(fallback_poles)} 標柱を保持 (valid={vf}..{vt}, reason={conv_err or err})"
            )
        else:
            report["feeds"].append({
                "id": fid, "pref": pref, "label": feed["label"], "status": "failed_no_fallback",
                "poles": 0, "error": conv_err or err,
            })
            print(f"[FAIL] pref_{pref:02d} {feed['label']}: 取得失敗・前回版なし ({conv_err or err})")

    # 3. 変更のあった都道府県シャードを書き出し（0件になった県は公開インデックスから除外）
    manifest_path = staging_data_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else {"schema_version": "2.0", "prefs": []}
    )
    pref_entries_by_code = {int(p["pref"]): p for p in manifest.get("prefs", [])}

    for pref in sorted(touched_prefs):
        tt_dir = staging_data_dir / "timetables" / f"pref_{pref:02d}"
        idx_file = staging_data_dir / "index" / f"pref_{pref:02d}.json"
        rt_file = staging_data_dir / "routes" / f"pref_{pref:02d}.json"
        i18n_file = staging_data_dir / "i18n" / f"pref_{pref:02d}.json"
        if tt_dir.exists():
            for old in tt_dir.glob("b*.json"):
                old.unlink()
        p_list = list(poles_by_pref[pref].values())
        if not p_list:
            idx_file.unlink(missing_ok=True)
            rt_file.unlink(missing_ok=True)
            if tt_dir.exists():
                shutil.rmtree(tt_dir, ignore_errors=True)
            pref_entries_by_code.pop(pref, None)
            print(f"[REMOVED-EMPTY] pref_{pref:02d}: 公開可能データ0件のため公開対象から除外（基本ライセンスはbundled_odptへ移動）")
            continue

        entry = write_pref_shards(pref, p_list, staging_data_dir)
        if entry:
            entry["operators"] = summarize_pref_operators(p_list, feed_meta_by_prefix)
            pref_entries_by_code[pref] = entry
        if routes_by_pref[pref]:
            rt_file.parent.mkdir(parents=True, exist_ok=True)
            rt_file.write_text(
                json.dumps({"pref": pref, "routes": routes_by_pref[pref]}, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        if i18n_by_pref[pref]:
            i18n_file.parent.mkdir(parents=True, exist_ok=True)
            i18n_file.write_text(
                json.dumps(i18n_by_pref[pref], ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )

    # 4. 全県の manifest.json に operators サマリが無い県も補完し、更新日時を付与
    for pref_code, entry in sorted(pref_entries_by_code.items()):
        if "operators" not in entry:
            p_list = list(load_pref_poles_plain(staging_data_dir, pref_code).values())
            entry["operators"] = summarize_pref_operators(p_list, feed_meta_by_prefix)

    manifest["schema_version"] = "2.1"
    manifest["generated_at"] = datetime.now(JST).isoformat(timespec="seconds")
    manifest["gtfs_sync"] = True
    manifest["public_license_only"] = True
    manifest["prefs"] = [pref_entries_by_code[k] for k in sorted(pref_entries_by_code.keys())]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    report["total_public_prefs"] = len(manifest["prefs"])
    report["total_public_poles"] = sum(p["pole_count"] for p in manifest["prefs"])
    return report


def run_bundled_odpt_build(
    out_bundled_dir: Path,
    raw_dir: Path,
    api_key: str = "",
    ref_ymd: Optional[str] = None,
    use_cached_raw: bool = True,
    cached_raw_dir: Optional[Path] = None,
    simulate_fail_ids: Optional[Set[str]] = None,
) -> Dict:
    """ODPT 基本ライセンス全社をアプリ内同梱用 .json.z (Web/data/bundled_odpt/) へ生成する。"""
    ref = ref_ymd or today_jst_ymd()
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_bundled_dir.mkdir(parents=True, exist_ok=True)

    feed_meta_by_prefix = {f["pole_prefix"]: f for f in ODPT_BASIC_LICENSE_FEEDS}
    poles_by_pref: Dict[int, Dict[str, Dict]] = defaultdict(dict)
    routes_by_pref: Dict[int, List[Dict]] = defaultdict(list)
    i18n_by_pref: Dict[int, Dict[str, str]] = defaultdict(dict)
    report: Dict = {"mode": "bundled-odpt", "ref_ymd": ref, "feeds": []}

    for feed in ODPT_BASIC_LICENSE_FEEDS:
        fid = feed["id"]
        pref = int(feed["pref"])
        prefix = feed["pole_prefix"]

        zip_path, matched_url, validity, err = acquire_feed_zip(
            feed,
            raw_dir=raw_dir,
            api_key=api_key,
            ref_ymd=ref,
            use_cached_raw=use_cached_raw,
            cached_raw_dir=cached_raw_dir,
            simulate_fail_ids=simulate_fail_ids,
        )

        new_poles: List[Dict] = []
        route_lines: List[Dict] = []
        i18n_map: Dict[str, str] = {}
        conv_err = ""
        if zip_path:
            try:
                conv = convert_gtfs(
                    gtfs_zip_path=zip_path,
                    operator_type=feed["operator_type"],
                    operator_short=feed["operator_short"],
                    supports_realtime=bool(feed.get("rt_feed")),
                    default_timetable_url=feed["default_url"],
                    badge_color=feed["badge_color"],
                    allowed_route_types=feed.get("allowed_route_types"),
                )
                new_poles = conv.get("poles", [])
                if new_poles:
                    route_lines = extract_routes_from_gtfs(
                        zip_path=zip_path,
                        op_type=feed["operator_type"],
                        op_short=feed["operator_short"],
                        op_name_default=feed["label"],
                        id_prefix=prefix,
                        eps=0.00010,
                        allowed_route_types=feed.get("allowed_route_types"),
                    )
                    i18n_map = extract_i18n_from_zip(zip_path)
            except Exception as exc:
                conv_err = f"{type(exc).__name__}: {exc}"
                new_poles = []

        if new_poles:
            for p in new_poles:
                p["pole_id"] = prefix + p["pole_id"]
                p["operator_name"] = feed["label"]
                p["feed_source_id"] = fid
                p["feed_license"] = feed["license"]
                p["feed_attribution"] = feed["attribution"]
                p["feed_official_url"] = feed["official_url"]
                if validity.get("valid_from"):
                    p["feed_valid_from"] = validity["valid_from"]
                if validity.get("valid_to"):
                    p["feed_valid_to"] = validity["valid_to"]
                if validity.get("feed_version"):
                    p["feed_version"] = validity["feed_version"]
                if validity.get("expired_on_ref"):
                    p["feed_expired"] = True
                if feed.get("rt_feed"):
                    p["rt_feed"] = feed["rt_feed"]
                for r in p.get("routes", []):
                    r["route_id"] = prefix + r["route_id"]
                    if r.get("prev_pole_id"):
                        r["prev_pole_id"] = prefix + r["prev_pole_id"]
                    if r.get("next_pole_id"):
                        r["next_pole_id"] = prefix + r["next_pole_id"]
                poles_by_pref[pref][p["pole_id"]] = p

            routes_by_pref[pref].extend(route_lines)
            for k, v in i18n_map.items():
                i18n_by_pref[pref].setdefault(k, v)
            rec = {
                "id": fid, "pref": pref, "label": feed["label"], "status": "ok",
                "poles": len(new_poles), "routes": len(route_lines), "i18n": len(i18n_map),
                "valid_from": validity.get("valid_from", ""), "valid_to": validity.get("valid_to", ""),
                "expired_on_ref": bool(validity.get("expired_on_ref")),
                "source_url": matched_url, "license": feed["license"],
            }
            report["feeds"].append(rec)
            print(
                f"[BUNDLED-OK] pref_{pref:02d} {feed['label']}: poles={len(new_poles)}, "
                f"routes={len(route_lines)}, valid={rec['valid_from']}..{rec['valid_to']}"
            )
            continue

        # 既存 bundled_odpt からのフォールバック
        prev_poles = {k: v for k, v in load_pref_poles_z(out_bundled_dir, pref).items() if k.startswith(prefix)}
        prev_routes = [r for r in load_pref_routes(out_bundled_dir, pref, compressed=True) if str(r.get("id", "")).startswith(prefix)]
        if prev_poles:
            vf, vt = poles_calendar_range(list(prev_poles.values()))
            is_expired = bool(vt and ref > vt)
            for pid, p in prev_poles.items():
                if is_expired:
                    p["feed_expired"] = True
                poles_by_pref[pref][pid] = p
            routes_by_pref[pref].extend(prev_routes)
            status = "fallback_expired" if is_expired else "fallback_valid"
            report["feeds"].append({
                "id": fid, "pref": pref, "label": feed["label"], "status": status,
                "poles": len(prev_poles), "valid_from": vf, "valid_to": vt, "error": conv_err or err,
            })
            print(f"[BUNDLED-{status.upper()}] pref_{pref:02d} {feed['label']}: poles={len(prev_poles)}")
        else:
            report["feeds"].append({
                "id": fid, "pref": pref, "label": feed["label"], "status": "failed_no_fallback",
                "poles": 0, "error": conv_err or err,
            })
            print(f"[BUNDLED-FAIL] pref_{pref:02d} {feed['label']}: {conv_err or err}")

    # 書き出し
    manifest_prefs: List[Dict] = []
    for pref in sorted(poles_by_pref.keys()):
        p_list = list(poles_by_pref[pref].values())
        entry = write_pref_shards_z(pref, p_list, out_bundled_dir)
        if not entry:
            continue
        entry["operators"] = summarize_pref_operators(p_list, feed_meta_by_prefix)
        manifest_prefs.append(entry)
        if routes_by_pref[pref]:
            write_json_z(
                out_bundled_dir / "routes" / f"pref_{pref:02d}.json.z",
                {"pref": pref, "routes": routes_by_pref[pref]},
            )
        if i18n_by_pref[pref]:
            write_json_z(
                out_bundled_dir / "i18n" / f"pref_{pref:02d}.json.z",
                i18n_by_pref[pref],
            )

    manifest = {
        "schema_version": "1.0",
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "license_scope": "odpt_basic_license_app_bundled_only",
        "prefs": manifest_prefs,
    }
    (out_bundled_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_z = sum(f.stat().st_size for f in out_bundled_dir.rglob("*.z"))
    report["total_bundled_prefs"] = len(manifest_prefs)
    report["total_bundled_poles"] = sum(p["pole_count"] for p in manifest_prefs)
    report["total_compressed_mb"] = round(total_z / (1024 * 1024), 2)
    print(
        f"BUNDLED_ODPT_DONE prefs={report['total_bundled_prefs']} "
        f"poles={report['total_bundled_poles']} size={report['total_compressed_mb']}MB"
    )
    return report


def verify_preservation(published_data_dir: Path, staging_data_dir: Path) -> Tuple[bool, List[str]]:
    """公開版と新規生成版を事業者単位で比較し、公開対象事業者の想定外消失を検知する。"""
    issues: List[str] = []
    pub_manifest = json.loads((published_data_dir / "manifest.json").read_text(encoding="utf-8"))
    stg_manifest = json.loads((staging_data_dir / "manifest.json").read_text(encoding="utf-8"))
    stg_prefs = {int(p["pref"]): p for p in stg_manifest.get("prefs", [])}

    for p in pub_manifest.get("prefs", []):
        pcode = int(p["pref"])
        pub_poles = load_pref_poles_plain(published_data_dir, pcode)
        pub_public_ops: Counter = Counter()
        for pid, pole in pub_poles.items():
            if pid.startswith(NON_PUBLIC_POLE_PREFIXES):
                continue
            pub_public_ops[pole.get("operator_name", "?")] += 1
        if not pub_public_ops:
            continue

        if pcode not in stg_prefs:
            issues.append(f"pref_{pcode:02d} vanished (had {sum(pub_public_ops.values())} public poles)")
            continue
        stg_poles = load_pref_poles_plain(staging_data_dir, pcode)
        stg_ops: Counter = Counter(pole.get("operator_name", "?") for pole in stg_poles.values())

        # 非公開プレフィックスが紛れ込んでいないことも同時に検査
        leaked = [pid for pid in stg_poles.keys() if pid.startswith(NON_PUBLIC_POLE_PREFIXES)]
        if leaked:
            issues.append(f"pref_{pcode:02d} leaked {len(leaked)} non-public poles (sample={leaked[0]})")

        for op_name, prev_cnt in pub_public_ops.items():
            cur_cnt = stg_ops.get(op_name, 0)
            if cur_cnt == 0 and prev_cnt >= 5:
                issues.append(f"pref_{pcode:02d} operator vanished: {op_name} ({prev_cnt} -> 0)")

    return len(issues) == 0, issues


def main() -> None:
    root = SCRIPT_DIR.parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["public", "bundled-odpt", "verify-preservation"], required=True)
    parser.add_argument("--staging-data-dir", type=Path, default=root / "national-data-site" / "data")
    parser.add_argument("--published-data-dir", type=Path, default=root / "national-data-site" / "data")
    parser.add_argument("--out-bundled-dir", type=Path, default=root / "ios-app" / "BusTimetableNavi" / "Web" / "data" / "bundled_odpt")
    parser.add_argument("--raw-dir", type=Path, default=root / "data-pipeline" / "raw" / "extra_feeds")
    parser.add_argument("--cached-raw-dir", type=Path, default=root / "data-pipeline" / "raw" / "odpt_and_rt")
    parser.add_argument("--catalog-file", type=Path)
    parser.add_argument("--ref-ymd", default="")
    parser.add_argument("--use-cached-raw", action="store_true")
    parser.add_argument("--simulate-fail", default="", help="カンマ区切りのフィードID（失敗テスト用）")
    parser.add_argument("--report-json", type=Path)
    args = parser.parse_args()

    sim_fail = {x.strip() for x in args.simulate_fail.split(",") if x.strip()}
    ref = args.ref_ymd.strip() or today_jst_ymd()

    if args.mode == "public":
        rep = run_public_sync(
            staging_data_dir=args.staging_data_dir,
            published_data_dir=args.published_data_dir,
            raw_dir=args.raw_dir,
            catalog_file=args.catalog_file,
            ref_ymd=ref,
            use_cached_raw=args.use_cached_raw,
            cached_raw_dir=args.cached_raw_dir,
            simulate_fail_ids=sim_fail,
        )
        if args.report_json:
            args.report_json.parent.mkdir(parents=True, exist_ok=True)
            args.report_json.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.mode == "bundled-odpt":
        api_key = os.environ.get("ODPT_API_KEY", "").strip()
        rep = run_bundled_odpt_build(
            out_bundled_dir=args.out_bundled_dir,
            raw_dir=args.raw_dir,
            api_key=api_key,
            ref_ymd=ref,
            use_cached_raw=args.use_cached_raw,
            cached_raw_dir=args.cached_raw_dir,
            simulate_fail_ids=sim_fail,
        )
        if args.report_json:
            args.report_json.parent.mkdir(parents=True, exist_ok=True)
            args.report_json.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.mode == "verify-preservation":
        ok, issues = verify_preservation(args.published_data_dir, args.staging_data_dir)
        if not ok:
            print("PRESERVATION_CHECK_FAILED:")
            for i in issues:
                print("  -", i)
            sys.exit(1)
        print("PRESERVATION_CHECK_OK")


if __name__ == "__main__":
    main()
