---
name: bupt-gym-booking
description: Check bookable times and place bookings for BUPT gym venues from natural-language requests such as 游泳馆什么时候能预约, 健身房什么时候能预约, 预约今天下午的游泳馆, or 预约明天上午10点的老健身房.
---

# BUPT Gym Booking

Use this skill when the user wants to check reservable times or place a reservation for 北邮体育 venues.

It supports any current venue name returned by the live stadium list, including:
- `健身房`
- `鸿雁健身房`
- `一层羽毛球`
- `三层羽毛球馆`
- `乒乓球场`
- `台球室`
- `网球场`
- `游泳馆`
- `沙河校区健身房`
- `沙河校区乒乓球馆`
- `沙河校区羽毛场地`
- `游泳达标测试`

Run the helper tool:

```bash
cd /path/to/openclaw-gym-booking-skill
python3 ./scripts/gym_booking_tool.py ...
```

Before running it, create a local config file or export the required local variables.

Option 1: create `.env.local` in the skill root:

```bash
cat > .env.local <<'EOF'
SESSION_A='value_a'
SESSION_B='value_b'
SESSION_C='value_c'
SESSION_D='value_d'
SESSION_E='value_e'
EOF
```

Option 2: export them in the current shell:

```bash
export SESSION_A='value_a'
export SESSION_B='value_b'
export SESSION_C='value_c'
export SESSION_D='value_d'
export SESSION_E='value_e'
```

The tool rebuilds the session from those values and returns JSON. Keep `.env.local` private; it is ignored by Git.

Generated QR/detail pages are written under:

```bash
./generated_orders/
```

## Venue mapping

- Exact venue names can be passed directly to `--venue`
- `游泳馆` can also be passed as `swim`
- `鸿雁健身房` can also be passed as `hongyan-gym`
- `老健身房` can also be passed as `old-gym`
- Availability questions for `健身房` mean both `健身房` and `鸿雁健身房`, so use `all-gyms`
- Booking requests for `健身房` or `老健身房` mean `健身房` unless the user explicitly says `鸿雁健身房`
- Use `all-venues` if the user asks about all current venues

## Time resolution

- Resolve `今天` and `明天` in `Asia/Shanghai`
- Convert relative dates to absolute `YYYY-MM-DD` before running commands
- Map periods as:
  - `上午` -> `morning`
  - `下午` -> `afternoon`
  - `晚上` -> `evening`
- If the user gives a clock time such as `10点` or `10:00`, prefer `--time HH:MM` over `--period`

## Commands

### Check availability

- `游泳馆什么时候能预约`

```bash
python3 ./scripts/gym_booking_tool.py list --venue swim
```

- `健身房什么时候能预约`

```bash
python3 ./scripts/gym_booking_tool.py list --venue all-gyms
```

- `乒乓球场什么时候能预约`

```bash
python3 ./scripts/gym_booking_tool.py list --venue '乒乓球场'
```

### Make a booking

- `预约今天下午的游泳馆`

```bash
python3 ./scripts/gym_booking_tool.py book --venue swim --date YYYY-MM-DD --period afternoon
```

- `预约明天上午10点的老健身房`

```bash
python3 ./scripts/gym_booking_tool.py book --venue old-gym --date YYYY-MM-DD --time 10:00
```

### Get QR Page

- `给我游泳馆二维码`

```bash
python3 ./scripts/gym_booking_tool.py qr --venue swim
```

### Cancel a booking

- `取消订单 81307`

```bash
python3 ./scripts/gym_booking_tool.py cancel --order-id 81307
```

### Wait for payment and get HTML (non-VIP venues)

```bash
python3 ./scripts/gym_booking_tool.py wait-pay --order-id <order_id> --timeout 300
```

## Response rules

### Availability

- For availability questions, read `bookable_slots`
- If `bookable_slots` is empty, say that there are currently no open reservable slots, then summarize the returned dates and time ranges from `all_slots`
- For `健身房什么时候能预约`, report `健身房` and `鸿雁健身房` separately

### Booking — VIP (健身卡) venues

When `book` returns `needs_payment=false`:

- Booking is complete. Report success and send the `order_page_path` HTML file directly.

### Booking — non-VIP (paid) venues like 游泳馆

When `book` returns `needs_payment=true`:

1. Tell the user the booking was placed and give them the `pay_url` to complete payment in WeChat.
2. **NEVER declare payment success based on `order_result` fields.** The only source of truth for payment status is `order_details.data.audit_status`: `6` = pending payment, `1` = paid and confirmed.
3. Immediately run `wait-pay` to poll for payment (this blocks until paid or timeout):
   ```bash
   python3 ./scripts/gym_booking_tool.py wait-pay --order-id <order_id> --timeout 300
   ```
4. When `wait-pay` returns `status=paid`, send the `order_page_path` HTML file directly to the user.
5. If `wait-pay` returns `status=timeout`, tell the user payment was not detected and suggest running the `qr` command after paying.

### Other rules

- For QR requests, run `qr --venue ...` and return `order_page_path`
- If booking fails, report the exact server reason from `order_result.info`
- If the tool returns `status=error`, explain that no matching slot was found and include the requested date and time
- Keep the final reply short and directly answer whether the reservation succeeded
