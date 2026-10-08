from research.optimization_assessment import promotion


def scenario(month, values, drawdown=10):
    return {"month": month, "metrics": {"net_profit": sum(values), "trade_count": len(values), "max_drawdown": drawdown},
            "trades": [{"net_pnl": v, "gross_pnl": v+1, "fees": 1, "holding_minutes": 10} for v in values]}


def test_promotion_rejects_improvement_that_depends_on_the_largest_trade():
    control = [scenario(m, [60,30,10]) for m in ["July", "August", "September"]]
    diversified = [scenario(m, [70,30,10]) for m in ["July", "August", "September"]]
    assert promotion(control, diversified, 8)["passed"]
    concentrated = [scenario(m, [80,15,10]) for m in ["July", "August", "September"]]
    result = promotion(control, concentrated, 8)
    assert result["checks"]["total_net_improves"]
    assert result["checks"]["each_window_net_not_worse"]
    assert not result["checks"]["without_best_trade_net_not_worse"]
    assert not result["passed"]


def test_promotion_requires_every_window_and_prespecified_sample_floor():
    control = [scenario(m, [60,30,10]) for m in ["July", "August", "September"]]
    candidate = [scenario("July", [50,30,10]), scenario("August", [100,30,10]), scenario("September", [100,30,10],11)]
    result = promotion(control, candidate, 8)
    assert result["total_net_delta"] > 0
    assert not result["checks"]["each_window_net_not_worse"]
    assert not result["checks"]["each_window_drawdown_not_worse"]
    sparse = [scenario("July", [100,30,10]), scenario("August", [100,30,10]), scenario("September", [100])]
    assert not promotion(control, sparse, 8)["checks"]["minimum_trade_count"]
