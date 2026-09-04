"""Backtrader broker used by the event-driven adapter."""

import backtrader as bt
from backtrader.order import BuyOrder, SellOrder

from autobit.config import CostConfig
from autobit.risk.position_sizer import calculate_size
from autobit.strategy.donchian_trend import initial_stop_price


class OneShotFractionalFiller:
    """Fill a configured fraction once for each distinct Backtrader order."""

    def __init__(self, broker: "EventBacktestBroker") -> None:
        self._broker = broker
        self._filled_order_refs: set[int] = set()

    def __call__(self, order: bt.Order, price: float, ago: int) -> float:
        del ago
        remainder = abs(float(order.executed.remsize))
        if order.ref in self._filled_order_refs:
            return remainder
        self._filled_order_refs.add(order.ref)
        fraction = float(order.info.get("fill_fraction", 1.0))
        requested_fill = remainder * fraction
        if not order.isbuy() or not order.info.get("execution_cap_enabled", False):
            return requested_fill

        atr = float(order.info.execution_atr)
        actual_stop = initial_stop_price(float(price), atr, float(order.info.initial_atr_mult))
        decision = calculate_size(
            equity=float(self._broker.getvalue()),
            cash=float(self._broker.getcash()),
            entry=float(price),
            stop=actual_stop,
            current_atr_pct=atr / float(price),
            baseline_atr_pct=float(order.info.baseline_atr_pct),
            risk_rate=float(order.info.risk_rate),
            exposure_cap=float(order.info.exposure_cap),
            costs=CostConfig(
                fee_rate=float(order.info.fee_rate),
                slippage_rate=0.0,
            ),
        )
        safe_fill = min(requested_fill, remainder, float(decision.quantity))
        if safe_fill <= 0.0:
            order.addinfo(rejection_reason="EXECUTION_CAP")
            order.reject(self._broker)
            self._broker.notify(order)
            return 0.0
        if safe_fill < remainder - 1e-12:
            cap_bound = float(decision.quantity) < requested_fill - 1e-12
            order.addinfo(partial_reason="EXECUTION_CAP" if cap_bound else "PARTIAL_FILL")
        return safe_fill


class EventBacktestBroker(bt.brokers.BackBroker):
    """A named broker boundary for the Backtrader execution adapter."""

    def __init__(self) -> None:
        super().__init__()
        self._pre_submit_hook = None
        self._pre_match_hook = None
        self._same_bar_order_hook = None

    def set_pre_submit_hook(self, hook) -> None:
        """Register the strategy callback that observes native Created orders."""
        self._pre_submit_hook = hook

    def set_pre_match_hook(self, hook) -> None:
        """Run a data-boundary callback before submitted or pending orders match."""
        self._pre_match_hook = hook

    def set_same_bar_order_hook(self, hook) -> None:
        """Register synchronous delivery for conservative same-bar execution."""
        self._same_bar_order_hook = hook

    def submit(self, order, check=True):
        self._notify_pre_submit(order)
        return super().submit(order, check=check)

    def next(self) -> None:
        if self._pre_match_hook is not None:
            self._pre_match_hook()
        super().next()

    def cancel(self, order, bracket=False):
        """Retire a submitted stop before its next-bar replacement is checked."""
        try:
            self.submitted.remove(order)
        except ValueError:
            return super().cancel(order, bracket=bracket)
        order.cancel()
        self.notify(order)
        self._ococheck(order)
        if not bracket:
            self._bracketize(order, cancel=True)
        return True

    def cancel_end_of_data(
        self,
        order: bt.Order,
        *,
        terminal_reason: str = "END_OF_DATA",
    ) -> bool:
        """Terminally cancel an order that cannot receive another broker cycle."""
        order.addinfo(terminal_reason=terminal_reason)
        try:
            self.submitted.remove(order)
        except ValueError:
            pass
        else:
            order.cancel()
            self.notify(order)
            return True

        if order in self.pending:
            return self.cancel(order)
        if order.alive():
            order.cancel()
            self.notify(order)
            return True
        return False

    def reconcile_same_bar_stop(self, order: bt.Order) -> bool:
        """Activate and execute a newly submitted stop against the current bar."""
        if order.exectype != bt.Order.Stop or not order.alive():
            return False
        try:
            self.submitted.remove(order)
        except ValueError:
            return False

        self._notify_same_bar(order)
        self.submit_accept(order)
        self._notify_same_bar(order)
        self.pending.remove(order)
        status_before = order.status
        self._try_exec_stop(
            order,
            float(order.data.open[0]),
            float(order.data.high[0]),
            float(order.data.low[0]),
            float(order.created.price),
            float(order.data.close[0]),
        )
        if order.status != status_before:
            self._notify_same_bar(order)
        if order.alive():
            self.pending.append(order)
        self._get_value()
        return order.status in (order.Partial, order.Completed)

    def settle_immediate_market_order(self, order: bt.Order, price: float) -> bool:
        """Natively execute one market exit before the normal broker match cycle."""
        if order.exectype != bt.Order.Market or not order.alive():
            return False
        try:
            self.submitted.remove(order)
        except ValueError:
            return False

        self._notify_same_bar(order)
        self.submit_accept(order)
        self._notify_same_bar(order)
        try:
            self.pending.remove(order)
        except ValueError:
            return False
        self._execute(order, ago=0, price=float(price))
        self._notify_same_bar(order)
        self._get_value()
        return order.status == order.Completed

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
        self._notify_pre_submit(order)
        order.reject(self)
        self.orders.append(order)
        self.notify(order)
        return order

    def _notify_pre_submit(self, order: bt.Order) -> None:
        if self._pre_submit_hook is not None:
            self._pre_submit_hook(order)

    def _notify_same_bar(self, order: bt.Order) -> None:
        if self._same_bar_order_hook is not None:
            self._same_bar_order_hook(order)
