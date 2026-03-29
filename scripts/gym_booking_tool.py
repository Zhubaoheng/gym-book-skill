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
                # bookable 条件：未过期 + 已开放 + 未满
                # is_open=1 即可预约；lock_reason 可能残留旧公告但不影响实际预约
                slot["bookable"] = bool(
                    not _is_slot_past(slot["date"], slot["interval_time"])
                    and slot["is_open"] == 1
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

    # chooseVerify: 提交选择验证（抓包显示在下单前必须调用）
    selected_for_verify = [
        {
            "date": slot["date"],
            "week": slot["week"],
            "week_msg": slot["week_msg"],
            "area_name": config.display_name,
            "interval_time": slot["interval_time"],
            "interval_id": slot["interval_id"],
            "area_id": slot["area_id"],
        }
    ]
    booking_session.choose_verify(config.stadium_id, config.venue_id, selected_for_verify)

    venue_cfg = booking_session.get_venue_config(config.venue_id, config.stadium_id, config.category_id)

    # vipInfo: 查询是否持有健身卡
    is_vip = "0"
    vip_res = booking_session.vip_info(config.venue_id)
    if vip_res.get("status") == 1:
        vip_data = vip_res.get("data") or {}
        if int(vip_data.get("vip_status", 0)) == 2:
            is_vip = "1"

    with contextlib.redirect_stdout(io.StringIO()):
        captcha = booking_session.get_and_recognize_captcha(max_retries=3)
    order_data = {
        "stadium_id": config.stadium_id,
        "venue_id": config.venue_id,
        "stadium_name": config.project_name,
        "project_name": config.project_name,
        "category_id": config.category_id,
        "captcha": captcha,
        "is_vip": is_vip,
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

    audit_status = int((order_details.get("data") or {}).get("audit_status", 0)) if order_details else 0
    needs_payment = bool(pay_url and audit_status == 6)

    # 从 order_result 中移除微信支付模板字段（err_code / err_code_des），
    # 这两个字段与实际付款状态无关，容易误导判断。
    # 付款状态的唯一依据是 order_details.data.audit_status：
    #   6 = 待支付，1 = 已预约（付款成功）
    sanitized_result = {k: v for k, v in result.items() if k != "data"}
    sanitized_result["data"] = {
        k: v for k, v in result_data.items()
        if k not in ("err_code", "err_code_des")
    }

    return {
        "status": "ok",
        "query_venue": venue_key,
        "venue_config": venue_cfg,
        "captcha": captcha,
        "selected_slot": slot,
        "order_result": sanitized_result,
        "order_id": order_id,
        "pay_url": pay_url,
        "needs_payment": needs_payment,
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


def list_orders(session: GymSession) -> dict:
    """获取我的预约列表，只保留有效字段，方便 Agent 识别并选择订单。"""
    res = session.my_subscribe(page=1)
    if res.get("status") != 1:
        return {"status": "error", "message": res.get("info", "查询失败")}
    raw = res.get("data") or []
    orders = [
        {
            "order_id": item.get("order_id"),
            "order_num": item.get("order_num"),
            "stadium_name": item.get("stadium_name"),
            "project_name": item.get("project_name"),
            "location": item.get("location"),
            "audit_status": item.get("audit_status"),
            "audit_status_text": item.get("audit_status_text"),
            "detail": item.get("detail") or [],
        }
        for item in raw
    ]
    return {"status": "ok", "orders": orders}


def wait_pay(session: GymSession, order_id: str, timeout: int = 300, interval: int = 5) -> dict:
    """
    轮询订单付款状态，付款完成后生成本地 HTML 并返回路径。

    audit_status=6 → 待支付；audit_status=1 → 已预约（付款成功）。
    timeout 秒内未付款则返回超时错误。
    """
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        res = session.get_order_details(order_id=order_id)
        if res.get("status") != 1:
            return {"status": "error", "message": f"查询订单失败: {res.get('info','')}"}
        data = res["data"]
        audit_status = int(data.get("audit_status", 0))
        if audit_status == 3:
            return {"status": "error", "message": "订单已取消"}
        if audit_status != 6:
            # 付款完成，生成 HTML
            order_page_path = None
            try:
                output_path = ROOT / "generated_orders" / f"order_{order_id}.html"
                order_page_path = session.render_order_detail_html(res, str(output_path))
            except Exception as exc:
                return {"status": "paid", "order_details": res, "order_page_path": None, "error": str(exc)}
            return {
                "status": "paid",
                "order_id": order_id,
                "stadium_name": data.get("stadium_name", ""),
                "details": data.get("details", []),
                "order_details": res,
                "order_page_path": order_page_path,
            }
        _time.sleep(interval)
    return {"status": "timeout", "message": f"{timeout} 秒内未检测到付款，请稍后再查询二维码"}


def cancel_booking(session: GymSession, order_id: str) -> dict:
    """取消指定 order_id 的预约。"""
    # 先获取订单详情确认存在
    details_res = session.get_order_details(order_id=order_id)
    if details_res.get("status") != 1:
        return {"status": "error", "message": f"订单不存在或查询失败: {details_res.get('info','')}"}
    order = details_res["data"]
    result = session.cancel_order(order_id, order_num=order.get("order_num", ""))
    if result.get("status") == 1:
        return {
            "status": "success",
            "message": result.get("info", "取消预约成功"),
            "order_id": order_id,
            "stadium_name": order.get("stadium_name", ""),
            "details": order.get("details", []),
        }
    return {"status": "error", "message": result.get("info", "取消失败"), "raw": result}


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

    sub.add_parser("list-orders")

    cancel_parser = sub.add_parser("cancel")
    cancel_parser.add_argument("--order-id", required=True, help="订单 ID（数字）")

    wait_pay_parser = sub.add_parser("wait-pay")
    wait_pay_parser.add_argument("--order-id", required=True, help="订单 ID（数字）")
    wait_pay_parser.add_argument("--timeout", type=int, default=300, help="最长等待秒数（默认 300）")

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

    if args.command == "list-orders":
        print(json.dumps(list_orders(session), ensure_ascii=False, indent=2))

    if args.command == "cancel":
        print(json.dumps(cancel_booking(session, args.order_id), ensure_ascii=False, indent=2))

    if args.command == "wait-pay":
        print(json.dumps(wait_pay(session, args.order_id, timeout=args.timeout), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
