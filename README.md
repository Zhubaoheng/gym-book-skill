# 北邮体育预约 Skill

这是一个用于北邮体育场馆预约流程的 OpenClaw Skill。

**其中的环境变量参数需要通过抓包获得，常见抓包软件均可，这也是环境配置的一部分。为防止接口滥用并尽量维护绝大多数同学的正常使用权益，本 Skill 仅供学习交流使用。如需了解具体加密参数的处理方式，请联系 [zhubaoheng0728@gmail.com](mailto:zhubaoheng0728@gmail.com)。**

## 功能

- 查询当前所有在线场馆的可预约时间
- 一次查询所有场馆的可预约时间和状态
- 按日期 + 时段或具体时间自动发起预约
- 取消指定订单的预约
- 非 VIP 场馆（如游泳馆）预约后轮询付款状态，付款完成后自动生成入场 HTML
- 为当前有效订单生成与原始页面完全相同的本地预约详情页和二维码页面

## 支持的请求示例

- `游泳馆什么时候能预约`
- `健身房什么时候能预约`
- `乒乓球场什么时候能预约`
- `预约今天下午的游泳馆`
- `预约明天上午10点的老健身房`
- `给我游泳馆二维码`
- `取消订单 81307`
- `游泳馆预约后给我付款链接`

## 支持的场馆

Skill 支持所有当前从实时场馆列表中返回的场馆，例如：

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

## 主要命令

```bash
python3 ./scripts/gym_booking_tool.py list --venue swim
python3 ./scripts/gym_booking_tool.py list --venue all-gyms
python3 ./scripts/gym_booking_tool.py list --venue '乒乓球场'
python3 ./scripts/gym_booking_tool.py book --venue swim --date YYYY-MM-DD --period afternoon
python3 ./scripts/gym_booking_tool.py book --venue old-gym --date YYYY-MM-DD --time 10:00
python3 ./scripts/gym_booking_tool.py qr --venue swim
python3 ./scripts/gym_booking_tool.py cancel --order-id 81307
python3 ./scripts/gym_booking_tool.py wait-pay --order-id 69449 --timeout 300
```

## 场馆别名

- `swim` -> `游泳馆`
- `old-gym` -> `健身房`
- `hongyan-gym` -> `鸿雁健身房`
- `all-gyms` -> `健身房` + `鸿雁健身房`
- `all-venues` -> 当前所有在线场馆

## 输出说明

工具输出为 JSON：

- 查询可预约时间时，返回 `bookable_slots` 和 `all_slots`
- 发起预约时，返回 `order_result`
- 预约成功或查询二维码时，可能返回 `order_page_path`
- 生成的本地 HTML 页面默认写入 `./generated_orders/`

## 私有配置说明

本仓库不包含真实运行参数。

- 仓库中出现的环境变量名是混淆后的占位名
- 真实环境变量仅保存在本地私有配置中
- 真实值仅供作者本人使用，不包含在公开仓库中

五个本地环境变量分别表示：

- `SESSION_A`：`uid`
- `SESSION_B`：某个人信息字段
- `SESSION_C`：`token`
- `SESSION_D`：`v3/api.php/*` 请求体所使用的 `AES-128-CBC` 加密 `key`
- `SESSION_E`：`v3/api.php/*` 请求体所使用的 `AES-128-CBC` 加密 `iv`

## 环境配置

使用 [uv](https://docs.astral.sh/uv/) 管理依赖：

```bash
uv sync          # 安装所有依赖
uv run python scripts/gym_booking_tool.py list --venue swim
```

## 健身卡（VIP）支持

如果你办理了校园健身卡，预约流程会自动检测 VIP 状态：

- 下单前自动调用 `vipInfo` 查询健身卡信息
- 持卡用户（`vip_status=2`）下单时自动设置 `is_vip=1` 并跳过支付
- 下单前自动调用 `chooseVerify` 验证所选时段

## 说明

- 相对日期按 `Asia/Shanghai` 时区解析
- 运行时需要能够访问目标场馆系统
- Skill 的意图映射与行为规则见 [SKILL.md](SKILL.md)
