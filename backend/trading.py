import math
from decimal import Decimal

from vnpy.trader.constant import Direction, Offset, OrderType
from vnpy.trader.object import OrderRequest


def order_request(main, payload, reference, closing=False):
    contract = main.get_contract(payload["symbol"])
    if not contract:
        raise ValueError("合约尚未加载")
    volume = payload["volume"]
    if not isinstance(volume, int) or isinstance(volume, bool) or volume < 1:
        raise ValueError("手数必须为正整数")
    if volume < contract.min_volume or (
        contract.max_volume and volume > contract.max_volume
    ):
        raise ValueError("数量超出合约最小/最大委托量")
    direction = Direction[payload["direction"]]
    if direction not in {Direction.LONG, Direction.SHORT}:
        raise ValueError("方向无效")
    if closing:
        direction = Direction.SHORT if direction == Direction.LONG else Direction.LONG
    tick = main.get_tick(contract.vt_symbol)
    if not tick:
        raise ValueError("请先订阅并等待行情")
    price = payload.get("price")
    if price is None:
        price = tick.ask_price_1 if direction == Direction.LONG else tick.bid_price_1
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        raise ValueError("价格无效，或当前没有对手报价")
    if (
        tick.limit_up
        and price > tick.limit_up
        or tick.limit_down
        and price < tick.limit_down
    ):
        raise ValueError("价格超出涨跌停范围")
    step = Decimal(str(contract.pricetick))
    if step <= 0 or Decimal(str(price)) % step:
        raise ValueError(f"价格必须是最小变动价位 {contract.pricetick} 的整数倍")
    request = OrderRequest(
        symbol=contract.symbol,
        exchange=contract.exchange,
        direction=direction,
        type=OrderType.LIMIT,
        volume=volume,
        price=price,
        offset=Offset.CLOSE if closing else Offset.OPEN,
        reference=reference,
    )
    if closing:
        converter = main.get_converter("CTP")
        holding = (
            converter.get_position_holding(contract.vt_symbol) if converter else None
        )
        if not holding:
            raise ValueError("持仓状态尚未同步")
        side = "long" if direction == Direction.SHORT else "short"
        available = getattr(holding, side + "_pos") - getattr(
            holding, side + "_pos_frozen"
        )
        if volume > available:
            raise ValueError(f"可平仓量不足，当前可平 {available:g} 手")
    return request
