#!/usr/bin/env python3
"""
全国シャード（national-data-site/data/）へ、
  1) gtfs-data.jp 公開静的GTFS のリアルタイム対応（trip_map + rt_feed）再生成 (--public-rt / --kumamoto)
  2) ODPT 公開静的GTFS-JP（APIキー不要: 仙台市営・青森市営・京福バス・群馬各社・東京コミュバス群・名古屋SRT・宇野/下津井/赤磐・小豆島等）の一括取得・マージ＋路線図生成 (--public-odpt)
  3) ODPT 認証付き静的GTFS-JP 全事業者（西武・京王・関東・小田急・西東京・横浜市営・川崎市バス・臨港・京都市営・京都バス・伊予鉄・松江市営・船木鉄道・伊豆箱根・秋葉・朝日・十王・拓殖・秋田等）＋ JSON-LD API 限定事業者（東急バス・相鉄バス）のマージ＋路線図生成 (--odpt)
  4) ODPT Challenge API 限定事業者（国際興業・東武バス・神奈川中央交通）のマージ＋路線図生成 (--odpt-challenge)
  5) 既存ダウンロード済み ODPT ZIP からの路線図ポリライン再生成 (--rebuild-existing-odpt-routes)
を行うユーティリティ。

使用例:
  # APIキー不要: ODPT 公開静的GTFS全件の取得・マージ＋既存ODPT路線図生成
  python3 national-data-site/scripts/merge_odpt_and_rt.py --public-odpt --rebuild-existing-odpt-routes --sync-bundled-tokyo

  # 認証付き: ODPT GTFS-JP 全社 + JSON-LD (東急バス・相鉄バス) を取得・マージ
  ODPT_API_KEY="..." python3 national-data-site/scripts/merge_odpt_and_rt.py --odpt --odpt-jsonld --sync-bundled-tokyo

  # Challenge 2026 トークンがある場合: 国際興業・東武バス・神奈川中央交通を追加
  ODPT_CHALLENGE_KEY="..." python3 national-data-site/scripts/merge_odpt_and_rt.py --odpt-challenge --sync-bundled-tokyo
"""

import argparse
import getpass
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from build_national_shards import API_URL, catalog_items, download_zip, fetch_json, write_pref_shards
from build_tokyo_routes import extract_routes_from_gtfs
from extra_feeds_registry import NON_PUBLIC_POLE_PREFIXES, SENDAI_ZIP, sendai_urls_on
from gtfs_to_app_data import convert_gtfs
from odpt_jsonld_to_app_data import convert_odpt_jsonld, fetch_odpt_jsonld


def _is_public_data_dir(data_dir: Path) -> bool:
    """公開 Pages 用 national-data-site/data（またはその配下）かを判定する。"""
    resolved = data_dir.resolve()
    public_root = (SCRIPT_DIR.parent / "data").resolve()
    try:
        resolved.relative_to(public_root)
        return True
    except ValueError:
        return False


def _guard_no_non_public_write(data_dir: Path, reason: str) -> None:
    if _is_public_data_dir(data_dir):
        raise SystemExit(
            f"[BLOCKED] 公開データ先 ({data_dir.resolve()}) への非公開・認証付きデータの書き込みを中止しました: {reason}\n"
            "  ODPT基本ライセンス対象は extra_feeds_sync.py --mode bundled-odpt を使用し、アプリ同梱先へ出力してください。"
        )

# gtfs-data.jp 公開静的GTFS のうち、公開GTFS-RT と突合可能な事業者
PUBLIC_GTFS_RT_FEEDS = {
    # 群馬県 (pref_10)
    ("nagai-unyu", "Nagaibus"): (10, "nagai_bus", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_NagaiTransportation_AllLines_trip_update"),
    # 千葉県 (pref_12)
    ("keisei-transitbus", "keiseitransitbus"): (12, "keisei_chiba_west", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_KeiseiTransitBus_AllLines_trip_update"),
    # 高知県 (pref_39)
    ("tosaden-kotsu", "GTFS-Tosadentraffic_Regularbus"): (39, "tosaden_regular", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_TosadenTraffic_Regularbus_trip_update"),
    ("tosaden-kotsu", "GTFS-Tosadentraffic_Airportbus"): (39, "tosaden_airport", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_TosadenTraffic_Airportbus_trip_update"),
    ("kochi-seinan-kotsu", "GTFS-Seinantraffic_Localbus"): (39, "kochi_seinan", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_KochiSeinanTraffic_Localbus_trip_update"),
    ("kochi-koryo-kotsu", "GTFS-Koryotraffic_Localbus"): (39, "kochi_koryo", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_KochiKoryoKotsu_Localbus_trip_update"),
    ("khkhokubu-kotsu", "GTFS-Hokubutraffic_Localbus"): (39, "kenko_hokubu", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_KenkoHokubuKotsu_Localbus_trip_update"),
    ("kochi-suzakicity", "GTFS-Susakicity_Bus"): (39, "susaki_city", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_SusakiCity_Citybus_trip_update"),
    ("tosacity", "GTFS-Tosacity_Bus"): (39, "tosa_dragon", "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_TosaCity_AllLines_trip_update"),
    # 熊本県 (pref_43)
    ("sankobus", "sankobus"): (43, "kumamoto_sanko", "https://km.bus-vision.jp/realtime/sankobus_vpos_update.bin"),
    ("kumamoto-toshibus", "kumamoto-toshibus"): (43, "kumamoto_toshi", "https://km.bus-vision.jp/realtime/toshibus_vpos_update.bin"),
    ("kumamotodentetsu", "kumamotodentetsu-bus"): (43, "kumamoto_dentetsu", "https://km.bus-vision.jp/realtime/dentetsu_vpos_update.bin"),
    ("kuma-bus", "kuma-bus"): (43, "kumamoto_kumabus", "https://km.bus-vision.jp/realtime/kumabus_vpos_update.bin"),
}


def _odpt_urls(base_no_query: str, fallback_date: Optional[str] = None) -> List[str]:
    urls = [f"{base_no_query}?date=current"]
    if fallback_date:
        urls.append(f"{base_no_query}?date={fallback_date}")
    return urls


# ===== APIキー不要: ODPT 公開静的 GTFS-JP（gtfs-data.jp 未掲載・未取得の32件） =====
ODPT_PUBLIC_STATIC_TARGETS: List[Dict] = [
    # 青森県 (pref_02)
    {
        "id": "aomori_city",
        "pref": 2,
        "label": "青森市営バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/AomoriCity/AllLines.zip", "20261201"),
        "operator_type": "public",
        "operator_short": "市営",
        "badge_color": "#15803d",
        "rt_feed": "aomori_city",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_AomoriCity_AllLines_vehicle",
        "default_url": "https://www.city.aomori.aomori.jp/koutsu/",
    },
    {
        "id": "aomori_comm",
        "pref": 2,
        "label": "青森市コミュニティバス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/UrbanDevelopment_AomoriCity/AllLines.zip", "20261001"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "aomori_comm",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_UrbanDevelopment_AomoriCity_AllLines_vehicle",
        "default_url": "https://www.city.aomori.aomori.jp/",
    },
    # 宮城県 (pref_04)
    {
        "id": "sendai",
        "pref": 4,
        "label": "仙台市交通局（仙台市営バス）",
        "urls": _odpt_urls(SENDAI_ZIP),
        "operator_type": "public",
        "operator_short": "市営",
        "badge_color": "#15803d",
        "rt_feed": "sendai",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_SendaiMunicipal_bus_realtime_information_vehicle",
        "default_url": "https://www.kotsu.city.sendai.jp/",
    },
    # 群馬県 (pref_10)
    {
        "id": "gunma_bus",
        "pref": 10,
        "label": "群馬バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/GunmaBus/AllLines.zip", "20261016"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "gunma_bus",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_GunmaBus_AllLines_vehicle",
        "default_url": "https://www.gunbus.co.jp/",
    },
    {
        "id": "gunma_chuo",
        "pref": 10,
        "label": "群馬中央バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/GunmachuoBus/AllLines.zip", "20260430"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "gunma_chuo",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_GunmachuoBus_AllLines_vehicle",
        "default_url": "https://www.gunmachuobus.co.jp/",
    },
    {
        "id": "kanetsu",
        "pref": 10,
        "label": "関越交通バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/Kan_etsuTransportation/AllLines.zip", "20261216"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "kanetsu",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_Kan_etsuTransportation_AllLines_vehicle",
        "default_url": "https://kan-etsu.net/",
    },
    {
        "id": "joshin_kanko",
        "pref": 10,
        "label": "上信観光バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/JoshinKankoBus/AllLines.zip", "20261127"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "joshin_kanko",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_JoshinKankoBus_AllLines_vehicle",
        "default_url": "https://www.joshinkankobus.jp/",
    },
    {
        "id": "joshin_hire",
        "pref": 10,
        "label": "上信ハイヤー",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/JoshinHire/AllLines.zip", "20260819"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "joshin_hire",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_JoshinHire_AllLines_vehicle",
        "default_url": "https://www.joshin-hire.co.jp/",
    },
    {
        "id": "nippon_chuo",
        "pref": 10,
        "label": "日本中央バス（全線）",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/NipponChuoBus/Maebashi_Area.zip", "20260401"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "nippon_chuo",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_NipponChuoBus_Maebashi_Area_vehicle",
        "default_url": "https://ncbbus.co.jp/",
    },
    {
        "id": "takasaki_yosii",
        "pref": 10,
        "label": "高崎市よしいバス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/TakasakiCity/yosiibus.zip", "20260421"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "takasaki_yosii",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_TakasakiCity_yosiibus_vehicle",
        "default_url": "https://www.city.takasaki.gunma.jp/",
    },
    {
        "id": "midori_city",
        "pref": 10,
        "label": "群馬県みどり市路線バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/MidoriCity/AllLines.zip", "20260630"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.midori.gunma.jp/",
    },
    # 東京都 (pref_13)
    {
        "id": "hitachi_chiyoda",
        "pref": 13,
        "label": "千代田区地域福祉交通「風ぐるま」（日立自動車交通）",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/HitachiAutomobileTransportation/Chiyoda_ALLLINES.zip", "20260601"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.hitachi-gr.com/",
    },
    {
        "id": "hitachi_kita",
        "pref": 13,
        "label": "北区コミュニティバス「Kバス」（日立自動車交通）",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/HitachiAutomobileTransportation/KitaAllLines.zip", "20251220"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.hitachi-gr.com/",
    },
    {
        "id": "taito_megurin",
        "pref": 13,
        "label": "台東区コミュニティバス「めぐりん」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/TokyoTaitoCity/megurinCCBY40.zip", "20261102"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.taito.lg.jp/",
    },
    {
        "id": "suginami_gsm",
        "pref": 13,
        "label": "杉並区グリーンスローモビリティ",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/TokyoSuginamiCity/GreenSlowMobility.zip", "20261001"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "suginami_gsm",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_TokyoSuginamiCity_GreenSlowMobility_vehicle",
        "default_url": "https://www.city.suginami.tokyo.jp/",
    },
    {
        "id": "kiyose_kiyo",
        "pref": 13,
        "label": "清瀬市コミュニティバス「きよバス」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/KiyoseCity/KiyoBus.zip", "20260401"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "kiyose_kiyo",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_KiyoseCity_KiyoBus_vehicle",
        "default_url": "https://www.city.kiyose.lg.jp/",
    },
    {
        "id": "machida",
        "pref": 13,
        "label": "町田市コミュニティバス・市民バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/MachidaCity/AllLines.zip", "20261001"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.machida.tokyo.jp/",
    },
    {
        "id": "higashimurayama",
        "pref": 13,
        "label": "東村山市コミュニティバス「グリーンバス」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/HigashiMurayamaCity/Alllines.zip", "20260401"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.higashimurayama.tokyo.jp/",
    },
    {
        "id": "higashiyamato",
        "pref": 13,
        "label": "東大和市コミュニティバス「ちょこバス」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/HigashiyamatoCity/AllLines_CCBY4.zip", "20261118"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.higashiyamato.lg.jp/",
    },
    {
        "id": "inagi",
        "pref": 13,
        "label": "稲城市コミュニティバス「iバス」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/InagiCity/ibas_A_course.zip", "20240219"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.inagi.tokyo.jp/",
    },
    {
        "id": "kokubunji",
        "pref": 13,
        "label": "国分寺市地域バス「ぶんバス」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/KokubunjiCity/kokubunji_city_bunbus.zip", "20260105"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.kokubunji.tokyo.jp/",
    },
    {
        "id": "nishitokyo_hana",
        "pref": 13,
        "label": "西東京市「はなバス」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/NishitokyoCity/AllLines.zip", "20260401"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.nishitokyo.lg.jp/",
    },
    {
        "id": "miyake",
        "pref": 13,
        "label": "三宅村営バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/MiyakeVill/AllLine.zip", "20250929"),
        "operator_type": "public",
        "operator_short": "村営",
        "badge_color": "#15803d",
        "rt_feed": None,
        "default_url": "https://www.vill.miyake.tokyo.jp/",
    },
    {
        "id": "mizuho_demand",
        "pref": 13,
        "label": "瑞穂町デマンド交通「チョイソコみずほまち」",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/MizuhoTown/Mizuho_Area.zip", "20260202"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.mizuho.tokyo.jp/",
    },
    # 福井県 (pref_18 — 新規都道府県!)
    {
        "id": "keifuku",
        "pref": 18,
        "label": "京福バス（路線バス）",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/KeifukuBus/keifuku_rosen.zip", "20261201"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "keifuku",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_KeifukuBus_keifuku_rosen_vehicle",
        "default_url": "https://bus.keifuku.co.jp/",
    },
    # 愛知県 (pref_23)
    {
        "id": "nagoya_srt",
        "pref": 23,
        "label": "名古屋市 SRT",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/NagoyaHousingCityPlanningBureau/NagoyaSRT_AllLines.zip", "20260911"),
        "operator_type": "public",
        "operator_short": "公営",
        "badge_color": "#15803d",
        "rt_feed": "nagoya_srt",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_NagoyaHousingCityPlanningBureau_NagoyaSRT_AllLines_vehicle",
        "default_url": "https://www.city.nagoya.jp/",
    },
    # 岡山県 (pref_33)
    {
        "id": "uno_bus",
        "pref": 33,
        "label": "宇野自動車（路線バス）",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/UnoBus/AllLines.zip", "20261116"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "uno_bus",
        "rt_check_url": "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_UnoBus_AllLines_vehicle",
        "default_url": "https://www2.unobus.co.jp/",
    },
    {
        "id": "shimoden_pub",
        "pref": 33,
        "label": "下津井電鉄（路線バス）",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/Shimoden/Shimoden_GTFS_Static.zip", "20261001"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": None,
        "default_url": "https://bus.shimoden.net/",
    },
    {
        "id": "akaiwa",
        "pref": 33,
        "label": "赤磐市広域路線バス・市民バス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/AkaiwaCity/AllLines.zip", "20261201"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.akaiwa.lg.jp/",
    },
    # 香川県 (pref_37)
    {
        "id": "shodoshima_olive",
        "pref": 37,
        "label": "小豆島オリーブバス",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/ShodoshimaOliveBus/alllines.zip", "20261013"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": None,
        "default_url": "https://www.shodoshima-olive-bus.com/",
    },
    {
        "id": "shodoshima_town",
        "pref": 37,
        "label": "小豆島町営バス",
        "urls": [
            "https://api-public.odpt.org/api/v4/files/ShodoshimaTown/data/" + urllib.parse.quote("gtfs-町営バス三都西線2022v01.zip"),
        ],
        "operator_type": "public",
        "operator_short": "町営",
        "badge_color": "#15803d",
        "rt_feed": None,
        "default_url": "https://www.town.shodoshima.lg.jp/",
    },
]

# ===== 認証付き (ODPT_API_KEY 必須): ODPT 静的 GTFS-JP 全事業者 (28件) =====
ODPT_STATIC_TARGETS: List[Dict] = [
    # 北海道 (pref_01)
    {
        "id": "hokkaido_takushoku",
        "pref": 1,
        "label": "北海道拓殖バス（一般路線）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/HokkaidoTakushokuBus/Takusyoku_regular_line.zip", "20261001"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hokkaido_takushoku",
        "default_url": "https://www.takubus.com/",
    },
    {
        "id": "hokkaido_takushoku_hw",
        "pref": 1,
        "label": "北海道拓殖バス（高速・空港連絡）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/HokkaidoTakushokuBus/Takusyoku_highway_line.zip", "20261118"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": None,
        "default_url": "https://www.takubus.com/",
    },
    # 秋田県 (pref_05)
    {
        "id": "akita_city",
        "pref": 5,
        "label": "秋田市（マイタウン・バス、ぐるる）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/AkitaCity/AkitaCityBus.zip", "20261001"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "akita_city",
        "default_url": "https://www.city.akita.lg.jp/",
    },
    # 群馬県 (pref_10)
    {
        "id": "juo_bus",
        "pref": 10,
        "label": "十王自動車（伊勢崎・本庄線）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/JuoBus/Isesaki_Honjo_Line.zip", "20261025"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "juo_bus",
        "default_url": "https://www.juo.co.jp/",
    },
    # 埼玉県 (pref_11)
    {
        "id": "asahi_bus",
        "pref": 11,
        "label": "朝日自動車（朝日バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/AsahiBus/20261001.zip", "20261126"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": None,
        "default_url": "https://www.asahibus.jp/",
    },
    # 東京都 (pref_13)
    {
        "id": "seibu",
        "pref": 13,
        "label": "西武バス",
        "urls": [
            "https://api.odpt.org/api/v4/files/SeibuBus/data/SeibuBus-GTFS.zip",
            "https://api.odpt.org/api/v4/files/odpt/SeibuBus/AllLines.zip?date=current",
        ],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "seibu",
        "default_url": "https://www.seibubus.co.jp/",
    },
    {
        "id": "keio",
        "pref": 13,
        "label": "京王バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/KeioBus/AllLines.zip", "20261201"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "keio",
        "default_url": "https://www.keio-bus.com/",
    },
    {
        "id": "kanto",
        "pref": 13,
        "label": "関東バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/KantoBus/AllLines.zip", "20261201"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "kanto",
        "default_url": "https://www.kanto-bus.co.jp/",
    },
    {
        "id": "odakyu",
        "pref": 13,
        "label": "小田急バス",
        "urls": [
            "https://api.odpt.org/api/v4/files/odpt/OdakyuBus/AIILines.zip?date=current",
            "https://api.odpt.org/api/v4/files/odpt/OdakyuBus/AIILines.zip?date=20260924",
            "https://api.odpt.org/api/v4/files/odpt/OdakyuBus/AllLines.zip?date=current",
        ],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": None,
        "default_url": "https://www.odakyubus.co.jp/",
    },
    {
        "id": "nishi_tokyo",
        "pref": 13,
        "label": "西東京バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/NishiTokyoBus/NTBus.zip", "20261001"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "nishi_tokyo",
        "default_url": "https://www.nisitokyobus.co.jp/",
    },
    {
        "id": "hitachi",
        "pref": 13,
        "label": "文京区コミュニティバス「Bーぐる」（日立自動車交通）",
        "urls": _odpt_urls("https://api-public.odpt.org/api/v4/files/odpt/HitachiAutomobileTransportation/AllLines.zip", "20260401"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.hitachi-gr.com/",
    },
    {
        "id": "chuo_edobus",
        "pref": 13,
        "label": "中央区コミュニティバス「江戸バス」",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/TokyoChuoCity/Alldata.zip", "20260712"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.chuo.lg.jp/",
    },
    {
        "id": "tokyu_meguro",
        "pref": 13,
        "label": "目黒区コミュニティバス（東急バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/TokyuBus/tokyubus_community_MeguroCity.zip", "20261001"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.tokyubus.co.jp/",
    },
    {
        "id": "tokyu_ota",
        "pref": 13,
        "label": "大田区コミュニティバス（東急バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/TokyuBus/tokyubus_community_OtaCity.zip", "20261001"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.tokyubus.co.jp/",
    },
    {
        "id": "tokyu_shinagawa",
        "pref": 13,
        "label": "品川区コミュニティバス（東急バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/TokyuBus/tokyubus_community_ShinagawaCity.zip", "20261001"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.tokyubus.co.jp/",
    },
    {
        "id": "koganei_coco",
        "pref": 13,
        "label": "小金井市CoCoバス（野川・七軒家循環）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/TsukubaKankouKotsu/CoCoBusNogawa.zip", "20260401"),
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.koganei.lg.jp/",
    },
    {
        "id": "oshima_bus",
        "pref": 13,
        "label": "大島バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/OshimaBus/AllLines.zip", "20261001"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": None,
        "default_url": "https://www.oshima-bus.com/",
    },
    # 神奈川県 (pref_14)
    {
        "id": "yokohama",
        "pref": 14,
        "label": "横浜市交通局（横浜市営バス）",
        "urls": [
            "https://api.odpt.org/api/v4/files/odpt/YokohamaMunicipal/Bus.zip?date=current",
            "https://api.odpt.org/api/v4/files/odpt/YokohamaMunicipal/Bus.zip?date=20261101",
            "https://api.odpt.org/api/v4/files/odpt/YokohamaMunicipal/AllLines.zip?date=current",
        ],
        "operator_type": "public",
        "operator_short": "市営",
        "badge_color": "#15803d",
        "rt_feed": "yokohama",
        "default_url": "https://www.city.yokohama.lg.jp/kotsu/bus/",
    },
    {
        "id": "kawasaki",
        "pref": 14,
        "label": "川崎市交通局（川崎市バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/TransportationBureau_CityOfKawasaki/AllLines.zip", "20261101"),
        "operator_type": "public",
        "operator_short": "市営",
        "badge_color": "#15803d",
        "rt_feed": "kawasaki",
        "default_url": "https://www.city.kawasaki.jp/820/",
    },
    {
        "id": "rinko",
        "pref": 14,
        "label": "川崎鶴見臨港バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/KawasakiTsurumiRinkoBus/allrinko.zip", "20261101"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "rinko",
        "default_url": "https://www.rinkobus.co.jp/",
    },
    # 静岡県 (pref_22)
    {
        "id": "izuhakone",
        "pref": 22,
        "label": "伊豆箱根バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/IzuhakoneBus/IZHB.zip", "20261201"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "izuhakone",
        "default_url": "https://www.izuhakone.co.jp/bus/",
    },
    {
        "id": "akiha",
        "pref": 22,
        "label": "秋葉バスサービス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/AkihaBusService/AllLines.zip", "20261001"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "akiha",
        "default_url": "https://www.akihabus.co.jp/",
    },
    # 京都府 (pref_26 — 新規都道府県!)
    {
        "id": "kyoto_city",
        "pref": 26,
        "label": "京都市交通局（京都市営バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/KyotoMunicipalTransportation/Kyoto_City_Bus_GTFS.zip", "20261110"),
        "operator_type": "public",
        "operator_short": "市営",
        "badge_color": "#15803d",
        "rt_feed": None,
        "default_url": "https://www.city.kyoto.lg.jp/kotsu/",
    },
    {
        "id": "kyoto_bus",
        "pref": 26,
        "label": "京都バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/KyotoBus/AllLines.zip", "20261125"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "kyoto_bus",
        "default_url": "https://www.kyotobus.jp/",
    },
    # 島根県 (pref_32)
    {
        "id": "matsue_city",
        "pref": 32,
        "label": "松江市交通局（松江市営バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/MatsueCityTrafficOffice/AllLines.zip", "20261124"),
        "operator_type": "public",
        "operator_short": "市営",
        "badge_color": "#15803d",
        "rt_feed": "matsue_city",
        "default_url": "https://matsue-bus.jp/",
    },
    # 岡山県 (pref_33 — RT対応版で shimoden_pub を上書き可能)
    {
        "id": "shimoden",
        "pref": 33,
        "label": "下津井電鉄（路線バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/Shimoden/Shimoden_BUS_GTFS_Realtime.zip", "20261001"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "shimoden",
        "replaces_id": "shimoden_pub",
        "default_url": "https://bus.shimoden.net/",
    },
    # 山口県 (pref_35 — 新規都道府県!)
    {
        "id": "sentetsu",
        "pref": 35,
        "label": "船木鉄道（船鉄バス）",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/SentetsuBus/AllLines.zip", "20261001"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "sentetsu",
        "default_url": "https://www.sentetsu.jp/",
    },
    # 愛媛県 (pref_38 — 新規都道府県!)
    {
        "id": "iyotetsu",
        "pref": 38,
        "label": "伊予鉄バス",
        "urls": _odpt_urls("https://api.odpt.org/api/v4/files/odpt/IyotetsuBus/AllLines.zip", "20261025"),
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "iyotetsu",
        "default_url": "https://www.iyotetsu.co.jp/bus/",
    },
]

# ===== ODPT JSON-LD API 限定事業者（GTFS-JP が提供されていない大手バス） =====
ODPT_JSONLD_STANDARD_TARGETS: List[Dict] = [
    {
        "id": "tokyu_jsonld",
        "pref": 13,
        "operator_id": "TokyuBus",
        "label": "東急バス",
        "api_base": "https://api.odpt.org/api/v4",
        "has_fare": False,
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "default_url": "https://www.tokyubus.co.jp/",
    },
    {
        "id": "sotetsu_jsonld",
        "pref": 14,
        "operator_id": "SotetsuBus",
        "label": "相鉄バス",
        "api_base": "https://api.odpt.org/api/v4",
        "has_fare": False,
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "default_url": "https://www.sotetsu.co.jp/bus/",
    },
]

ODPT_JSONLD_CHALLENGE_TARGETS: List[Dict] = [
    {
        "id": "kokusai_kogyo",
        "pref": 13,
        "operator_id": "KokusaiKogyoBus",
        "label": "国際興業バス",
        "api_base": "https://api-challenge.odpt.org/api/v4",
        "has_fare": False,
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "default_url": "https://5931bus.com/",
    },
    {
        "id": "tobu_bus",
        "pref": 11,
        "operator_id": "TobuBus",
        "label": "東武バス",
        "api_base": "https://api-challenge.odpt.org/api/v4",
        "has_fare": False,
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "default_url": "https://www.tobu-bus.com/",
    },
    {
        "id": "kanachu",
        "pref": 14,
        "operator_id": "Kanachu",
        "label": "神奈川中央交通（神奈中バス）",
        "api_base": "https://api-challenge.odpt.org/api/v4",
        "has_fare": True,
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "default_url": "https://www.kanachu.co.jp/",
    },
]


# ===== 県・バス協会・自治体独自オープンデータポータル（東京コミュ・鳥取県 CC BY / 広島県バス協会 CC0） =====
PREF_PORTAL_TARGETS: List[Dict] = [
    # --- 東京都 (pref_13): 港区ちぃばす（フジエクスプレス / CC BY）・渋谷区ハチ公バス（CC BY） ---
    {
        "id": "tokyo_chiibus",
        "prefix": "nat_p13_901_",
        "pref": 13,
        "label": "港区コミュニティバス「ちぃばす」（フジエクスプレス）",
        "urls": ["https://gtfs-jp.buskita.com/fxc/gtfs.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.fujikyu.co.jp/fujiexpress/chiibus.html",
    },
    {
        "id": "tokyo_hachiko",
        "prefix": "nat_p13_902_",
        "pref": 13,
        "label": "渋谷区コミュニティバス「ハチ公バス」",
        "urls": ["https://www.arcgis.com/sharing/rest/content/items/185d0dbc980443b8b60e135349e2ae5e/data"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.shibuya.tokyo.jp/kurashi/kotsu/hachiko_bus/index.html",
    },
    # --- 鳥取県 (pref_31): https://odp-pref-tottori.tori-info.co.jp/bus.html (CC BY) ---
    {
        "id": "tottori_nihonkotsu",
        "prefix": "nat_p31_001_",
        "pref": 31,
        "label": "日本交通（鳥取県）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/1.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "tottori_nihonkotsu",
        "rt_check_url": "https://odp-pref-tottori.tori-info.co.jp/bus_data/1_rt.json",
        "default_url": "https://www.nihonkotsu.co.jp/bus/",
    },
    {
        "id": "tottori_hinomaru",
        "prefix": "nat_p31_002_",
        "pref": 31,
        "label": "日ノ丸自動車",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/2.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "tottori_hinomaru",
        "rt_check_url": "https://odp-pref-tottori.tori-info.co.jp/bus_data/2_rt.json",
        "default_url": "https://hinomarubus.co.jp/rosen_bus/timetable/",
    },
    {
        "id": "tottori_city",
        "prefix": "nat_p31_003_",
        "pref": 31,
        "label": "鳥取市（くる梨・ループ麒麟獅子等）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/3.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "tottori_city",
        "rt_check_url": "https://odp-pref-tottori.tori-info.co.jp/bus_data/3_rt.bin",
        "default_url": "https://www.city.tottori.lg.jp/",
    },
    {
        "id": "tottori_iwami",
        "prefix": "nat_p31_004_",
        "pref": 31,
        "label": "岩美町営バス",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/4.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.iwami.gr.jp/",
    },
    {
        "id": "tottori_wakasa",
        "prefix": "nat_p31_005_",
        "pref": 31,
        "label": "若桜町営バス（おにっこバス）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/5.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.wakasa.tottori.jp/",
    },
    {
        "id": "tottori_yazu",
        "prefix": "nat_p31_006_",
        "pref": 31,
        "label": "八頭町営バス（やずバス）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/6.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.yazu.tottori.jp/",
    },
    {
        "id": "tottori_misasa",
        "prefix": "nat_p31_007_",
        "pref": 31,
        "label": "三朝町営バス（みささサンサンバス）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/7.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.misasa.tottori.jp/",
    },
    {
        "id": "tottori_kotoura",
        "prefix": "nat_p31_008_",
        "pref": 31,
        "label": "琴浦町営バス（ことうらバス）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/8.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.kotoura.tottori.jp/",
    },
    {
        "id": "tottori_yonago",
        "prefix": "nat_p31_009_",
        "pref": 31,
        "label": "米子市循環バス（だんだんバス・どんぐりコロコロ）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/9.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.city.yonago.lg.jp/",
    },
    {
        "id": "tottori_nanbu",
        "prefix": "nat_p31_010_",
        "pref": 31,
        "label": "南部町営ふれあいバス",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/11.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.nanbu.tottori.jp/",
    },
    {
        "id": "tottori_nichinan",
        "prefix": "nat_p31_011_",
        "pref": 31,
        "label": "日南町巡回バス（たったもバス）",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/12.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.nichinan.lg.jp/",
    },
    {
        "id": "tottori_hino",
        "prefix": "nat_p31_012_",
        "pref": 31,
        "label": "日野町営バス",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/13.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town.hino.tottori.jp/",
    },
    {
        "id": "tottori_kofu",
        "prefix": "nat_p31_013_",
        "pref": 31,
        "label": "江府町営バス",
        "urls": ["https://odp-pref-tottori.tori-info.co.jp/bus_data/14.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": None,
        "default_url": "https://www.town-kofu.jp/",
    },
    # --- 広島県 (pref_34): https://www.bus-kyo.or.jp/gtfs-open-data (CC0) ---
    {
        "id": "hiro_hiroden",
        "prefix": "nat_p34_001_",
        "pref": 34,
        "label": "広島電鉄（広電バス）",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/8/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_hiroden",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/8/vehicle_position.bin",
        "default_url": "https://www.hiroden.co.jp/bus/",
    },
    {
        "id": "hiro_hirobus",
        "prefix": "nat_p34_002_",
        "pref": 34,
        "label": "広島バス",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/9/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_hirobus",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/9/vehicle_position.bin",
        "default_url": "https://www.hirobus.co.jp/",
    },
    {
        "id": "hiro_hirokou",
        "prefix": "nat_p34_003_",
        "pref": 34,
        "label": "広島交通（広交観光・井原線含む）",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/10/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_hirokou",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/10/vehicle_position.bin",
        "default_url": "https://www.hiroko-group.co.jp/kotsu/",
    },
    {
        "id": "hiro_geiyo",
        "prefix": "nat_p34_004_",
        "pref": 34,
        "label": "芸陽バス",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/11/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_geiyo",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/11/vehicle_position.bin",
        "default_url": "https://www.geiyo.co.jp/",
    },
    {
        "id": "hiro_bihoku",
        "prefix": "nat_p34_005_",
        "pref": 34,
        "label": "備北交通",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/12/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_bihoku",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/12/vehicle_position.bin",
        "default_url": "https://www.bihoku.co.jp/",
    },
    {
        "id": "hiro_jrbus",
        "prefix": "nat_p34_006_",
        "pref": 34,
        "label": "JRバス中国",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/15/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_jrbus",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/15/vehicle_position.bin",
        "default_url": "https://www.chugoku-jrbus.co.jp/",
    },
    {
        "id": "hiro_bonbus",
        "prefix": "nat_p34_007_",
        "pref": 34,
        "label": "エイチ・ディー西広島（ボン・バス）",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/13/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_bonbus",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/13/vehicle_position.bin",
        "default_url": "https://www.bonbus.co.jp/",
    },
    {
        "id": "hiro_forble",
        "prefix": "nat_p34_008_",
        "pref": 34,
        "label": "フォーブル",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/14/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_forble",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/14/vehicle_position.bin",
        "default_url": "https://forble.jp/",
    },
    {
        "id": "hiro_sasaki",
        "prefix": "nat_p34_009_",
        "pref": 34,
        "label": "ささき観光（おおのハートバス）",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/17/current_data.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "hiro_sasaki",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/17/vehicle_position.bin",
        "default_url": "https://www.city.hatsukaichi.hiroshima.jp/",
    },
    {
        "id": "hiro_kure",
        "prefix": "nat_p34_010_",
        "pref": 34,
        "label": "呉市生活バス",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/18/current_data.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "hiro_kure",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/18/vehicle_position.bin",
        "default_url": "https://www.city.kure.lg.jp/",
    },
    {
        "id": "hiro_hatsukaichi",
        "prefix": "nat_p34_011_",
        "pref": 34,
        "label": "廿日市市自主運行バス",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/19/current_data.zip"],
        "operator_type": "community",
        "operator_short": "コミュ",
        "badge_color": "#d97706",
        "rt_feed": "hiro_hatsukaichi",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/19/vehicle_position.bin",
        "default_url": "https://www.city.hatsukaichi.hiroshima.jp/",
    },
    {
        "id": "hiro_asahi",
        "prefix": "nat_p34_012_",
        "pref": 34,
        "label": "朝日交通（阿戸熊野線）",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/54/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_asahi",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/54/vehicle_position.bin",
        "default_url": "https://www.bus-kyo.or.jp/",
    },
    {
        "id": "hiro_onomichi",
        "prefix": "nat_p34_013_",
        "pref": 34,
        "label": "おのみちバス",
        "urls": ["https://ajt-mobusta-gtfs.mcapps.jp/static/53/current_data.zip"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_onomichi",
        "rt_check_url": "https://ajt-mobusta-gtfs.mcapps.jp/realtime/53/vehicle_position.bin",
        "default_url": "https://onomichibus.jp/",
    },
    {
        "id": "hiro_etajima",
        "prefix": "nat_p34_014_",
        "pref": 34,
        "label": "江田島バス",
        "urls": ["https://gtfs-st.busit.jp/api/etajimabus"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_etajima",
        "rt_check_url": "https://gtfs-rt.busit.jp/api/Etajimabus/TripUpdates",
        "default_url": "https://etajimabus.jp/",
    },
    {
        "id": "hiro_chugokubus",
        "prefix": "nat_p34_015_",
        "pref": 34,
        "label": "中国バス",
        "urls": ["https://bus-vision.jp/gtfs_v2/chugokubus/gtfsFeed"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_chugokubus",
        "rt_check_url": "https://bus-vision.jp/realtime/chugokubus_vpos_update_v2.bin",
        "default_url": "https://www.chugokubus.jp/",
    },
    {
        "id": "hiro_tomotetsu",
        "prefix": "nat_p34_016_",
        "pref": 34,
        "label": "鞆鉄道（トモテツバス）",
        "urls": ["https://bus-vision.jp/gtfs_v2/tomotetsubus/gtfsFeed"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_tomotetsu",
        "rt_check_url": "https://bus-vision.jp/realtime/tomotetsubus_vpos_update_v2.bin",
        "default_url": "https://www.tomotetsu.co.jp/",
    },
    {
        "id": "hiro_ikasa",
        "prefix": "nat_p34_017_",
        "pref": 34,
        "label": "井笠バスカンパニー",
        "urls": ["https://bus-vision.jp/gtfs_v2/ikasabus/gtfsFeed"],
        "operator_type": "private",
        "operator_short": "私営",
        "badge_color": "#1d4ed8",
        "rt_feed": "hiro_ikasa",
        "rt_check_url": "https://bus-vision.jp/realtime/ikasabus_vpos_update_v2.bin",
        "default_url": "https://www.ikasabus.jp/",
    },
]


def load_existing_pref_poles(data_dir: Path, pref_code: int) -> Dict[str, Dict]:
    tt_dir = data_dir / "timetables" / f"pref_{pref_code:02d}"
    poles: Dict[str, Dict] = {}
    if not tt_dir.exists():
        return poles
    for f in sorted(tt_dir.glob("b*.json")):
        chunk = json.loads(f.read_text(encoding="utf-8"))
        poles.update(chunk)
    return poles


def update_manifest_entry(data_dir: Path, pref_entry: Dict) -> None:
    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prefs = [p for p in manifest.get("prefs", []) if p.get("pref") != pref_entry["pref"]]
    prefs.append(pref_entry)
    prefs.sort(key=lambda x: x["pref"])
    manifest["prefs"] = prefs
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_pref_routes(data_dir: Path, pref_code: int, prefix: str, new_routes: List[Dict], replaces_prefix: Optional[str] = None) -> int:
    """routes/pref_XX.json へ指定プレフィックスの路線ポリラインを上書きマージする。"""
    routes_dir = data_dir / "routes"
    routes_dir.mkdir(parents=True, exist_ok=True)
    out_file = routes_dir / f"pref_{pref_code:02d}.json"
    existing_routes: List[Dict] = []
    if out_file.exists():
        try:
            doc = json.loads(out_file.read_text(encoding="utf-8"))
            existing_routes = doc.get("routes", []) if isinstance(doc, dict) else []
        except Exception:
            existing_routes = []
    filtered = [
        r for r in existing_routes
        if not str(r.get("id", "")).startswith(prefix)
        and (not replaces_prefix or not str(r.get("id", "")).startswith(replaces_prefix))
    ]
    filtered.extend(new_routes)
    out_file.write_text(
        json.dumps({"pref": pref_code, "routes": filtered}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return len(filtered)


def _read_varint(buf: bytes, i: int) -> Tuple[int, int]:
    val, shift = 0, 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, i
        shift += 7
    return val, i


def _parse_pb(buf: bytes):
    i, out = 0, []
    while i < len(buf):
        tag, i = _read_varint(buf, i)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
            out.append((fn, wt, v))
        elif wt == 1:
            out.append((fn, wt, buf[i : i + 8]))
            i += 8
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            out.append((fn, wt, buf[i : i + ln]))
            i += ln
        elif wt == 5:
            out.append((fn, wt, buf[i : i + 4]))
            i += 4
        else:
            break
    return out


def verify_live_rt_match(poles: List[Dict], rt_url: str) -> Tuple[int, int, int]:
    """ライブGTFS-RTのtrip_id/stop_idが生成した静的データと実際に何件一致するかを検証する。"""
    req = urllib.request.Request(rt_url, headers={"User-Agent": "BusTimetableNavi/1.0"})
    data = urllib.request.urlopen(req, timeout=15).read()
    trip_ids_by_stop: Dict[str, set] = {}
    all_trip_ids = set()
    for p in poles:
        raw_sid = re.sub(r"^(?:odpt_[a-z0-9_-]+?_|nat_p\d{2}_\d{3}_|nat_p\d{2}_)", "", p["pole_id"])
        s = trip_ids_by_stop.setdefault(raw_sid, set())
        for r in p.get("routes", []):
            tm = r.get("trip_map") or {}
            s.update(tm.keys())
            all_trip_ids.update(tm.keys())

    total_veh = 0
    trip_matched = 0
    stop_and_trip_matched = 0
    if rt_url.endswith(".json") or data.lstrip().startswith(b"{"):
        doc = json.loads(data.decode("utf-8", "ignore"))
        for ent in doc.get("entity") or []:
            vp = ent.get("vehicle") or {}
            tid = str((vp.get("trip") or {}).get("trip_id") or "")
            sid = str(vp.get("stop_id") or "")
            if not tid:
                continue
            total_veh += 1
            if tid in all_trip_ids:
                trip_matched += 1
            if sid and sid in trip_ids_by_stop and tid in trip_ids_by_stop[sid]:
                stop_and_trip_matched += 1
        return total_veh, trip_matched, stop_and_trip_matched
    for fn, wt, e in _parse_pb(data):
        if fn != 2 or wt != 2:
            continue
        ef = _parse_pb(e)
        vp = next((v for f2, w2, v in ef if f2 == 4 and w2 == 2), None)
        tu = next((v for f2, w2, v in ef if f2 == 3 and w2 == 2), None)
        tid = ""
        sid = ""
        if vp:
            vf = _parse_pb(vp)
            td = next((v for f2, w2, v in vf if f2 == 1 and w2 == 2), None)
            sb = next((v for f2, w2, v in vf if f2 == 7 and w2 == 2), None)
            if td:
                tb = next((v for f2, w2, v in _parse_pb(td) if f2 == 1 and w2 == 2), None)
                tid = tb.decode("utf-8", "ignore") if tb else ""
            sid = sb.decode("utf-8", "ignore") if sb else ""
        elif tu:
            tuf = _parse_pb(tu)
            td = next((v for f2, w2, v in tuf if f2 == 1 and w2 == 2), None)
            if td:
                tb = next((v for f2, w2, v in _parse_pb(td) if f2 == 1 and w2 == 2), None)
                tid = tb.decode("utf-8", "ignore") if tb else ""
        if not tid:
            continue
        total_veh += 1
        if tid in all_trip_ids:
            trip_matched += 1
        if sid and sid in trip_ids_by_stop and tid in trip_ids_by_stop[sid]:
            stop_and_trip_matched += 1
    return total_veh, trip_matched, stop_and_trip_matched


def run_public_rt_upgrade(data_dir: Path, raw_dir: Path, target_prefs: Optional[set] = None) -> None:
    print("=== Updating existing nationwide gtfs-data.jp operators with realtime trip_map + rt_feed ===")
    from fetch_pref_gtfs import infer_operator_meta
    items = catalog_items(fetch_json(API_URL))
    raw_dir.mkdir(parents=True, exist_ok=True)

    touched_prefs: Dict[int, Dict[str, Dict]] = {}
    slot_by_pref_op: Dict[Tuple[int, str], str] = {}

    for item in items:
        key = ((item.get("organization_id") or "").strip(), (item.get("feed_id") or "").strip())
        if key not in PUBLIC_GTFS_RT_FEEDS:
            continue
        pref, rt_feed, rt_url = PUBLIC_GTFS_RT_FEEDS[key]
        if target_prefs and pref not in target_prefs:
            continue

        if pref not in touched_prefs:
            existing = load_existing_pref_poles(data_dir, pref)
            touched_prefs[pref] = existing
            for pid, pole in existing.items():
                m = re.match(rf"^(nat_p{pref:02d}_\d{{3}}_)", pid)
                if m:
                    slot_by_pref_op.setdefault((pref, pole.get("operator_name", "")), m.group(1))

        existing = touched_prefs[pref]
        org_name = (item.get("organization_name") or "").strip()
        feed_name = (item.get("feed_name") or "").strip()
        op_full = f"{org_name}（{feed_name}）" if feed_name and feed_name not in org_name else org_name
        prefix = slot_by_pref_op.get((pref, op_full), f"nat_p{pref:02d}_{rt_feed}_")

        zip_path = raw_dir / f"pubrt_p{pref:02d}_{rt_feed}.zip"
        if not download_zip(item["file_url"], zip_path):
            print(f"[SKIP] p{pref:02d} {op_full}: download failed")
            continue

        op_type, op_short, badge_color = infer_operator_meta(org_name, feed_name)
        conv = convert_gtfs(
            gtfs_zip_path=zip_path,
            operator_type=op_type,
            operator_short=op_short,
            supports_realtime=True,
            default_timetable_url=item.get("organization_web_url") or item.get("feed_page_url") or "",
            badge_color=badge_color,
        )
        new_poles = conv.get("poles", [])
        for pid in [k for k in existing.keys() if k.startswith(prefix)]:
            del existing[pid]

        for p in new_poles:
            p["pole_id"] = prefix + p["pole_id"]
            p["operator_name"] = op_full
            p["rt_feed"] = rt_feed
            for r in p.get("routes", []):
                r["route_id"] = prefix + r["route_id"]
                if r.get("prev_pole_id"):
                    r["prev_pole_id"] = prefix + r["prev_pole_id"]
                if r.get("next_pole_id"):
                    r["next_pole_id"] = prefix + r["next_pole_id"]
            existing[p["pole_id"]] = p

        tot, tm, stm = verify_live_rt_match(new_poles, rt_url)
        print(f"[OK] p{pref:02d} {op_full} (feed={rt_feed}): poles={len(new_poles)} | LIVE RT match: live={tot}, trip_match={tm}, stop+trip={stm}")

    for pref, existing in sorted(touched_prefs.items()):
        tt_dir = data_dir / "timetables" / f"pref_{pref:02d}"
        if tt_dir.exists():
            for old in tt_dir.glob("b*.json"):
                old.unlink()
        entry = write_pref_shards(pref, list(existing.values()), data_dir)
        if entry:
            update_manifest_entry(data_dir, entry)
            files = sorted(tt_dir.glob("b*.json"))
            max_kb = max((f.stat().st_size for f in files), default=0) / 1024
            print(f"DONE pref_{pref:02d}: total_poles={entry['pole_count']} buckets={len(files)} max_bucket={max_kb:.0f}KB")


def download_odpt_zip(urls: List[str], api_key: str, dest: Path) -> Tuple[Optional[str], str]:
    last_err = ""
    for base_url in urls:
        full_url = base_url
        if api_key and "api.odpt.org/" in base_url:
            sep = "&" if "?" in base_url else "?"
            full_url = f"{base_url}{sep}{urllib.parse.urlencode({'acl:consumerKey': api_key})}"
        try:
            req = urllib.request.Request(full_url, headers={"User-Agent": "BusTimetableNavi/1.0"})
            with urllib.request.urlopen(req, timeout=45) as res:
                data = res.read()
            dest.write_bytes(data)
            with zipfile.ZipFile(dest, "r") as zf:
                if zf.namelist():
                    return base_url, ""
            last_err = "empty_zip"
        except urllib.error.HTTPError as e:
            dest.unlink(missing_ok=True)
            last_err = f"HTTP {e.code} ({base_url})"
        except Exception as e:
            dest.unlink(missing_ok=True)
            last_err = f"{type(e).__name__} ({base_url})"
    return None, last_err


def _ingest_gtfs_targets(
    targets: List[Dict],
    data_dir: Path,
    raw_dir: Path,
    api_key: str,
    proxy_base: str,
    file_prefix: str = "odpt",
) -> Set[int]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    poles_by_pref: Dict[int, Dict[str, Dict]] = {}
    touched_prefs: Set[int] = set()

    for tgt in targets:
        pref = tgt["pref"]
        zip_path = raw_dir / f"{file_prefix}_{tgt['id']}.zip"
        urls = tgt["urls"]
        if tgt["id"] == "sendai":
            urls = sendai_urls_on(datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d"))
        matched_url, err_detail = download_odpt_zip(urls, api_key, zip_path)
        if not matched_url:
            print(f"[SKIP] pref_{pref:02d} {tgt['label']}: 取得失敗 ({err_detail})")
            continue

        conv = convert_gtfs(
            gtfs_zip_path=zip_path,
            operator_type=tgt["operator_type"],
            operator_short=tgt["operator_short"],
            supports_realtime=bool(tgt.get("rt_feed")),
            default_timetable_url=tgt["default_url"],
            badge_color=tgt["badge_color"],
        )
        new_poles = conv.get("poles", [])
        if not new_poles:
            print(f"[EMPTY] pref_{pref:02d} {tgt['label']}: 停留所0件（デマンド専用・期限切れ等）のためスキップ")
            continue

        if pref not in poles_by_pref:
            poles_by_pref[pref] = load_existing_pref_poles(data_dir, pref)
        touched_prefs.add(pref)

        prefix = tgt.get("prefix") or f"odpt_{tgt['id']}_"
        replaces_prefix = f"odpt_{tgt['replaces_id']}_" if tgt.get("replaces_id") else None
        for pid in [
            k for k in poles_by_pref[pref].keys()
            if k.startswith(prefix) or (replaces_prefix and k.startswith(replaces_prefix))
        ]:
            del poles_by_pref[pref][pid]

        fare_routes = 0
        total_routes = 0
        for p in new_poles:
            p["pole_id"] = prefix + p["pole_id"]
            p["operator_name"] = tgt["label"]
            if tgt.get("rt_feed"):
                p["rt_feed"] = tgt["rt_feed"]
            for r in p.get("routes", []):
                total_routes += 1
                if r.get("fare"):
                    fare_routes += 1
                r["route_id"] = prefix + r["route_id"]
                if r.get("prev_pole_id"):
                    r["prev_pole_id"] = prefix + r["prev_pole_id"]
                if r.get("next_pole_id"):
                    r["next_pole_id"] = prefix + r["next_pole_id"]
            poles_by_pref[pref][p["pole_id"]] = p

        # 路線ポリラインも生成・マージ
        route_lines = extract_routes_from_gtfs(
            zip_path=zip_path,
            op_type=tgt["operator_type"],
            op_short=tgt["operator_short"],
            op_name_default=tgt["label"],
            id_prefix=prefix,
            eps=0.00010,
        )
        total_pref_routes = merge_pref_routes(data_dir, pref, prefix, route_lines, replaces_prefix=replaces_prefix)

        rt_note = "static"
        rt_check_url = tgt.get("rt_check_url") or (
            f"{proxy_base.rstrip('/')}/gtfs-rt/{tgt['rt_feed']}" if tgt.get("rt_feed") and proxy_base else ""
        )
        if tgt.get("rt_feed") and rt_check_url:
            try:
                tot, tm, stm = verify_live_rt_match(new_poles, rt_check_url)
                rt_note = f"LIVE RT: live={tot}, trip={tm}, stop+trip={stm}"
            except Exception as e:
                rt_note = f"RT check skipped: {e}"
        print(
            f"[OK] pref_{pref:02d} {tgt['label']}: poles={len(new_poles)}, "
            f"routes_poly={len(route_lines)} (pref計{total_pref_routes}), "
            f"fare={fare_routes}/{total_routes} ({rt_note})"
        )

    for pref in sorted(touched_prefs):
        tt_dir = data_dir / "timetables" / f"pref_{pref:02d}"
        if tt_dir.exists():
            for old in tt_dir.glob("b*.json"):
                old.unlink()
        entry = write_pref_shards(pref, list(poles_by_pref[pref].values()), data_dir)
        if entry:
            update_manifest_entry(data_dir, entry)
            print(f"DONE pref_{pref:02d}: total_poles={entry['pole_count']}")

    return touched_prefs


def run_odpt_jsonld_merge(
    targets: List[Dict],
    data_dir: Path,
    raw_dir: Path,
    api_key: str,
    allow_cache_only: bool = False,
) -> Set[int]:
    if not api_key and not allow_cache_only:
        raise RuntimeError("ODPT JSON-LD 用のアクセストークンが未設定です")
    raw_dir.mkdir(parents=True, exist_ok=True)
    poles_by_pref: Dict[int, Dict[str, Dict]] = {}
    touched_prefs: Set[int] = set()

    for tgt in targets:
        pref = tgt["pref"]
        op_id = tgt["operator_id"]
        api_base = tgt["api_base"]
        cache_dir = raw_dir / f"jsonld_{tgt['id']}"
        try:
            poles_raw = fetch_odpt_jsonld(api_base, "odpt:BusstopPole", op_id, api_key, cache_dir / "poles.json")
            patterns_raw = fetch_odpt_jsonld(api_base, "odpt:BusroutePattern", op_id, api_key, cache_dir / "patterns.json")
            timetables_raw = fetch_odpt_jsonld(api_base, "odpt:BusTimetable", op_id, api_key, cache_dir / "timetables.json")
            fares_raw = (
                fetch_odpt_jsonld(api_base, "odpt:BusroutePatternFare", op_id, api_key, cache_dir / "fares.json")
                if tgt.get("has_fare")
                else []
            )
        except Exception as e:
            print(f"[SKIP] JSON-LD pref_{pref:02d} {tgt['label']}: {e}")
            continue

        prefix = tgt.get("prefix") or f"odpt_{tgt['id']}_"
        new_poles, route_lines = convert_odpt_jsonld(
            poles_raw=poles_raw,
            patterns_raw=patterns_raw,
            timetables_raw=timetables_raw,
            fares_raw=fares_raw,
            operator_name=tgt["label"],
            operator_type=tgt["operator_type"],
            operator_short=tgt["operator_short"],
            default_timetable_url=tgt["default_url"],
            badge_color=tgt["badge_color"],
            id_prefix=prefix,
        )
        if not new_poles:
            print(f"[EMPTY] JSON-LD pref_{pref:02d} {tgt['label']}: 停留所0件")
            continue

        if pref not in poles_by_pref:
            poles_by_pref[pref] = load_existing_pref_poles(data_dir, pref)
        touched_prefs.add(pref)

        for pid in [k for k in poles_by_pref[pref].keys() if k.startswith(prefix)]:
            del poles_by_pref[pref][pid]
        for p in new_poles:
            poles_by_pref[pref][p["pole_id"]] = p

        total_pref_routes = merge_pref_routes(data_dir, pref, prefix, route_lines)
        print(
            f"[OK] JSON-LD pref_{pref:02d} {tgt['label']}: "
            f"poles={len(new_poles)}, routes_poly={len(route_lines)} (pref計{total_pref_routes})"
        )

    for pref in sorted(touched_prefs):
        tt_dir = data_dir / "timetables" / f"pref_{pref:02d}"
        if tt_dir.exists():
            for old in tt_dir.glob("b*.json"):
                old.unlink()
        entry = write_pref_shards(pref, list(poles_by_pref[pref].values()), data_dir)
        if entry:
            update_manifest_entry(data_dir, entry)
            print(f"DONE pref_{pref:02d}: total_poles={entry['pole_count']}")

    return touched_prefs


def rebuild_existing_odpt_routes(data_dir: Path, raw_dir: Path) -> Set[int]:
    """ローカルに保存済みの odpt_*.zip から路線図ポリラインを生成して data/routes/pref_XX.json へマージする。"""
    touched: Set[int] = set()
    by_id = {t["id"]: t for t in ODPT_STATIC_TARGETS + ODPT_PUBLIC_STATIC_TARGETS}
    for zp in sorted(raw_dir.glob("odpt_*.zip")):
        tid = zp.stem[len("odpt_") :]
        tgt = by_id.get(tid)
        if not tgt:
            continue
        pref = tgt["pref"]
        prefix = f"odpt_{tid}_"
        route_lines = extract_routes_from_gtfs(
            zip_path=zp,
            op_type=tgt["operator_type"],
            op_short=tgt["operator_short"],
            op_name_default=tgt["label"],
            id_prefix=prefix,
            eps=0.00010,
        )
        if route_lines:
            total = merge_pref_routes(data_dir, pref, prefix, route_lines)
            touched.add(pref)
            print(f"[ROUTES] pref_{pref:02d} {tgt['label']}: +{len(route_lines)} routes (pref計{total})")
    return touched


def sync_bundled_tokyo_data(data_dir: Path, sync_bundled_tokyo: Path) -> None:
    if not sync_bundled_tokyo.exists():
        return
    shutil.copy2(data_dir / "index" / "pref_13.json", sync_bundled_tokyo / "index" / "pref_13.json")
    dst_tt = sync_bundled_tokyo / "timetables" / "pref_13"
    dst_tt.mkdir(parents=True, exist_ok=True)
    for old in dst_tt.glob("b*.json"):
        old.unlink()
    for src in (data_dir / "timetables" / "pref_13").glob("b*.json"):
        shutil.copy2(src, dst_tt / src.name)
    src_routes = data_dir / "routes" / "pref_13.json"
    if src_routes.exists():
        dst_routes_dir = sync_bundled_tokyo / "routes"
        dst_routes_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_routes, dst_routes_dir / "pref_13.json")
    shutil.copy2(data_dir / "manifest.json", sync_bundled_tokyo / "manifest.json")
    print(f"Synced bundled Tokyo pref_13 (index + timetables + routes + manifest) -> {sync_bundled_tokyo}")


def main() -> None:
    root = SCRIPT_DIR.parent.parent
    parser = argparse.ArgumentParser(description="全国GTFS-RT対応・ODPT公開/認証静的GTFS・JSON-LD・路線図マージ")
    parser.add_argument("--data-dir", type=Path, default=root / "national-data-site" / "data")
    parser.add_argument("--raw-dir", type=Path, default=root / "data-pipeline" / "raw" / "odpt_and_rt")
    parser.add_argument("--kumamoto", action="store_true", help="熊本4社（公開）をリアルタイム対応で再ビルド")
    parser.add_argument("--public-rt", action="store_true", help="群馬・千葉・高知の既存公開事業者をリアルタイム対応で再ビルド")
    parser.add_argument("--public-odpt", action="store_true", help="APIキー不要のODPT公開静的GTFS-JP全件を取得・マージ＋路線図生成")
    parser.add_argument("--rebuild-existing-odpt-routes", action="store_true", help="既存ダウンロード済みODPT ZIPの路線図ポリラインを生成・マージ")
    parser.add_argument("--odpt", action="store_true", help="認証付きODPT静的GTFS-JP全社を取得・マージ＋路線図生成")
    parser.add_argument("--odpt-jsonld", action="store_true", help="認証付きODPT JSON-LD限定事業者（東急バス・相鉄バス）を取得・マージ")
    parser.add_argument("--rebuild-cached-jsonld", action="store_true", help="ローカルキャッシュ済みODPT JSON-LD（東急バス・相鉄バス）を事業者フィルタ適用で再構築")
    parser.add_argument("--odpt-challenge", action="store_true", help="ODPT Challenge API事業者（国際興業・東武・神奈中）を取得・マージ")
    parser.add_argument("--pref-portals", action="store_true", help="県・バス協会・自治体独自ポータル（東京ちぃばす/ハチ公・鳥取県13件・広島県17件）を取得・マージ＋路線図生成")
    parser.add_argument("--tokyo-portals", action="store_true", help="東京自治体・独自ポータル（ちぃばす・ハチ公バス）のみ取得・マージ＋路線図生成")
    parser.add_argument("--proxy-base", default="https://bus-timetable-navi-proxy.innerburg-works.workers.dev")
    parser.add_argument("--sync-bundled-tokyo", action="store_true", help="iOSアプリ同梱の Web/data/（東京都）も同期")
    args = parser.parse_args()

    touched_any_tokyo = False

    if args.public_rt:
        run_public_rt_upgrade(args.data_dir, args.raw_dir, target_prefs={10, 12, 39})
    if args.kumamoto:
        run_public_rt_upgrade(args.data_dir, args.raw_dir, target_prefs={43})
    if args.pref_portals:
        print("=== Ingesting Prefecture / Bus Association Portal GTFS-JP datasets (Tokyo Comm / Tottori / Hiroshima) ===")
        touched = _ingest_gtfs_targets(
            PREF_PORTAL_TARGETS,
            args.data_dir,
            args.raw_dir,
            api_key="",
            proxy_base=args.proxy_base,
            file_prefix="portal",
        )
        if 13 in touched:
            touched_any_tokyo = True
    if args.tokyo_portals:
        print("=== Ingesting Tokyo Portal GTFS-JP datasets (Chiibus & Hachiko Bus) ===")
        touched = _ingest_gtfs_targets(
            [t for t in PREF_PORTAL_TARGETS if t["pref"] == 13],
            args.data_dir,
            args.raw_dir,
            api_key="",
            proxy_base=args.proxy_base,
            file_prefix="portal",
        )
        if 13 in touched:
            touched_any_tokyo = True
    if args.rebuild_cached_jsonld:
        _guard_no_non_public_write(args.data_dir, "--rebuild-cached-jsonld (ODPT JSON-LD)")
        print("=== Rebuilding cached ODPT JSON-LD datasets with strict operator filtering (TokyuBus, SotetsuBus) ===")
        touched = run_odpt_jsonld_merge(
            ODPT_JSONLD_STANDARD_TARGETS,
            args.data_dir,
            args.raw_dir,
            api_key="",
            allow_cache_only=True,
        )
        if 13 in touched:
            touched_any_tokyo = True
    if args.public_odpt:
        print("=== Ingesting PUBLIC ODPT GTFS-JP datasets (no API key required) ===")
        public_safe_targets = [
            t for t in ODPT_PUBLIC_STATIC_TARGETS
            if not f"odpt_{t['id']}_".startswith(NON_PUBLIC_POLE_PREFIXES)
        ]
        touched = _ingest_gtfs_targets(
            public_safe_targets,
            args.data_dir,
            args.raw_dir,
            api_key="",
            proxy_base=args.proxy_base,
            file_prefix="odpt_pub",
        )
        if 13 in touched:
            touched_any_tokyo = True
    if args.rebuild_existing_odpt_routes:
        _guard_no_non_public_write(args.data_dir, "--rebuild-existing-odpt-routes (contains auth ODPT ZIPs)")
        print("=== Rebuilding route polylines from existing downloaded ODPT ZIPs ===")
        touched = rebuild_existing_odpt_routes(args.data_dir, args.raw_dir)
        if 13 in touched:
            touched_any_tokyo = True
    if args.odpt or args.odpt_jsonld:
        _guard_no_non_public_write(args.data_dir, "--odpt / --odpt-jsonld (authenticated ODPT basic-license data)")
        api_key = os.environ.get("ODPT_API_KEY", "").strip()
        if not api_key or api_key == "あなたのODPTトークン":
            api_key = getpass.getpass("ODPT アクセストークンを貼り付けて Enter（画面には表示されません）: ").strip()
        if args.odpt:
            print("=== Ingesting AUTH ODPT GTFS-JP datasets ===")
            touched = _ingest_gtfs_targets(
                ODPT_STATIC_TARGETS,
                args.data_dir,
                args.raw_dir,
                api_key=api_key,
                proxy_base=args.proxy_base,
                file_prefix="odpt",
            )
            if 13 in touched:
                touched_any_tokyo = True
        if args.odpt_jsonld:
            print("=== Ingesting AUTH ODPT JSON-LD datasets (TokyuBus, SotetsuBus) ===")
            touched = run_odpt_jsonld_merge(ODPT_JSONLD_STANDARD_TARGETS, args.data_dir, args.raw_dir, api_key)
            if 13 in touched:
                touched_any_tokyo = True
    if args.odpt_challenge:
        _guard_no_non_public_write(args.data_dir, "--odpt-challenge (authenticated ODPT Challenge data)")
        ckey = os.environ.get("ODPT_CHALLENGE_KEY", "").strip()
        if not ckey:
            ckey = getpass.getpass("ODPT Challenge2026 アクセストークンを貼り付けて Enter: ").strip()
        print("=== Ingesting ODPT Challenge JSON-LD datasets (KokusaiKogyo, Tobu, Kanachu) ===")
        touched = run_odpt_jsonld_merge(ODPT_JSONLD_CHALLENGE_TARGETS, args.data_dir, args.raw_dir, ckey)
        if 13 in touched:
            touched_any_tokyo = True

    if args.sync_bundled_tokyo and (touched_any_tokyo or args.odpt or args.public_odpt or args.rebuild_existing_odpt_routes):
        sync_bundled_tokyo_data(args.data_dir, root / "ios-app" / "BusTimetableNavi" / "Web" / "data")


if __name__ == "__main__":
    main()
