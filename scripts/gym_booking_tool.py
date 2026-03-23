#!/usr/bin/env python3
"""
OpenClaw helper for BUPT gym booking.

Usage examples:
  python3 scripts/gym_booking_tool.py list --venue swim
  python3 scripts/gym_booking_tool.py list --venue all-gyms
  python3 scripts/gym_booking_tool.py book --venue swim --date 2026-03-24 --period afternoon
  python3 scripts/gym_booking_tool.py book --venue old-gym --date 2026-03-24 --time 10:00
"""

import argparse
import contextlib
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parents[0]
ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from gym_auto_book import GymSession  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")


def _is_slot_past(date_str: str, time_str: str) -> bool:
    """判断某个时间段是否已过期（当前时间已超过该时段结束时间）。"""
    try:
        end_time_str = time_str.split("-")[1]
        end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")
        end_dt = end_dt.replace(tzinfo=TZ)
        return datetime.now(TZ) >= end_dt
    except Exception:
        return False


@dataclass(frozen=True)
class VenueConfig:
    key: str
    display_name: str
    stadium_id: str
    venue_id: str
    category_id: str
    area_ids: tuple[str, ...]
    project_name: str


VENUE_ALIASES = {
    "swim": "游泳馆",
    "old-gym": "健身房",
    "hongyan-gym": "鸿雁健身房",
    "old gym": "健身房",
    "hongyan gym": "鸿雁健身房",
    "all-gyms": "all-gyms",
    "all gyms": "all-gyms",
    "all-venues": "all-venues",
    "all venues": "all-venues",
    "老健身房": "健身房",
}

GYM_GROUP_NAMES = {"健身房", "鸿雁健身房"}


def unique_values(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def slugify_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "venue"


def parse_user_range(user_range: str) -> tuple[str, ...]:
    values = [part.strip() for part in user_range.strip("[]").split(",") if part.strip()]
    return tuple(values)


def fetch_venue_catalog(session: GymSession) -> Dict[str, VenueConfig]:
    stadium_res = session.get_stadium_list()
    stadiums = stadium_res.get("data", {}).get("stadium", [])
    catalog: Dict[str, VenueConfig] = {}

    for stadium in stadiums:
        stadium_id = str(stadium.get("id"))
        details = session.get_stadium_details(stadium_id)
        data = details.get("data") or {}
        venue_entries = data.get("venue") or []
        area_ids = parse_user_range(data.get("user_range", ""))

        for entry in venue_entries:
            venue_id = None
            date_list = entry.get("list") or []
            if date_list:
                venue_id = date_list[0].get("venue_id")
            if venue_id is None:
                continue

            display_name = stadium.get("name") or data.get("name") or entry.get("name") or stadium_id
            key = slugify_name(display_name)
            dedupe = 2
            while key in catalog:
                key = f"{slugify_name(display_name)}-{dedupe}"
                dedupe += 1

            catalog[key] = VenueConfig(
                key=key,
                display_name=display_name,
                stadium_id=stadium_id,
                venue_id=str(venue_id),
                category_id=str(entry.get("category_id")),
                area_ids=area_ids or ("1",),
                project_name=entry.get("name") or display_name,
            )

    return catalog


def resolve_interval_request(session: GymSession, config: VenueConfig) -> tuple[VenueConfig, dict]:
    category_candidates = unique_values([config.category_id, "1", "6", "8", "2", "3", "14", "15"])
    full_area_range = ",".join(config.area_ids)
    area_candidates = unique_values(([full_area_range] if full_area_range else []) + list(config.area_ids) + ["28", "38", "1"])
    last_res = {"status": 0, "info": "未找到可用参数组合"}
    working_session = load_session()

    for category_id in category_candidates:
        for area_id in area_candidates:
            interval_res = working_session.get_interval(
                config.venue_id,
                config.stadium_id,
                category_id,
                area_id,
            )
            if interval_res.get("status") == 1:
                resolved = VenueConfig(
                    key=config.key,
                    display_name=config.display_name,
                    stadium_id=config.stadium_id,
                    venue_id=config.venue_id,
                    category_id=category_id,
                    area_ids=(area_id,),
                    project_name=config.project_name,
                )
                return resolved, interval_res
            last_res = interval_res

    return config, last_res


def load_session() -> GymSession:
    session = GymSession()
    session.load_session_from_env()
    return session


def now_date() -> date:
    return datetime.now(TZ).date()


def resolve_venue_keys(catalog: Dict[str, VenueConfig], venue_query: str) -> List[str]:
    query = venue_query.strip()
    normalized = VENUE_ALIASES.get(query.lower(), query)

    if normalized == "all-venues":
        return list(catalog.keys())
    if normalized == "all-gyms":
        return [key for key, cfg in catalog.items() if cfg.display_name in GYM_GROUP_NAMES]

    direct = [key for key, cfg in catalog.items() if cfg.display_name == normalized]
    if direct:
        return direct

    contains = [key for key, cfg in catalog.items() if normalized in cfg.display_name or cfg.display_name in normalized]
    if contains:
        return contains

    alias_contains = [key for key, cfg in catalog.items() if normalized.lower() in key.lower()]
    if alias_contains:
        return alias_contains

    return []


def flatten_slots(config: VenueConfig, interval_res: dict) -> List[dict]:
    data = interval_res.get("data")
    if not isinstance(data, dict):
        return []

    slots = []
    for day in data.get("interval", []):
        week_msg = day.get("week")
        for row in day.get("list", []):
            for item in row:
                slot = {
                    "venue_key": config.key,
                    "venue_name": config.display_name,
                    "stadium_id": config.stadium_id,
                    "venue_id": config.venue_id,
                    "category_id": config.category_id,
                    "area_id": str(item.get("area_id")),
                    "project_name": config.project_name,
                    "date": item.get("date"),
                    "week": item.get("week"),
                    "week_msg": week_msg,
                    "interval_time": item.get("interval_time"),
                    "interval_id": str(item.get("interval_id")),
                    "is_open": item.get("is_open"),
                    "is_lock": item.get("is_lock"),
                    "lock_type": item.get("lock_type"),
                    "lock_reason": item.get("lock_reason") or "",
                    "selected": item.get("selected"),
                    "max": item.get("max"),
                    "price": item.get("price"),
                }
                # bookable 条件：未过期 + 未满 + 无系统锁定
                # lock_reason 非空 = 系统锁定（不开放/维护等），不可预约
                # selected >= max = 已满，不可预约
                slot["bookable"] = bool(
                    not _is_slot_past(slot["date"], slot["interval_time"])
                    and slot["is_open"] == 1
                    and slot["lock_reason"] == ""
                    and slot["selected"] < slot["max"]
                )
                slots.append(slot)
    return slots


def list_slots(session: GymSession, venue_key: str) -> dict:
    catalog = fetch_venue_catalog(session)
    target_keys = resolve_venue_keys(catalog, venue_key)
    if not target_keys:
        return {
            "status": "error",
            "query_venue": venue_key,
            "bookable_slots": [],
            "all_slots": [],
            "errors": [{"venue_name": venue_key, "info": "未找到匹配场馆"}],
            "available_venues": [cfg.display_name for cfg in catalog.values()],
        }
    all_slots = []
    errors = []

    for key in target_keys:
        config, interval_res = resolve_interval_request(session, catalog[key])
        if interval_res.get("status") != 1:
            errors.append(
                {
                    "venue_key": key,
                    "venue_name": config.display_name,
                    "info": interval_res.get("info", "查询失败"),
                }
            )
            continue
        slots = flatten_slots(config, interval_res)
        if not slots:
            errors.append(
                {
                    "venue_key": key,
                    "venue_name": config.display_name,
                    "info": "接口返回为空或结构异常",
                }
            )
        all_slots.extend(slots)

    bookable = [slot for slot in all_slots if slot["bookable"]]
    return {
        "status": "ok",
        "query_venue": venue_key,
        "resolved_venues": [catalog[key].display_name for key in target_keys],
        "bookable_slots": bookable,
        "all_slots": all_slots,
        "errors": errors,
    }


def parse_hour_minute(text: str) -> tuple[int, int]:
    hour, minute = text.split(":", 1)
    return int(hour), int(minute)


def slot_matches_period(slot: dict, period: str) -> bool:
    start_text = slot["interval_time"].split("-", 1)[0]
    hour, _minute = parse_hour_minute(start_text)
    if period == "morning":
        return hour < 12
    if period == "afternoon":
        return 12 <= hour < 18
    if period == "evening":
        return hour >= 18
    return True


def slot_matches_time(slot: dict, time_text: str) -> bool:
    start_text, end_text = slot["interval_time"].split("-", 1)
    start_h, start_m = parse_hour_minute(start_text)
    end_h, end_m = parse_hour_minute(end_text)
    want_h, want_m = parse_hour_minute(time_text)
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    want_minutes = want_h * 60 + want_m
    return start_minutes <= want_minutes < end_minutes


def choose_slot(slots: List[dict], target_date: str, period: Optional[str], time_text: Optional[str]) -> Optional[dict]:
    candidates = [slot for slot in slots if slot["date"] == target_date]
    if time_text:
        candidates = [slot for slot in candidates if slot_matches_time(slot, time_text)]
    elif period:
        candidates = [slot for slot in candidates if slot_matches_period(slot, period)]

    bookable = [slot for slot in candidates if slot["bookable"]]
    if not bookable:
        return None

    return sorted(bookable, key=lambda item: item["interval_time"])[0]


def book_slot(session: GymSession, venue_key: str, target_date: str, period: Optional[str], time_text: Optional[str]) -> dict:
    listing = list_slots(session, venue_key)
    if listing.get("status") == "error":
        return listing
    slot = choose_slot(listing["all_slots"], target_date, period, time_text)

    if not slot:
        return {
            "status": "error",
            "message": "未找到匹配的时间段",
            "query_venue": venue_key,
            "target_date": target_date,
            "period": period,
            "time": time_text,
            "errors": listing.get("errors", []),
        }

    config = VenueConfig(
        key=slot["venue_key"],
        display_name=slot["venue_name"],
        stadium_id=slot["stadium_id"],
        venue_id=slot["venue_id"],
        category_id=slot["category_id"],
        area_ids=(slot["area_id"],),
        project_name=slot.get("project_name") or slot["venue_name"],
    )
    booking_session = load_session()
    venue_cfg = booking_session.get_venue_config(config.venue_id, config.stadium_id, config.category_id)
    with contextlib.redirect_stdout(io.StringIO()):
        captcha = booking_session.get_and_recognize_captcha(max_retries=3)
    order_data = {
        "stadium_id": config.stadium_id,
        "venue_id": config.venue_id,
        "stadium_name": config.project_name,
        "project_name": config.project_name,
        "category_id": config.category_id,
        "captcha": captcha,
        "price": str(slot.get("price", "0")).replace(".00", ""),
        "p_count": "0",
        "details": [
            {
                "date": slot["date"],
                "week": slot["week"],
                "week_msg": slot["week_msg"],
                "area_name": f"{config.display_name} ",
                "interval_time": slot["interval_time"],
                "interval_id": slot["interval_id"],
                "area_id": slot["area_id"],
            }
        ],
    }
    result = booking_session.add_order(order_data)
    order_page_path = None
    order_details = None
    order_page_error = None

    result_data = result.get("data") or {}
    order_id = str(result_data.get("order_id") or "")
    pay_url = result_data.get("pay_url") or ""
    if result.get("status") == 1 and order_id:
        try:
            order_details = booking_session.get_order_details(order_id=order_id)
            if order_details.get("status") == 1:
                output_path = ROOT / "generated_orders" / f"order_{order_id}.html"
                order_page_path = booking_session.render_order_detail_html(
                    order_details,
                    str(output_path),
                    pay_url=pay_url,
                )
        except Exception as exc:
            order_page_error = str(exc)

    return {
        "status": "ok",
        "query_venue": venue_key,
        "venue_config": venue_cfg,
        "captcha": captcha,
        "selected_slot": slot,
        "order_result": result,
        "order_details": order_details,
        "order_page_path": order_page_path,
        "order_page_error": order_page_error,
    }


def record_matches_venue(record: dict, config: VenueConfig) -> bool:
    if record.get("type") != 1:
        return False

    stadium_name = str(record.get("stadium_name") or "")
    venue_name = str(record.get("venue_name") or "")
    return (
        stadium_name == config.display_name
        or venue_name == config.display_name
        or config.display_name in venue_name
    )


def record_is_active(record: dict) -> bool:
    details = record.get("order_detail") or []
    return any(int(detail.get("status") or 0) == 1 for detail in details)


def get_qrcode_page(session: GymSession, venue_key: str) -> dict:
    catalog = fetch_venue_catalog(session)
    target_keys = resolve_venue_keys(catalog, venue_key)
    if not target_keys:
        return {
            "status": "error",
            "query_venue": venue_key,
            "message": "未找到匹配场馆",
            "available_venues": [cfg.display_name for cfg in catalog.values()],
        }

    working_session = load_session()
    record_pages = []
    page = 1
    while True:
        result = working_session.get_use_records(page=page)
        if result.get("status") != 1:
            return {
                "status": "error",
                "query_venue": venue_key,
                "message": result.get("info", "获取使用记录失败"),
                "record_result": result,
            }
        rows = result.get("data") or []
        if not rows:
            break
        record_pages.extend(rows)
        if len(rows) < 10:
            break
        page += 1

    candidates = []
    for key in target_keys:
        config = catalog[key]
        matches = [record for record in record_pages if record_matches_venue(record, config) and record_is_active(record)]
        for record in matches:
            candidates.append((config, record))

    if not candidates:
        return {
            "status": "error",
            "query_venue": venue_key,
            "message": "没有找到当前有效二维码",
            "resolved_venues": [catalog[key].display_name for key in target_keys],
            "recent_records": record_pages[:10],
        }

    config, record = candidates[0]
    order_id = str(record.get("order_id") or "")
    order_details = working_session.get_order_details(order_id=order_id)
    if order_details.get("status") != 1:
        return {
            "status": "error",
            "query_venue": venue_key,
            "message": order_details.get("info", "获取预约详情失败"),
            "resolved_venues": [catalog[key].display_name for key in target_keys],
            "record": record,
            "order_details": order_details,
        }

    output_path = ROOT / "generated_orders" / f"order_{order_id}.html"
    order_page_path = working_session.render_order_detail_html(order_details, str(output_path))
    return {
        "status": "ok",
        "query_venue": venue_key,
        "resolved_venues": [catalog[key].display_name for key in target_keys],
        "record": record,
        "order_details": order_details,
        "order_page_path": order_page_path,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--venue", required=True, help="场馆名、别名，或 all-gyms/all-venues")

    book_parser = sub.add_parser("book")
    book_parser.add_argument("--venue", required=True, help="场馆名、别名，或 all-gyms/all-venues")
    book_parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    book_parser.add_argument("--period", choices=["morning", "afternoon", "evening"])
    book_parser.add_argument("--time", help="HH:MM")

    qr_parser = sub.add_parser("qr")
    qr_parser.add_argument("--venue", required=True, help="场馆名或别名")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = load_session()

    if args.command == "list":
        print(json.dumps(list_slots(session, args.venue), ensure_ascii=False, indent=2))
        return

    if args.command == "book":
        print(
            json.dumps(
                book_slot(session, args.venue, args.date, args.period, args.time),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "qr":
        print(json.dumps(get_qrcode_page(session, args.venue), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
