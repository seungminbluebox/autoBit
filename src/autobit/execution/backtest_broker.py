"""Backtrader broker used by the event-driven adapter."""

import backtrader as bt
from backtrader.order import BuyOrder, SellOrder


class OneShotFractionalFiller:
    """Fill a configured fraction once for each distinct Backtrader order."""

    def __init__(self) -> None:
        self._filled_order_refs: set[int] = set()

    def __call__(self, order: bt.Order, price: float, ago: int) -> float:
        del price, ago
        remainder = abs(float(order.executed.remsize))
        if order.ref in self._filled_order_refs:
            return remainder
        self._filled_order_refs.add(order.ref)
        fraction = float(order.info.get("fill_fraction", 1.0))
        return remainder * fraction


class EventBacktestBroker(bt.brokers.BackBroker):
    """A named broker boundary for the Backtrader execution adapter."""

    def buy(
        self,
        owner,
        data,
        size,
        price=None,
        plimit=None,
        exectype=None,
        valid=None,
        tradeid=0,
        oco=None,
        trailamount=None,
        trailpercent=None,
        parent=None,
        transmit=True,
        histnotify=False,
        _checksubmit=True,
        **kwargs,
    ):
        if self.getposition(data).size > 0.0 or self._has_live_order(data, isbuy=True):
            return self._rejected_order(
                BuyOrder,
                owner,
                data,
                size,
                price,
                plimit,
                exectype,
                valid,
                tradeid,
                trailamount,
                trailpercent,
                parent,
                transmit,
                histnotify,
                "DUPLICATE_ENTRY",
                kwargs,
            )
        return super().buy(
            owner,
            data,
            size,
            price=price,
            plimit=plimit,
            exectype=exectype,
            valid=valid,
            tradeid=tradeid,
            oco=oco,
            trailamount=trailamount,
            trailpercent=trailpercent,
            parent=parent,
            transmit=transmit,
            histnotify=histnotify,
            _checksubmit=_checksubmit,
            **kwargs,
        )

    def sell(
        self,
        owner,
        data,
        size,
        price=None,
        plimit=None,
        exectype=None,
        valid=None,
        tradeid=0,
        oco=None,
        trailamount=None,
        trailpercent=None,
        parent=None,
        transmit=True,
        histnotify=False,
        _checksubmit=True,
        **kwargs,
    ):
        position_size = max(0.0, float(self.getposition(data).size))
        invalid_size = float(size) > position_size + 1e-12
        if invalid_size or self._has_live_order(data, isbuy=False):
            reason = "OVERSELL" if invalid_size else "DUPLICATE_EXIT"
            return self._rejected_order(
                SellOrder,
                owner,
                data,
                size,
                price,
                plimit,
                exectype,
                valid,
                tradeid,
                trailamount,
                trailpercent,
                parent,
                transmit,
                histnotify,
                reason,
                kwargs,
            )
        return super().sell(
            owner,
            data,
            size,
            price=price,
            plimit=plimit,
            exectype=exectype,
            valid=valid,
            tradeid=tradeid,
            oco=oco,
            trailamount=trailamount,
            trailpercent=trailpercent,
            parent=parent,
            transmit=transmit,
            histnotify=histnotify,
            _checksubmit=_checksubmit,
            **kwargs,
        )

    def _has_live_order(self, data, *, isbuy: bool) -> bool:
        return any(
            order.data is data and order.alive() and order.isbuy() is isbuy
            for order in self.orders
        )

    def _rejected_order(
        self,
        order_type,
        owner,
        data,
        size,
        price,
        plimit,
        exectype,
        valid,
        tradeid,
        trailamount,
        trailpercent,
        parent,
        transmit,
        histnotify,
        rejection_reason: str,
        kwargs: dict,
    ):
        order = order_type(
            owner=owner,
            data=data,
            size=size,
            price=price,
            pricelimit=plimit,
            exectype=exectype,
            valid=valid,
            tradeid=tradeid,
            trailamount=trailamount,
            trailpercent=trailpercent,
            parent=parent,
            transmit=transmit,
            histnotify=histnotify,
        )
        order.addinfo(**kwargs)
        order.addinfo(rejection_reason=rejection_reason)
        order.reject(self)
        self.orders.append(order)
        self.notify(order)
        return order
