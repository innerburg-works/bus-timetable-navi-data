#!/usr/bin/env python3
"""
ODPT 公開 GBFS（ドコモ・バイクシェア全国版 + OpenStreet HELLO CYCLING）から、
都道府県別の軽量シェアサイクルポートJSON（national-data-site/data/bikeshare/pref_XX.json）を生成する。

- リアルタイム台数は Cloudflare Workers (/bikeshare) でライブ取得するが、
  プロキシ未デプロイ時やオフライン時でもポート位置を即時表示できるようにするための静的フォールバックデータ。
"""

import argparse
import json
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DOCOMO_INFO_URL = "https://api-public.odpt.org/api/v4/gbfs/docomo-cycle/station_information.json"
HELLO_INFO_URL = "https://api-public.odpt.org/api/v4/gbfs/hellocycling/station_information.json"

# 47都道府県代表中心（住所・都道府県名マッチ失敗時の最寄りフォールバック用）
PREF_CENTERS: Dict[int, Tuple[str, float, float]] = {
    1: ("北海道", 43.064, 141.347), 2: ("青森県", 40.824, 140.740), 3: ("岩手県", 39.704, 141.153),
    4: ("宮城県", 38.269, 140.872), 5: ("秋田県", 39.719, 140.102), 6: ("山形県", 38.240, 140.363),
    7: ("福島県", 37.750, 140.468), 8: ("茨城県", 36.342, 140.447), 9: ("栃木県", 36.566, 139.884),
    10: ("群馬県", 36.391, 139.061), 11: ("埼玉県", 35.857, 139.649), 12: ("千葉県", 35.605, 140.123),
    13: ("東京都", 35.689, 139.692), 14: ("神奈川県", 35.448, 139.642), 15: ("新潟県", 37.902, 139.023),
    16: ("富山県", 36.695, 137.211), 17: ("石川県", 36.594, 136.626), 18: ("福井県", 36.065, 136.222),
    19: ("山梨県", 35.664, 138.568), 20: ("長野県", 36.651, 138.181), 21: ("岐阜県", 35.391, 136.722),
    22: ("静岡県", 34.977, 138.383), 23: ("愛知県", 35.180, 136.907), 24: ("三重県", 34.730, 136.509),
    25: ("滋賀県", 35.004, 135.869), 26: ("京都府", 35.021, 135.756), 27: ("大阪府", 34.686, 135.520),
    28: ("兵庫県", 34.691, 135.183), 29: ("奈良県", 34.685, 135.833), 30: ("和歌山県", 34.226, 135.167),
    31: ("鳥取県", 35.504, 134.238), 32: ("島根県", 35.472, 133.050), 33: ("岡山県", 34.662, 133.935),
    34: ("広島県", 34.397, 132.460), 35: ("山口県", 34.186, 131.471), 36: ("徳島県", 34.066, 134.559),
    37: ("香川県", 34.340, 134.043), 38: ("愛媛県", 33.842, 132.766), 39: ("高知県", 33.560, 133.531),
    40: ("福岡県", 33.606, 130.418), 41: ("佐賀県", 33.249, 130.299), 42: ("長崎県", 32.745, 129.874),
    43: ("熊本県", 32.790, 130.742), 44: ("大分県", 33.238, 131.613), 45: ("宮崎県", 31.911, 131.424),
    46: ("鹿児島県", 31.560, 130.558), 47: ("沖縄県", 26.212, 127.681),
}


def detect_pref(lat: float, lng: float, address: str = "") -> int:
    if address:
        for code, (name, _, _) in PREF_CENTERS.items():
            if name in address:
                return code
    best_code = 13
    best_d = 1e9
    for code, (_, clat, clng) in PREF_CENTERS.items():
        d = (lat - clat) ** 2 + (lng - clng) ** 2
        if d < best_d:
            best_d = d
            best_code = code
    return best_code


def fetch_json(url: str) -> Dict:
    req = urllib.request.Request(url, headers={"User-Agent": "BusTimetableNavi/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    root = Path(__file__).resolve().parent.parent.parent
    parser = argparse.ArgumentParser(description="ODPT公開GBFSシェアサイクル都道府県別静的データ生成")
    parser.add_argument("--out-dir", type=Path, default=root / "national-data-site" / "data" / "bikeshare")
    parser.add_argument("--bundle-tokyo-out", type=Path, default=root / "ios-app" / "BusTimetableNavi" / "Web" / "data" / "bikeshare" / "pref_13.json")
    args = parser.parse_args()

    by_pref: Dict[int, List[Dict]] = defaultdict(list)

    for prov, url in (("docomo", DOCOMO_INFO_URL), ("hello", HELLO_INFO_URL)):
        data = fetch_json(url)
        stations = data.get("data", {}).get("stations", [])
        cnt = 0
        for s in stations:
            try:
                lat = float(s.get("lat"))
                lng = float(s.get("lon"))
            except (TypeError, ValueError):
                continue
            if not (20.0 <= lat <= 46.5 and 122.0 <= lng <= 154.5):
                continue
            sid = str(s.get("station_id") or "").strip()
            name = str(s.get("name") or "").strip()
            if not sid or not name:
                continue
            addr = str(s.get("address") or "").strip()
            cap_raw = s.get("capacity") if s.get("capacity") is not None else s.get("vehicle_capacity")
            try:
                cap: Optional[int] = int(cap_raw) if cap_raw is not None and str(cap_raw).strip() != "" else None
            except ValueError:
                cap = None
            uris = s.get("rental_uris") if isinstance(s.get("rental_uris"), dict) else {}
            r_url = str(uris.get("ios") or uris.get("web") or "").strip()
            pref = detect_pref(lat, lng, addr)
            item: Dict = {
                "id": f"{prov}_{sid}",
                "p": prov,
                "n": name,
                "lat": round(lat, 6),
                "lng": round(lng, 6),
            }
            if cap is not None:
                item["cap"] = cap
            if r_url:
                item["url"] = r_url
            by_pref[pref].append(item)
            cnt += 1
        print(f"Fetched {prov}: {cnt} stations")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for pref, st_list in sorted(by_pref.items()):
        out_file = args.out_dir / f"pref_{pref:02d}.json"
        payload = {"pref": pref, "count": len(st_list), "stations": st_list}
        out_file.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        total += len(st_list)

    if 13 in by_pref and args.bundle_tokyo_out:
        args.bundle_tokyo_out.parent.mkdir(parents=True, exist_ok=True)
        args.bundle_tokyo_out.write_text(
            (args.out_dir / "pref_13.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    print(
        f"BIKESHARE_BUILD_OK prefs={len(by_pref)} total={total} "
        f"tokyo={len(by_pref.get(13, []))} kyoto={len(by_pref.get(26, []))}"
    )


if __name__ == "__main__":
    main()
