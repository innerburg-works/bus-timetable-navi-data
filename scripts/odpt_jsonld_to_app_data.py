#!/usr/bin/env python3
"""
ODPT JSON-LD API (odpt:BusstopPole / odpt:BusroutePattern / odpt:BusTimetable /
odpt:BusroutePatternFare) を「バス時刻表ナビ」アプリ用データ（poles + routes）へ変換するモジュール。

GTFS-JP が提供されていない主要事業者（東急バス・相鉄バス・国際興業バス・東武バス・神奈川中央交通 等）の
バス停・路線図・時刻表・運賃を取り込むために使用します。
"""

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from build_tokyo_routes import simplify_dp
from gtfs_to_app_data import calc_bearing_arrow, normalize_hhmm, pick_direction_icon


def rows_for_operator(rows: List[Dict], operator_id: str) -> List[Dict]:
    """Bulk dumps may ignore the operator URL parameter, including cached dumps."""
    wanted = f"odpt.Operator:{operator_id}"
    selected = []
    for row in rows:
        operators = row.get("odpt:operator")
        if operators:
            if wanted in (operators if isinstance(operators, list) else [operators]):
                selected.append(row)
        else:
            # Some types omit operator but carry it in an ODPT resource identifier.
            uri = row.get("owl:sameAs") or row.get("odpt:fromBusroutePattern") or ""
            if uri.partition(":")[2].startswith(operator_id + "."):
                selected.append(row)
    return selected


def fetch_odpt_jsonld(
    api_base: str,
    rdf_type: str,
    operator_id: str,
    api_key: str,
    cache_path: Optional[Path] = None,
    timeout: int = 90,
) -> List[Dict]:
    """ODPT JSON-LD 全件取得（まず .json ダンプAPIを試行し、失敗時は通常エンドポイントへフォールバック）。"""
    if cache_path and cache_path.exists() and cache_path.stat().st_size > 20:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                scoped = rows_for_operator(data, operator_id)
                if scoped:
                    return scoped
        except Exception:
            cache_path.unlink(missing_ok=True)

    params = {"odpt:operator": f"odpt.Operator:{operator_id}"}
    if api_key:
        params["acl:consumerKey"] = api_key
    qs = urllib.parse.urlencode(params)

    candidates = [
        f"{api_base.rstrip('/')}/{rdf_type}.json?{qs}",
        f"{api_base.rstrip('/')}/{rdf_type}?{qs}",
    ]
    last_err: Optional[Exception] = None
    for url in candidates:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "BusTimetableNavi/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read().decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                data = rows_for_operator(data, operator_id)
                if not data:
                    continue
                if cache_path:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                return data
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (401, 403):
                raise
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    return []


def classify_odpt_calendar(cal_uri: str) -> Tuple[Set[str], str]:
    """odpt:Calendar URI から運行区分セット ({weekday, saturday, holiday}) と曜日ビットを推定する。"""
    low = (cal_uri or "").lower()
    if "saturdayholiday" in low or "saturdayandholiday" in low:
        return {"saturday", "holiday"}, "0000011"
    if "saturday" in low or ".sat" in low or "土曜" in cal_uri:
        return {"saturday"}, "0000010"
    if "holiday" in low or "sunday" in low or ".hol" in low or ".sun" in low or "日曜" in cal_uri or "休日" in cal_uri:
        return {"holiday"}, "0000001"
    if "everyday" in low or "daily" in low:
        return {"weekday", "saturday", "holiday"}, "1111111"
    return {"weekday"}, "1111100"


def _extract_route_name(pattern_obj: Dict) -> Tuple[str, str]:
    title = (pattern_obj.get("dc:title") or "").strip()
    note = (pattern_obj.get("odpt:note") or "").strip()
    busroute = (pattern_obj.get("odpt:busroute") or "").split(":")[-1]
    short_name = ""
    if title:
        # 例: "渋11 田園調布駅行" -> "渋11"
        m = re.match(r"^([^\s　:：]+)", title)
        if m:
            short_name = m.group(1)
    if not short_name and note:
        short_name = note.split(":")[0].strip()
    if not short_name:
        short_name = busroute.split(".")[-1] if "." in busroute else busroute
    return short_name or "バス", title or note


def _summarize_pattern_fares(fare_rows: List[Dict], default_pay: Optional[str] = None) -> Dict[str, Dict]:
    """odpt:BusroutePatternFare 配列から路線パターン別の運賃オブジェクトを生成。"""
    by_pattern: Dict[str, Set[int]] = defaultdict(set)
    for row in fare_rows:
        pat = (row.get("odpt:fromBusroutePattern") or row.get("odpt:toBusroutePattern") or "").strip()
        price = row.get("odpt:ticketFare")
        if price is None:
            price = row.get("odpt:icCardFare")
        if not pat or price is None:
            continue
        try:
            yen = int(round(float(price)))
        except (TypeError, ValueError):
            continue
        if yen >= 0:
            by_pattern[pat].add(yen)

    out: Dict[str, Dict] = {}
    for pat, prices in by_pattern.items():
        pos = sorted(p for p in prices if p > 0)
        if len(prices) == 1:
            obj: Dict = {"t": "flat", "p": next(iter(prices))}
            if default_pay:
                obj["pay"] = default_pay
            out[pat] = obj
        else:
            obj = {"t": "dist", "pay": "alight"}
            if pos:
                obj["min"] = pos[0]
            out[pat] = obj
    return out


def convert_odpt_jsonld(
    poles_raw: List[Dict],
    patterns_raw: List[Dict],
    timetables_raw: List[Dict],
    fares_raw: Optional[List[Dict]],
    operator_name: str,
    operator_type: str,
    operator_short: str,
    default_timetable_url: str,
    badge_color: str,
    id_prefix: str,
    supports_realtime: bool = False,
    rt_feed: Optional[str] = None,
) -> Tuple[List[Dict], List[Dict]]:
    """
    ODPT JSON-LD 配列群を (poles, route_polylines) へ変換する。
    """
    stop_meta: Dict[str, Dict] = {}
    for s in poles_raw:
        same_as = (s.get("owl:sameAs") or s.get("@id") or "").strip()
        if not same_as:
            continue
        try:
            lat = float(s.get("geo:lat") or 0)
            lng = float(s.get("geo:long") or 0)
        except (TypeError, ValueError):
            continue
        if not (20.0 <= lat <= 46.5 and 122.0 <= lng <= 154.5):
            continue
        title = s.get("dc:title") or ""
        if not title and isinstance(s.get("title"), dict):
            title = s["title"].get("ja") or s["title"].get("en") or ""
        platform = str(s.get("odpt:platformNumber") or s.get("odpt:busstopPoleNumber") or "").strip()
        stop_meta[same_as] = {
            "stop_id": same_as,
            "stop_name": title.strip() or same_as.split(".")[-1],
            "platform_code": platform,
            "lat": lat,
            "lng": lng,
        }

    pattern_meta: Dict[str, Dict] = {}
    route_polylines: List[Dict] = []
    seen_route_keys: Set[str] = set()

    for p_idx, pat in enumerate(patterns_raw):
        pat_id = (pat.get("owl:sameAs") or pat.get("@id") or "").strip()
        if not pat_id:
            continue
        r_short, r_long = _extract_route_name(pat)
        order_raw = pat.get("odpt:busstopPoleOrder") or []
        order_sorted = sorted(order_raw, key=lambda x: int(x.get("odpt:index", 0) or 0))
        pole_seq = [
            (o.get("odpt:busstopPole") or "").strip()
            for o in order_sorted
            if (o.get("odpt:busstopPole") or "").strip()
        ]
        pattern_meta[pat_id] = {
            "pat_id": pat_id,
            "route_name": r_short,
            "route_long": r_long,
            "pole_seq": pole_seq,
        }

        # 路線ポリライン抽出（ug:region GeoJSON LineString 優先、なければ停留所座標列）
        pts: List[Tuple[float, float]] = []
        region = pat.get("ug:region")
        if isinstance(region, dict) and region.get("type") == "LineString":
            for coord in region.get("coordinates") or []:
                if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                    lng, lat = float(coord[0]), float(coord[1])
                    if 20.0 <= lat <= 46.5 and 122.0 <= lng <= 154.5:
                        pts.append((lat, lng))
        if len(pts) < 2 and pole_seq:
            pts = [(stop_meta[sid]["lat"], stop_meta[sid]["lng"]) for sid in pole_seq if sid in stop_meta]

        if len(pts) >= 2:
            # 同一系統・同一方向の重複を抑制
            rk = f"{r_short}|{pole_seq[0] if pole_seq else ''}|{pole_seq[-1] if pole_seq else ''}"
            if rk not in seen_route_keys:
                seen_route_keys.add(rk)
                simp = simplify_dp(pts, eps=0.00010)
                if len(simp) >= 2:
                    lats = [x[0] for x in simp]
                    lngs = [x[1] for x in simp]
                    route_polylines.append({
                        "id": f"{id_prefix}{p_idx}_{hashlib.md5(pat_id.encode('utf-8')).hexdigest()[:6]}",
                        "name": r_short,
                        "long": r_long,
                        "op_type": operator_type,
                        "op_short": operator_short,
                        "op_name": operator_name,
                        "color": badge_color,
                        "bbox": [round(min(lats), 5), round(min(lngs), 5), round(max(lats), 5), round(max(lngs), 5)],
                        "pts": [[round(lat, 5), round(lng, 5)] for lat, lng in simp],
                    })

    pattern_fares = _summarize_pattern_fares(fares_raw or [])

    # pole_uri -> (route_name, dest_stop) -> 集計オブジェクト
    pole_routes: Dict[str, Dict[Tuple[str, str], Dict]] = defaultdict(dict)
    cal_defs: Dict[str, Dict] = {}

    for tt in timetables_raw:
        pat_id = (tt.get("odpt:busroutePattern") or "").strip()
        p_info = pattern_meta.get(pat_id)
        objs = tt.get("odpt:busTimetableObject") or []
        if not objs:
            continue
        objs_sorted = sorted(objs, key=lambda x: int(x.get("odpt:index", 0) or 0))
        seq_entries: List[Tuple[str, str, str]] = []
        for o in objs_sorted:
            sid = (o.get("odpt:busstopPole") or "").strip()
            dep = normalize_hhmm(o.get("odpt:departureTime") or o.get("odpt:arrivalTime") or "")
            if not sid or not dep or sid not in stop_meta:
                continue
            dest_sign = (o.get("odpt:destinationSign") or "").strip()
            seq_entries.append((sid, dep, dest_sign))
        if len(seq_entries) < 2:
            continue

        cal_uri = (tt.get("odpt:calendar") or "odpt.Calendar:Weekday").strip()
        day_types, cal_bits = classify_odpt_calendar(cal_uri)
        cal_key = cal_uri.split(":")[-1] or "Weekday"
        if cal_key not in cal_defs:
            cal_defs[cal_key] = {"d": cal_bits}

        r_name = p_info["route_name"] if p_info else (tt.get("dc:title") or "バス").split()[0]
        origin_name = stop_meta[seq_entries[0][0]]["stop_name"]
        fallback_dest = stop_meta[seq_entries[-1][0]]["stop_name"]
        r_fare = pattern_fares.get(pat_id)

        for idx, (sid, dep, dest_sign) in enumerate(seq_entries):
            if idx + 1 >= len(seq_entries):
                continue  # 終点は乗車対象外
            prev_sid = seq_entries[idx - 1][0] if idx > 0 else None
            next_sid = seq_entries[idx + 1][0]
            dest_stop = dest_sign or fallback_dest
            route_key = (r_name, dest_stop)
            if route_key not in pole_routes[sid]:
                sid_hash = hashlib.md5(sid.encode("utf-8")).hexdigest()[:8]
                dest_hash = hashlib.md5(dest_stop.encode("utf-8")).hexdigest()[:4]
                r_hash = hashlib.md5(r_name.encode("utf-8")).hexdigest()[:6]
                pole_routes[sid][route_key] = {
                    "route_id": f"{id_prefix}{r_hash}_{sid_hash}_{dest_hash}",
                    "gtfs_route_id": pat_id or r_name,
                    "route_name": r_name,
                    "badge_color": badge_color,
                    "icon": pick_direction_icon(dest_stop),
                    "origin_stop_name": origin_name,
                    "destination_stop_name": dest_stop,
                    "prev_stop_name": stop_meta[prev_sid]["stop_name"] if prev_sid else None,
                    "prev_pole_id": f"{id_prefix}{hashlib.md5(prev_sid.encode('utf-8')).hexdigest()[:12]}" if prev_sid else None,
                    "next_stop_name": stop_meta[next_sid]["stop_name"],
                    "next_pole_id": f"{id_prefix}{hashlib.md5(next_sid.encode('utf-8')).hexdigest()[:12]}",
                    "timetable": {"weekday": set(), "saturday": set(), "holiday": set()},
                    "svc_times": defaultdict(set),
                    "_next_raw_sid": next_sid,
                }
                if r_fare:
                    pole_routes[sid][route_key]["fare"] = r_fare
            elif r_fare and "fare" not in pole_routes[sid][route_key]:
                pole_routes[sid][route_key]["fare"] = r_fare

            for dt in day_types:
                pole_routes[sid][route_key]["timetable"][dt].add(dep)
            pole_routes[sid][route_key]["svc_times"][cal_key].add(dep)

    poles_out: List[Dict] = []
    for sid, r_map in pole_routes.items():
        s = stop_meta[sid]
        pole_id = f"{id_prefix}{hashlib.md5(sid.encode('utf-8')).hexdigest()[:12]}"
        arrow = "↗"
        used_cals: Set[str] = set()
        routes_list: List[Dict] = []
        for r_obj in r_map.values():
            nxt_raw = r_obj.pop("_next_raw_sid", None)
            if nxt_raw and nxt_raw in stop_meta:
                nxt = stop_meta[nxt_raw]
                arrow = calc_bearing_arrow(s["lat"], s["lng"], nxt["lat"], nxt["lng"])
            tt = r_obj["timetable"]
            r_obj["timetable"] = {
                "weekday": sorted(tt["weekday"]),
                "saturday": sorted(tt["saturday"]),
                "holiday": sorted(tt["holiday"]),
            }
            st_map = {k: sorted(v) for k, v in r_obj["svc_times"].items() if v}
            r_obj["svc_times"] = st_map
            used_cals.update(st_map.keys())
            routes_list.append(r_obj)

        pole_doc = {
            "pole_id": pole_id,
            "stop_name": s["stop_name"],
            "lat": s["lat"],
            "lng": s["lng"],
            "direction_arrow": arrow,
            "operator_name": operator_name,
            "operator_type": operator_type,
            "operator_short": operator_short,
            "supports_realtime": supports_realtime,
            "timetable_url": default_timetable_url,
            "svc_cal": {k: cal_defs[k] for k in sorted(used_cals) if k in cal_defs},
            "routes": routes_list,
        }
        if rt_feed:
            pole_doc["rt_feed"] = rt_feed
        if s.get("platform_code"):
            pole_doc["platform_code"] = s["platform_code"]
        poles_out.append(pole_doc)

    return poles_out, route_polylines
