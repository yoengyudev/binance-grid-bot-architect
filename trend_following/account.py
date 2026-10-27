"""Decimal paper wallet and the frozen study's risk/rounding policy."""
from dataclasses import dataclass, asdict
from decimal import Decimal as D, ROUND_DOWN


def decimal(value):
    result = D(str(value))
    if not result.is_finite():
        raise ValueError('Nonfinite account value')
    return result


def down(value, step):
    return (decimal(value) / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class Rules:
    step: D
    minq: D
    maxq: D
    notional: D
    tick: D
    minp: D

    def __post_init__(self):
        if any(not v.is_finite() or v <= 0 for v in asdict(self).values()) or self.maxq < self.minq:
            raise ValueError('Invalid SOL order rules')

    def payload(self):
        return {k: str(v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class Policy:
    capital: D = D('100')
    risk: D = D('.01')
    allocation: D = D('.8')
    fee: D = D('.001')
    slippage: D = D('.001')

    def __post_init__(self):
        if not self.capital.is_finite() or self.capital <= 0:
            raise ValueError('Paper capital must be positive')
        if not (0 < self.risk <= D('.01') and 0 < self.allocation <= D('.8') and
                0 <= self.fee < D('.1') and 0 <= self.slippage < D('.1')):
            raise ValueError('Invalid strategy risk/cost policy')

    def payload(self):
        return {k: str(v) for k, v in asdict(self).items()}


class Account:
    def __init__(self, rules, policy=Policy(), saved=None):
        self.rules, self.policy = rules, policy
        self.cash, self.qty, self.basis = policy.capital, D(0), D(0)
        self.realized, self.fees, self.peak, self.dd = D(0), D(0), policy.capital, D(0)
        self.active, self.opened, self.stop, self.entry = False, None, None, None
        self.fills, self.trades, self.rejects = [], [], []
        if saved:
            for key in ('cash', 'qty', 'basis', 'realized', 'fees', 'peak', 'dd'):
                setattr(self, key, decimal(saved[key]))
            self.active, self.opened = saved['active'], saved['opened']
            self.stop = decimal(saved['stop']) if saved['stop'] is not None else None
            self.entry = decimal(saved['entry']) if saved['entry'] is not None else None
        self.invariant()

    def saved(self):
        out = {k: str(getattr(self, k)) for k in ('cash','qty','basis','realized','fees','peak','dd')}
        out.update(active=self.active, opened=self.opened, stop=str(self.stop) if self.stop is not None else None,
                   entry=str(self.entry) if self.entry is not None else None)
        return out

    def floor(self, qty):
        return down(min(max(D(0), decimal(qty)), self.rules.maxq), self.rules.step)

    def valid(self, qty, price):
        return self.rules.minq <= qty <= self.rules.maxq and qty * price >= self.rules.notional

    def mark(self, price):
        equity = self.cash + self.qty * decimal(price)
        self.peak = max(self.peak, equity)
        self.dd = max(self.dd, (self.peak - equity) / self.peak * 100)
        return equity

    def invariant(self):
        if self.cash < 0 or self.qty < 0 or self.basis < D('-1e-16'):
            raise ArithmeticError('Negative paper wallet')
        if abs(self.cash + self.basis - self.policy.capital - self.realized) >= D('1e-13'):
            raise ArithmeticError('Paper cash/cost/realized identity failed')
        if self.active and (self.opened is None or self.stop is None or self.entry is None or self.qty <= 0):
            raise ArithmeticError('Incomplete active position')

    def reject(self, timestamp, reason):
        self.rejects.append(dict(t=timestamp, reason=reason))
        return False

    def buy(self, timestamp, raw, signal, *, mark=None):
        if self.active:
            raise ValueError('Only one active position is allowed')
        raw = decimal(raw)
        p = raw * (1 + self.policy.slippage)
        stop = down(p - D(3) * decimal(signal['atr']), self.rules.tick)
        if stop < self.rules.minp or stop <= 0 or stop >= raw:
            return self.reject(timestamp, 'invalid_stop')
        equity = self.mark(raw if mark is None else mark)
        budget = self.policy.risk * equity
        allocation = max(D(0), self.policy.allocation * equity - self.basis)
        stopnet = stop * (1-self.policy.slippage) * (1-self.policy.fee)
        qty = self.floor(min(max(D(0),budget-self.basis) / (p-(1-self.policy.fee)*stopnet),
                             min(allocation,self.cash)/p))
        while qty >= self.rules.minq:
            sell = self.floor(qty*(1-self.policy.fee))
            loss = qty*p-sell*stopnet
            if loss+self.basis <= budget:
                break
            qty -= self.rules.step
        received = qty*(1-self.policy.fee)
        sell = self.floor(received)
        if not self.valid(qty,p) or not self.valid(sell,stop*(1-self.policy.slippage)):
            return self.reject(timestamp, 'minimum_or_risk')
        loss, oldbasis, gross, fee = qty*p-sell*stopnet, self.basis, qty*p, qty*self.policy.fee
        self.cash -= gross
        self.qty += received
        self.basis += gross
        self.fees += fee*p
        self.active, self.opened, self.stop, self.entry = True, timestamp, stop, p
        self.fills.append(dict(t=timestamp,side='buy',reason='ema_cross',raw_price=str(raw),price=str(p),
                               quantity=str(qty),notional=str(gross),fee=str(fee),fee_asset='SOL',
                               stop=str(stop),planned_new_loss=str(loss),carried_basis=str(oldbasis),risk_budget=str(budget),
                               signal_closed_at=signal['closed_at'],scheduled_at=signal.get('scheduled_at',signal['closed_at']+3600)))
        self.invariant()
        self.mark(raw if mark is None else mark)
        return True

    def sell(self, timestamp, raw, reason):
        raw = decimal(raw)
        price = raw*(1-self.policy.slippage)
        qty = self.floor(self.qty)
        if not self.valid(qty,price):
            return False
        gross, fee = qty*price, qty*price*self.policy.fee
        basis = self.basis*qty/self.qty
        pnl = gross-fee-basis
        held = (timestamp-self.opened)/3600 if self.active else None
        self.cash += gross-fee
        self.qty -= qty
        self.basis -= basis
        self.realized += pnl
        self.fees += fee
        self.fills.append(dict(t=timestamp,side='sell',reason=reason,raw_price=str(raw),price=str(price),
                               quantity=str(qty),notional=str(gross),fee=str(fee),fee_asset='USDT'))
        self.trades.append(dict(t=timestamp,reason=reason,profit=str(pnl),held_hours=held))
        self.active, self.opened, self.stop, self.entry = False, None, None, None
        self.invariant()
        self.mark(raw)
        return True

    def update_stop(self, signal, observed_peak):
        if not self.active:
            return
        candidate = down(max(self.entry,decimal(observed_peak))-D(3)*decimal(signal['atr']),self.rules.tick)
        self.stop = max(self.stop,candidate)

    def boundary(self, timestamp, raw, signal=None, observed_peak=None, *, allow_entry=True, ask=None, mark=None):
        """A common event boundary used by replay and the paper runner.

        Opening/observed stop takes priority; exits cannot re-enter at the same
        event. A delayed signal's high-water input is bounded by its close.
        """
        exited = False
        if self.active and decimal(raw) <= self.stop:
            exited = self.sell(timestamp,raw,'stop_gap')
        if signal is None:
            return
        if self.active:
            if signal['ema_exit']:
                exited = self.sell(timestamp,raw,'ema_exit') or exited
            else:
                self.update_stop(signal, self.entry if observed_peak is None else observed_peak)
                if decimal(raw) <= self.stop:
                    exited = self.sell(timestamp,raw,'raised_stop') or exited
        elif self.qty > 0:
            self.sell(timestamp,raw,'residual_sweep')
        if not self.active and not exited and signal['ema_entry'] and allow_entry:
            self.buy(timestamp,raw if ask is None else ask,signal,mark=mark)
