#!/usr/bin/env python3
"""
GTFS-JP (ZIP) -> 「バス時刻表ナビ」アプリ用JSON変換パイプライン（全国共通）

都営バス・各地の市営バス・私営バス・コミュニティバスの GTFS-JP (ZIP) を読み込み、
のりば（stop_id）単位で以下を生成します:
  - バス停名・緯度経度・運行会社・運行会社タイプ・公式時刻表URL
  - 進行方向矢印（次バス停への方位角から自動算出）
  - 系統ごとの始発・終点・一つ前のバス停・次のバス停（および遷移用pole_id）
  - 平日 / 土曜 / 日祝の停車時刻配列（HH:MM）
  - 方面イメージのミニアイコン自動付与
"""

import argparse
import csv
import hashlib
import io
import json
import math
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


ICON_RULES: List[Tuple[List[str], str]] = [
    (["上野", "動物園"], "🐼"),
    (["新宿", "渋谷", "池袋", "品川", "東京駅", "大手町", "丸の内"], "🏙️"),
    (["九段", "役所", "都庁", "市役所", "庁舎"], "🏛️"),
    (["車庫", "営業所", "操車場"], "🚌"),
    (["病院", "医療", "クリニック"], "🏥"),
    (["大学", "高校", "学園"], "🎓"),
    (["公園", "庭園", "植物園"], "🌳"),
    (["銀座", "日本橋", "表参道", "原宿"], "🛍️"),
    (["港", "埠頭", "お台場", "豊洲", "フェリー"], "⚓"),
    (["駅"], "🚉"),
]


def pick_direction_icon(dest_name: str) -> str:
    for keywords, icon in ICON_RULES:
        if any(k in dest_name for k in keywords):
            return icon
    return "🚌"


def calc_bearing_arrow(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """現在バス停から次のバス停への方位角から8方向矢印を算出"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"]
    idx = int((deg + 22.5) // 45) % 8
    return arrows[idx]


def read_csv_from_zip(zf: zipfile.ZipFile, filename: str) -> List[Dict[str, str]]:
    if filename not in zf.namelist():
        return []
    with zf.open(filename) as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig")
        return list(csv.DictReader(text))


def build_service_calendars(
    calendar_rows: List[Dict[str, str]],
    calendar_dates_rows: List[Dict[str, str]],
) -> Dict[str, Dict]:
    """service_id ごとに正確な GTFS 運行規則 (曜日ビット・開始/終了日・追加/運休例外日) を構築"""
    cals: Dict[str, Dict] = {}
    day_keys = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for row in calendar_rows:
        sid = (row.get("service_id") or "").strip()
        if not sid:
            continue
        bits = "".join("1" if row.get(k) == "1" else "0" for k in day_keys)
        cals[sid] = {
            "d": bits,
            "s": (row.get("start_date") or "").strip(),
            "e": (row.get("end_date") or "").strip(),
            "a": [],
            "r": [],
        }

    for row in calendar_dates_rows:
        sid = (row.get("service_id") or "").strip()
        ex_type = (row.get("exception_type") or "").strip()
        ymd = (row.get("date") or "").strip()
        if not sid or len(ymd) != 8 or ex_type not in ("1", "2"):
            continue
        if sid not in cals:
            cals[sid] = {"d": "0000000", "s": "", "e": "", "a": [], "r": []}
        if ex_type == "1":
            cals[sid]["a"].append(ymd)
        else:
            cals[sid]["r"].append(ymd)

    # 重複除去・ソートし、空フィールドは省略してコンパクト化
    compact: Dict[str, Dict] = {}
    for sid, c in cals.items():
        obj: Dict = {"d": c["d"]}
        if c["s"]:
            obj["s"] = c["s"]
        if c["e"]:
            obj["e"] = c["e"]
        if c["a"]:
            obj["a"] = sorted(set(c["a"]))
        if c["r"]:
            obj["r"] = sorted(set(c["r"]))
        compact[sid] = obj
    return compact


def classify_services(
    calendar_rows: List[Dict[str, str]],
    calendar_dates_rows: List[Dict[str, str]],
) -> Dict[str, Set[str]]:
    """service_id ごとに有効な運行区分セット ({weekday, saturday, holiday}) を返す。
    - 毎日運行 (mon..sun=1) は全区分に含める（従来 elif で日曜が消えていた不具合を修正）
    - 適用期間切れ (end_date < today) は、他に有効な便がある場合は除外
    - calendar_dates.txt のみで定義された service_id は追加日 (exception_type=1) の曜日から判定
    """
    today_ymd = datetime.now().strftime("%Y%m%d")
    service_day_types: Dict[str, Set[str]] = defaultdict(set)
    active_sids: Set[str] = set()
    expired_sids: Set[str] = set()

    for row in calendar_rows:
        sid = (row.get("service_id") or "").strip()
        if not sid:
            continue
        end_date = (row.get("end_date") or "").strip()
        if end_date and len(end_date) == 8 and end_date < today_ymd:
            expired_sids.add(sid)
        else:
            active_sids.add(sid)

        days: Set[str] = set()
        if any(row.get(k) == "1" for k in ("monday", "tuesday", "wednesday", "thursday", "friday")):
            days.add("weekday")
        if row.get("saturday") == "1":
            days.add("saturday")
        if row.get("sunday") == "1":
            days.add("holiday")

        if not days:
            low = sid.lower()
            if "sat" in low or "土" in sid:
                days.add("saturday")
            elif "hol" in low or "sun" in low or "休" in sid or "祝" in sid:
                days.add("holiday")
            else:
                days.add("weekday")
        service_day_types[sid].update(days)

    # calendar_dates.txt (exception_type=1: 追加運行) を反映
    # 定期運行(calendar.txt)で既に曜日区分を持つサービスに特定日だけ追加された例外日は、一般曜日区分へ混ぜない
    regular_cal_sids = set(service_day_types.keys())
    for row in calendar_dates_rows:
        sid = (row.get("service_id") or "").strip()
        ex_type = (row.get("exception_type") or "").strip()
        ymd = (row.get("date") or "").strip()
        if not sid or ex_type != "1" or len(ymd) != 8:
            continue
        if ymd >= today_ymd:
            active_sids.add(sid)
        if sid in regular_cal_sids:
            continue
        try:
            dow = datetime.strptime(ymd, "%Y%m%d").weekday()  # 0=Mon..5=Sat,6=Sun
            if dow <= 4:
                service_day_types[sid].add("weekday")
            elif dow == 5:
                service_day_types[sid].add("saturday")
            else:
                service_day_types[sid].add("holiday")
        except ValueError:
            pass

    # 有効な service_id が1つ以上ある場合のみ、明確に期限切れの service_id を除外
    if active_sids:
        for sid in expired_sids:
            if sid not in active_sids:
                service_day_types.pop(sid, None)

    return service_day_types


def normalize_hhmm(raw_time: str) -> Optional[str]:
    parts = raw_time.strip().split(":")
    if len(parts) < 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
        return f"{h:02d}:{m:02d}"
    except ValueError:
        return None


def parse_yen_price(raw: str) -> Optional[int]:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        val = int(round(float(s)))
        return val if val >= 0 else None
    except ValueError:
        return None


FLAT_PREPAID_AGENCY_KEYWORDS = (
    "東京都交通局",
    "都営",
    "日立自動車交通",
    "フジエクスプレス",
    "文京区",
)


def extract_route_fares(
    fare_attr_rows: List[Dict[str, str]],
    fare_rule_rows: List[Dict[str, str]],
    agency_name: str,
    operator_short: str,
) -> Tuple[Dict[str, Dict], Optional[Dict]]:
    """GTFS の fare_attributes.txt / fare_rules.txt から系統ごとの運賃情報と既定運賃を抽出"""
    attr_by_id: Dict[str, Dict] = {}
    for row in fare_attr_rows:
        fid = (row.get("fare_id") or "").strip()
        price = parse_yen_price(row.get("price", ""))
        if not fid or price is None:
            continue
        attr_by_id[fid] = {
            "price": price,
            "payment_method": (row.get("payment_method") or "").strip(),
        }
    if not attr_by_id:
        return {}, None

    by_route: Dict[str, List[Tuple[int, bool]]] = defaultdict(list)
    global_rules: List[Tuple[int, bool]] = []
    for row in fare_rule_rows:
        fid = (row.get("fare_id") or "").strip()
        if fid not in attr_by_id:
            continue
        price = attr_by_id[fid]["price"]
        has_zone = bool((row.get("origin_id") or "").strip() or (row.get("destination_id") or "").strip())
        rid = (row.get("route_id") or "").strip()
        if rid:
            by_route[rid].append((price, has_zone))
        else:
            global_rules.append((price, has_zone))

    is_known_flat_prepaid = any(
        kw in (agency_name or "") or kw in (operator_short or "")
        for kw in FLAT_PREPAID_AGENCY_KEYWORDS
    )

    def summarize(entries: List[Tuple[int, bool]]) -> Optional[Dict]:
        if not entries:
            return None
        prices = sorted(set(p for p, _ in entries if p > 0))
        all_prices = sorted(set(p for p, _ in entries))
        has_zone = any(z for _, z in entries)
        if not has_zone and len(all_prices) == 1:
            obj: Dict = {"t": "flat", "p": all_prices[0]}
            if is_known_flat_prepaid:
                obj["pay"] = "board"
            return obj
        obj = {"t": "dist", "pay": "alight"}
        if prices:
            obj["min"] = prices[0]
        return obj

    route_fares: Dict[str, Dict] = {}
    for rid, entries in by_route.items():
        info = summarize(entries)
        if info:
            route_fares[rid] = info

    default_fare = summarize(global_rules)
    if default_fare is None and not by_route and len(attr_by_id) == 1:
        only_price = next(iter(attr_by_id.values()))["price"]
        default_fare = {"t": "flat", "p": only_price}
        if is_known_flat_prepaid:
            default_fare["pay"] = "board"

    return route_fares, default_fare


def convert_gtfs(
    gtfs_zip_path: Path,
    operator_type: str,
    operator_short: str,
    supports_realtime: bool,
    default_timetable_url: str,
    badge_color: str,
    allowed_route_types: Optional[Iterable[str]] = None,
) -> Dict:
    with zipfile.ZipFile(gtfs_zip_path, "r") as zf:
        agencies = read_csv_from_zip(zf, "agency.txt")
        stops_rows = read_csv_from_zip(zf, "stops.txt")
        routes_rows = read_csv_from_zip(zf, "routes.txt")
        trips_rows = read_csv_from_zip(zf, "trips.txt")
        stop_times_rows = read_csv_from_zip(zf, "stop_times.txt")
        calendar_rows = read_csv_from_zip(zf, "calendar.txt")
        calendar_dates_rows = read_csv_from_zip(zf, "calendar_dates.txt")
        fare_attr_rows = read_csv_from_zip(zf, "fare_attributes.txt")
        fare_rule_rows = read_csv_from_zip(zf, "fare_rules.txt")

    agency_name = agencies[0].get("agency_name", operator_short) if agencies else operator_short
    agency_url = agencies[0].get("agency_url", default_timetable_url) if agencies else default_timetable_url
    route_fares, default_fare = extract_route_fares(fare_attr_rows, fare_rule_rows, agency_name, operator_short)

    stops: Dict[str, Dict] = {}
    for s in stops_rows:
        sid = s.get("stop_id", "")
        if not sid:
            continue
        stops[sid] = {
            "stop_id": sid,
            "stop_name": s.get("stop_name", ""),
            "platform_code": (s.get("platform_code") or "").strip(),
            "lat": float(s.get("stop_lat", "0") or 0),
            "lng": float(s.get("stop_lon", "0") or 0),
            "stop_url": s.get("stop_url", "") or default_timetable_url or agency_url,
        }

    routes: Dict[str, Dict] = {}
    allowed_rt_set: Optional[Set[str]] = (
        {str(x).strip() for x in allowed_route_types if str(x).strip() != ""}
        if allowed_route_types is not None
        else None
    )
    zip_stem_low = gtfs_zip_path.stem.lower()
    is_tram_feed_name = any(
        k in zip_stem_low or k in (agency_name or "")
        for k in ("shinaidensha", "streetcar", "tram", "manyosen", "kumamotoshiden", "hakodateshiden", "市内電車", "路面電車", "市電", "万葉線")
    )
    for r in routes_rows:
        rid = r.get("route_id", "")
        if not rid:
            continue
        short_name = r.get("route_short_name") or r.get("route_long_name") or rid
        long_name = r.get("route_long_name") or ""
        color = r.get("route_color", "").strip()
        rtype = (r.get("route_type") or "3").strip()
        if allowed_rt_set is not None and rtype not in allowed_rt_set:
            continue
        route_label_all = f"{short_name} {long_name}"
        is_explicit_bus_name = ("バス" in route_label_all) or ("bus" in route_label_all.lower())
        if rtype == "4":
            vtype = "ferry"
        elif rtype == "0":
            vtype = "tram"
        elif rtype == "1":
            vtype = "subway"
        elif rtype == "2":
            vtype = "rail"
        elif is_tram_feed_name or (
            not is_explicit_bus_name
            and any(k in route_label_all for k in ("市内軌道線", "市内電車", "路面電車", "富山港線"))
        ):
            vtype = "tram"
        else:
            vtype = "bus"
        routes[rid] = {
            "route_id": rid,
            "route_name": short_name,
            "vehicle_type": vtype,
            "badge_color": f"#{color}" if color else badge_color,
        }

    service_day_types = classify_services(calendar_rows, calendar_dates_rows)
    service_cals = build_service_calendars(calendar_rows, calendar_dates_rows)

    trips: Dict[str, Dict] = {}
    for t in trips_rows:
        tid = t.get("trip_id", "")
        if not tid:
            continue
        trips[tid] = {
            "trip_id": tid,
            "route_id": t.get("route_id", ""),
            "service_id": t.get("service_id", ""),
            "headsign": t.get("trip_headsign", ""),
        }

    # trip_id ごとに停車順を復元（stop_headsign と pickup_type も保持）
    trip_stops: Dict[str, List[Tuple[int, str, str, str, str]]] = defaultdict(list)
    for st in stop_times_rows:
        tid = st.get("trip_id", "")
        sid = st.get("stop_id", "")
        dep = normalize_hhmm(st.get("departure_time") or st.get("arrival_time") or "")
        if not tid or not sid or not dep:
            continue
        try:
            seq = int(st.get("stop_sequence", "0"))
        except ValueError:
            seq = 0
        stop_hs = (st.get("stop_headsign") or "").strip()
        pickup_type = (st.get("pickup_type") or "0").strip()
        trip_stops[tid].append((seq, sid, dep, stop_hs, pickup_type))

    # pole_id(=stop_id) -> (route_id, destination_name) -> 集計情報
    pole_routes: Dict[str, Dict[Tuple[str, str], Dict]] = defaultdict(dict)

    for tid, seq_list in trip_stops.items():
        if tid not in trips or not seq_list:
            continue
        seq_list.sort(key=lambda x: x[0])
        t_info = trips[tid]
        r_info = routes.get(t_info["route_id"])
        if not r_info:
            continue

        day_types = service_day_types.get(t_info["service_id"])
        if not day_types:
            continue
        origin_stop = stops.get(seq_list[0][1], {}).get("stop_name", "")
        trip_dest_fallback = t_info["headsign"] or stops.get(seq_list[-1][1], {}).get("stop_name", "")

        for idx, (_, sid, dep, stop_hs, pickup_type) in enumerate(seq_list):
            if sid not in stops:
                continue
            # 降車専用 (pickup_type=1) や終点（次バス停なし）は乗車対象から除外
            if pickup_type == "1":
                continue
            prev_sid = seq_list[idx - 1][1] if idx > 0 else None
            next_sid = seq_list[idx + 1][1] if idx + 1 < len(seq_list) else None
            if next_sid is None:
                continue
            prev_name = stops[prev_sid]["stop_name"] if prev_sid and prev_sid in stops else None
            next_name = stops[next_sid]["stop_name"] if next_sid and next_sid in stops else None

            # stop_headsign を最優先にすることで循環系統の正確な行き先（例: 上野公園, 九段下）を反映
            dest_stop = stop_hs or trip_dest_fallback

            route_key = (r_info["route_id"], dest_stop)
            if route_key not in pole_routes[sid]:
                dest_hash = hashlib.md5(dest_stop.encode("utf-8")).hexdigest()[:4]
                r_fare = route_fares.get(r_info["route_id"]) or default_fare
                pole_routes[sid][route_key] = {
                    "route_id": f"{r_info['route_id']}_{sid}_{dest_hash}",
                    "gtfs_route_id": r_info["route_id"],
                    "route_name": r_info["route_name"],
                    "vehicle_type": r_info.get("vehicle_type", "bus"),
                    "badge_color": r_info["badge_color"],
                    "icon": pick_direction_icon(dest_stop),
                    "origin_stop_name": origin_stop,
                    "destination_stop_name": dest_stop,
                    "prev_stop_name": prev_name,
                    "prev_pole_id": prev_sid,
                    "next_stop_name": next_name,
                    "next_pole_id": next_sid,
                    "timetable": {"weekday": set(), "saturday": set(), "holiday": set()},
                    "svc_times": defaultdict(set),
                    "svc_pickup_types": defaultdict(lambda: defaultdict(set)),
                    "day_pickup_types": {
                        "weekday": defaultdict(set),
                        "saturday": defaultdict(set),
                        "holiday": defaultdict(set),
                    },
                    "trip_map": {},
                }
                if r_fare:
                    pole_routes[sid][route_key]["fare"] = r_fare
            pt_code = 2 if pickup_type == "2" else 3 if pickup_type == "3" else 0
            for dt in day_types:
                pole_routes[sid][route_key]["timetable"][dt].add(dep)
                pole_routes[sid][route_key]["day_pickup_types"][dt][dep].add(pt_code)
            if t_info["service_id"]:
                pole_routes[sid][route_key]["svc_times"][t_info["service_id"]].add(dep)
                pole_routes[sid][route_key]["svc_pickup_types"][t_info["service_id"]][dep].add(pt_code)
            if supports_realtime:
                pole_routes[sid][route_key]["trip_map"][tid] = dep

    poles_out: List[Dict] = []
    for sid, route_map in pole_routes.items():
        s = stops[sid]
        route_list = []
        used_sids: Set[str] = set()
        arrow = "↗"
        pole_vtypes: Set[str] = set()
        pole_booking_types: Set[int] = set()
        pole_any_booking = False
        pole_all_full_booking = True
        for r_obj in route_map.values():
            # 土日祝表示のため weekday / saturday / holiday を原本どおり保持しつつソート
            tt = r_obj["timetable"]
            r_obj["timetable"] = {
                "weekday": sorted(tt["weekday"]),
                "saturday": sorted(tt["saturday"]),
                "holiday": sorted(tt["holiday"]),
            }
            st_map = {k: sorted(v) for k, v in r_obj["svc_times"].items() if v}
            r_obj["svc_times"] = st_map
            used_sids.update(st_map.keys())

            svc_pt_raw = r_obj.pop("svc_pickup_types", {})
            day_pt_raw = r_obj.pop("day_pickup_types", {})
            svc_booking: Dict[str, Dict[str, int]] = {}
            total_svc_pairs = 0
            booked_svc_pairs = 0
            route_bk_codes: Set[int] = set()
            route_bk_times: Set[str] = set()
            for sv_id, dep_codes in svc_pt_raw.items():
                sv_out: Dict[str, int] = {}
                for dep_t, codes in dep_codes.items():
                    total_svc_pairs += 1
                    if 0 not in codes and (2 in codes or 3 in codes):
                        bcode = 2 if 2 in codes else 3
                        sv_out[dep_t] = bcode
                        booked_svc_pairs += 1
                        route_bk_codes.add(bcode)
                        route_bk_times.add(dep_t)
                if sv_out:
                    svc_booking[sv_id] = dict(sorted(sv_out.items()))

            booking_map: Dict[str, Dict[str, int]] = {}
            total_day_pairs = 0
            booked_day_pairs = 0
            for dt_key in ("weekday", "saturday", "holiday"):
                dt_out: Dict[str, int] = {}
                for dep_t, codes in (day_pt_raw.get(dt_key) or {}).items():
                    total_day_pairs += 1
                    if 0 not in codes and (2 in codes or 3 in codes):
                        bcode = 2 if 2 in codes else 3
                        dt_out[dep_t] = bcode
                        booked_day_pairs += 1
                        route_bk_codes.add(bcode)
                        route_bk_times.add(dep_t)
                if dt_out:
                    booking_map[dt_key] = dict(sorted(dt_out.items()))

            if svc_booking:
                r_obj["svc_booking"] = dict(sorted(svc_booking.items()))
            if booking_map:
                r_obj["booking_map"] = booking_map
            if route_bk_times:
                r_obj["booking_times"] = sorted(route_bk_times)
                r_obj["booking_types"] = sorted(route_bk_codes)
                pole_any_booking = True
                pole_booking_types.update(route_bk_codes)
                is_all_booked = (
                    (total_svc_pairs > 0 and booked_svc_pairs == total_svc_pairs)
                    or (total_svc_pairs == 0 and total_day_pairs > 0 and booked_day_pairs == total_day_pairs)
                )
                if is_all_booked:
                    r_obj["booking_required"] = True
                else:
                    r_obj["booking_partial"] = True
                    pole_all_full_booking = False
            else:
                pole_all_full_booking = False

            vt = r_obj.get("vehicle_type", "bus")
            pole_vtypes.add(vt)
            if vt == "bus":
                r_obj.pop("vehicle_type", None)
            if not supports_realtime or not r_obj["trip_map"]:
                r_obj.pop("trip_map", None)
            if r_obj["next_pole_id"] and r_obj["next_pole_id"] in stops:
                nxt = stops[r_obj["next_pole_id"]]
                arrow = calc_bearing_arrow(s["lat"], s["lng"], nxt["lat"], nxt["lng"])
            route_list.append(r_obj)

        pole_cals = {k: service_cals[k] for k in sorted(used_sids) if k in service_cals}

        pole_doc = {
            "pole_id": sid,
            "stop_name": s["stop_name"],
            "lat": s["lat"],
            "lng": s["lng"],
            "direction_arrow": arrow,
            "operator_name": agency_name,
            "operator_type": operator_type,
            "operator_short": operator_short,
            "supports_realtime": supports_realtime,
            "timetable_url": s["stop_url"],
            "svc_cal": pole_cals,
            "routes": route_list,
        }
        if s.get("platform_code"):
            pole_doc["platform_code"] = s["platform_code"]
        if pole_vtypes == {"ferry"}:
            pole_doc["vehicle_type"] = "ferry"
        elif pole_vtypes == {"tram"}:
            pole_doc["vehicle_type"] = "tram"
        elif pole_vtypes == {"subway"}:
            pole_doc["vehicle_type"] = "subway"
        elif pole_vtypes == {"rail"}:
            pole_doc["vehicle_type"] = "rail"
        if pole_any_booking:
            pole_doc["booking_types"] = sorted(pole_booking_types)
            if pole_all_full_booking and route_list:
                pole_doc["booking_required"] = True
            else:
                pole_doc["booking_partial"] = True
        poles_out.append(pole_doc)

    return {
        "schema_version": "1.0",
        "operator_name": agency_name,
        "pole_count": len(poles_out),
        "poles": poles_out,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="GTFS-JP ZIPをバス時刻表ナビ用JSONへ変換")
    parser.add_argument("--gtfs", required=True, type=Path, help="入力GTFS-JPのZIPファイルパス")
    parser.add_argument(
        "--operator-type",
        choices=["public", "private", "community"],
        default="public",
        help="運行会社タイプ",
    )
    parser.add_argument("--operator-short", default="公営", help="画面表示用の短縮ラベル（例: 都営, 横浜市営, 私営, コミュ）")
    parser.add_argument("--supports-realtime", action="store_true", help="GTFS-RT（リアルタイム接近情報）対応フラグ")
    parser.add_argument("--default-url", default="", help="stop_url未設定時の公式時刻表URL")
    parser.add_argument("--badge-color", default="#1b5e20", help="デフォルト系統バッジ色")
    parser.add_argument("--compact", action="store_true", help="余分なインデントを省いて最小サイズで出力")
    parser.add_argument("--js-var", default="", help="指定時は `window.<NAME> = ...;` 形式のJSファイルとして出力（iOS file:// 互換）")
    parser.add_argument("--output", required=True, type=Path, help="出力JSONまたはJSファイルパス")
    args = parser.parse_args()

    result = convert_gtfs(
        gtfs_zip_path=args.gtfs,
        operator_type=args.operator_type,
        operator_short=args.operator_short,
        supports_realtime=args.supports_realtime,
        default_timetable_url=args.default_url,
        badge_color=args.badge_color,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.compact:
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    else:
        serialized = json.dumps(result, ensure_ascii=False, indent=2)

    if args.js_var:
        args.output.write_text(f"window.{args.js_var} = {serialized};\n", encoding="utf-8")
    else:
        args.output.write_text(serialized, encoding="utf-8")
    print(f"Converted {result['pole_count']} poles -> {args.output}")


if __name__ == "__main__":
    main()
