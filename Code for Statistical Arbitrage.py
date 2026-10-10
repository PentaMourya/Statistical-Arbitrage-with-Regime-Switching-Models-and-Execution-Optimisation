"""
STATISTICAL ARBITRAGE WITH REGIME-SWITCHING, EXECUTION OPTIMISATION,
AND PHYSICAL / SOFTWARE ACTION MAPPING
-- Complete rewrite: every class, method, and control-flow path rebuilt --

DEFAULT: simulation mode (numpy + pandas only).

TO ENABLE PRODUCTION:
  1. Delete the single line containing only three double-quotes that appears
     just before 'class LiveFeed'.
  2. Delete the matching single line containing only three double-quotes that
     appears just after 'return core' at the end of 'run_live()'.
  3. Change  MODE = "simulation"  to  MODE = "production".

Optional production deps (all imports wrapped in try/except):
    pip install ccxt pyserial requests RPi.GPIO
"""

import sys
import time
from collections import deque

import numpy as np
import pandas as pd


# ============================================================================
# MODE
# ============================================================================
MODE = "simulation"


# ============================================================================
# LOOKUP TABLES
# ============================================================================
REGIME_NAMES = {
    0: "Preferred (Low Vol, Fast Reversion)",
    1: "Neutral (Moderate Vol)",
    2: "Worst (High Vol, Slow Reversion)",
}

# (green_on, yellow_on, red_on, speed_pct, alert_tag)
SIGNAL_TABLE = {
    0: (True,  False, False, 100, "OPTIMAL"),
    1: (False, True,  False,  50, "CAUTION"),
    2: (False, False, True,    5, "DANGER / STOP"),
}

POS_TAG = {1: "LONG ", -1: "SHORT", 0: "FLAT "}


# ============================================================================
# ROLLING STATISTICS HELPER (separated from trading logic)
# ============================================================================
class RollingStats:
    """Maintains a windowed buffer and computes Z-score + Sharpe."""

    def __init__(self, window):
        self.window = window
        self._levels = deque(maxlen=window)
        self._returns = deque(maxlen=window)

    def push(self, level):
        if self._levels:
            prev = self._levels[-1]
            denom = abs(prev) if abs(prev) > 1e-9 else 1e-9
            self._returns.append((level - prev) / denom)
        self._levels.append(level)

    def z(self, level):
        if len(self._levels) < self.window:
            return None
        arr = np.asarray(self._levels, dtype=float)
        sd = float(arr.std())
        return (level - float(arr.mean())) / sd if sd > 1e-9 else 0.0

    def sharpe(self):
        if len(self._returns) < 2:
            return 0.0
        arr = np.asarray(self._returns, dtype=float)
        sd = float(arr.std())
        return float(arr.mean() / sd * np.sqrt(252)) if sd > 1e-9 else 0.0


# ============================================================================
# CORE TRADING LOGIC
# ============================================================================
class StatArbCore:
    """Regime-switching statistical arbitrage engine (mode-agnostic)."""

    def __init__(self, window=30, entry_z=1.8, exit_z=0.4, notional=1000):
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.notional = notional

        self.stats = RollingStats(window)

        # Trade ledger: each entry is a tuple
        # (time, regime, price1, price2, spread, z, vol, position,
        #  realized, unrealized, total, sharpe, signal)
        self.ledger = []

        self.position = 0
        self.entry_price = 0.0
        self.realized = 0.0
        self.unrealized = 0.0
        self.total = 0.0
        self.trade_count = 0
        self.win_count = 0
        self.slippage = 0.0
        self.fees = 0.0

    # -- decision ---------------------------------------------------------
    def _decide(self, z):
        if z is None:
            return 0
        if z > self.entry_z and self.position <= 0:
            return -1
        if z < -self.entry_z and self.position >= 0:
            return 1
        if abs(z) < self.exit_z and self.position != 0:
            return 0
        return 0

    # -- execution --------------------------------------------------------
    def _simulate_fill(self, side, price, volume, regime_vol):
        slip = 0.0005 * (volume / 10000.0) * (1.0 + regime_vol) * price
        self.slippage += abs(slip)
        if side == 1:
            return price + slip
        if side == -1:
            return price - slip
        return price

    # -- PnL bookkeeping --------------------------------------------------
    def _revalue(self, spread):
        if self.position == 1:
            self.unrealized = (spread - self.entry_price) * self.notional
        elif self.position == -1:
            self.unrealized = (self.entry_price - spread) * self.notional
        else:
            self.unrealized = 0.0
        self.total = self.realized + self.unrealized

    # -- main step --------------------------------------------------------
    def process(self, tick):
        spread = tick["spread"]
        regime = tick["regime"]
        vol = tick["volatility"]
        volume = tick["volume"]

        self.stats.push(spread)
        z = self.stats.z(spread)
        sharpe = self.stats.sharpe()

        signal = self._decide(z)

        if signal != 0:
            # close first
            if self.position != 0:
                self.realized += self.unrealized
                self.unrealized = 0.0
                if self.total > 0:
                    self.win_count += 1
                self.trade_count += 1
                self.position = 0
            # open new
            if signal in (1, -1):
                self.entry_price = self._simulate_fill(signal, spread, volume, vol)
                self.position = signal
                self.fees += 0.1

        self._revalue(spread)

        row = (
            tick["timestamp"], tick["price1"], tick["price2"], spread,
            z if z is not None else 0.0, regime, vol, self.position,
            self.realized, self.unrealized, self.total, sharpe, signal,
        )
        self.ledger.append(row)
        return row

    # -- report -----------------------------------------------------------
    def to_frame(self):
        return pd.DataFrame(
            self.ledger,
            columns=[
                "time", "price1", "price2", "spread", "z_score", "regime",
                "volatility", "position", "realized_pnl", "unrealized_pnl",
                "total_pnl", "sharpe_roll", "signal",
            ],
        )

    def metrics(self):
        if not self.ledger:
            return {}
        df = self.to_frame()
        cummax = df["total_pnl"].cummax()
        max_dd = float((df["total_pnl"] - cummax).min())
        wr = self.win_count / self.trade_count if self.trade_count else 0.0
        return {
            "final_pnl": self.total,
            "avg_sharpe": float(df["sharpe_roll"].mean()),
            "max_drawdown": max_dd,
            "trades": self.trade_count,
            "wins": self.win_count,
            "win_rate": wr,
            "slippage": self.slippage,
            "fees": self.fees,
        }


# ============================================================================
# GPIO ABSTRACTION
# ============================================================================
class GPIOStub:
    """Fully self-contained mock implementing the RPi.GPIO surface."""

    BOARD = "BOARD"
    BCM = "BCM"
    OUT = "OUT"
    IN = "IN"
    HIGH = 1
    LOW = 0

    def __init__(self):
        self._state = {}

    def setmode(self, mode):
        print("[GPIO-Stub] Mode = " + str(mode))

    def setup(self, pin, direction):
        self._state[pin] = {"dir": direction, "val": self.LOW}
        print("[GPIO-Stub] Pin " + str(pin) + " configured as " + str(direction))

    def output(self, pin, val):
        if pin in self._state:
            self._state[pin]["val"] = val
        tag = "ON" if val == self.HIGH else "OFF"
        print("[GPIO-Stub] Pin " + str(pin) + " -> " + tag)

    def cleanup(self):
        for p in self._state:
            self._state[p]["val"] = self.LOW
        print("[GPIO-Stub] cleanup() complete")


def load_gpio_backend():
    try:
        import RPi.GPIO as RealGPIO
        print("[GPIO] Real RPi.GPIO active.")
        return RealGPIO
    except Exception:
        print("[GPIO] RPi.GPIO unavailable -> GPIOStub.")
        return GPIOStub()


# ============================================================================
# SIGNAL ROUTER (table-driven)
# ============================================================================
class SignalRouter:
    """Maps regime -> GPIO pins, serial speed, optional webhook."""

    def __init__(self, serial_port="/dev/ttyUSB0", baudrate=9600,
                 webhook_url=None, gpio=None):
        self.gpio = gpio if gpio is not None else load_gpio_backend()
        self.pins = (17, 27, 22)      # green, yellow, red

        self.gpio.setmode(self.gpio.BCM)
        for p in self.pins:
            self.gpio.setup(p, self.gpio.OUT)

        self.serial = None
        try:
            import serial
            self.serial = serial.Serial(serial_port, baudrate, timeout=1)
            self.serial.flush()
            print("[SERIAL] Connected on " + serial_port)
        except Exception as e:
            print("[SERIAL] Unavailable (" + str(e) + "). Print fallback.")

        self.alert = "INIT"
        self.last_speed = None
        self.webhook_url = webhook_url

    def dispatch(self, regime, z, pnl):
        green_on, yellow_on, red_on, speed, alert = SIGNAL_TABLE.get(
            regime, (False, False, False, 0, "UNKNOWN")
        )
        self.alert = alert

        # reset all
        for p in self.pins:
            self.gpio.output(p, self.gpio.LOW)

        # apply
        if green_on:
            self.gpio.output(self.pins[0], self.gpio.HIGH)
        if yellow_on:
            self.gpio.output(self.pins[1], self.gpio.HIGH)
        if red_on:
            self.gpio.output(self.pins[2], self.gpio.HIGH)

        print("   [ROUTE] Regime " + str(regime) + " -> " + alert
              + " @ speed " + str(speed) + "%")

        # webhook on emergency
        if regime == 2 and self.webhook_url:
            try:
                import requests
                requests.post(self.webhook_url,
                              json={"status": "STOP", "pnl": pnl, "z": z},
                              timeout=2)
                print("   [WEBHOOK] Sent.")
            except Exception as e:
                print("   [WEBHOOK] Failed: " + str(e))

        # serial
        cmd = "SPEED " + str(speed) + chr(10)
        if self.serial is not None:
            try:
                self.serial.write(cmd.encode())
                print("   [SERIAL] Sent: " + cmd.strip())
            except Exception as e:
                print("   [SERIAL] Write failed: " + str(e))
        else:
            print("   [SERIAL] (print) " + cmd.strip())

        self.last_speed = speed
        return speed

    def shutdown(self):
        try:
            self.gpio.cleanup()
        except Exception:
            pass
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
        print("[SHUTDOWN] Router closed.")


# ============================================================================
# REPORTING
# ============================================================================
def build_regime_table(core):
    if not core.ledger:
        return pd.DataFrame()
    df = core.to_frame()
    wr = core.win_count / core.trade_count if core.trade_count else 0.0
    rows = []
    for regime, grp in df.groupby("regime"):
        r = int(regime)
        avg_sharpe = float(grp["sharpe_roll"].mean())
        avg_vol = float(grp["volatility"].mean())
        avg_z = float(grp["z_score"].abs().mean())
        end_pnl = float(grp["total_pnl"].iloc[-1])
        score = avg_sharpe * 0.5 - avg_vol * 0.3 + (end_pnl / 1000.0) * 0.2
        rows.append({
            "Regime": r,
            "Label": REGIME_NAMES.get(r, "Unknown"),
            "Avg Sharpe": round(avg_sharpe, 3),
            "Avg Volatility": round(avg_vol, 4),
            "Avg |Z-Score|": round(avg_z, 3),
            "Total PnL": round(end_pnl, 2),
            "Win Rate": round(wr, 3),
            "Preference Score": round(score, 3),
        })
    return pd.DataFrame(rows).sort_values("Preference Score", ascending=False)


def emit_report(core, title):
    print()
    print("=" * 80)
    print(" " + title)
    print("=" * 80)
    m = core.metrics()
    if not m:
        print("  No data recorded.")
        return
    print("  Final PnL:          {:.2f}".format(m["final_pnl"]))
    print("  Avg Rolling Sharpe: {:.3f}".format(m["avg_sharpe"]))
    print("  Max Drawdown:       {:.2f}".format(m["max_drawdown"]))
    print("  Trades: {}  Wins: {}  Win Rate: {:.1f}%".format(
        m["trades"], m["wins"], m["win_rate"] * 100))
    print("  Slippage: {:.4f}  Fees: {:.2f}".format(m["slippage"], m["fees"]))
    table = build_regime_table(core)
    if not table.empty:
        print()
        print(" REGIME RANKING (PREFERRED -> WORST):")
        print(table.to_string(index=False))


# ============================================================================
# SIMULATION FEED
# ============================================================================
class TickFeed:
    """Synthetic cointegrated tick stream with regime switching."""

    REGIME_MAP = {
        0: {"theta": 0.15, "sigma": 0.10, "drift": 0.0001},
        1: {"theta": 0.08, "sigma": 0.25, "drift": 0.0003},
        2: {"theta": 0.02, "sigma": 0.50, "drift": 0.0005},
    }
    PROBS = (0.30, 0.50, 0.20)

    def __init__(self, seed=42, hedge=1.2):
        self.rng = np.random.default_rng(seed)
        self.t = 0
        self.regime = 0
        self.age = 0
        self.life = int(self.rng.integers(30, 50))
        self.spread = 0.0
        self.price1 = 100.0
        self.hedge = hedge

    def _maybe_rotate_regime(self):
        self.age += 1
        if self.age >= self.life:
            self.regime = int(self.rng.choice([0, 1, 2], p=self.PROBS))
            self.age = 0
            self.life = int(self.rng.integers(30, 50))

    def next_tick(self):
        self.t += 1
        self._maybe_rotate_regime()
        p = self.REGIME_MAP[self.regime]

        self.spread += p["theta"] * (-self.spread) + p["sigma"] * self.rng.normal(0, 1)
        self.price1 *= float(np.exp(p["drift"] + 0.15 * self.rng.normal(0, 1)))
        price2 = (self.price1 - self.spread) / self.hedge
        price2 *= float(np.exp(0.001 * self.rng.normal(0, 1)))
        vol_units = int(self.rng.poisson(5000 + 2000 * (1 + self.regime)))

        return {
            "timestamp": self.t,
            "price1": round(self.price1, 4),
            "price2": round(price2, 4),
            "spread": round(self.spread, 4),
            "volume": vol_units,
            "regime": self.regime,
            "volatility": round(p["sigma"], 4),
        }


# ============================================================================
# SIMULATION RUNNER
# ============================================================================
def run_sim(max_ticks=200):
    print("=" * 80)
    print(" SIMULATION MODE")
    print("=" * 80)

    feed = TickFeed(seed=42)
    core = StatArbCore(window=30, entry_z=1.8, exit_z=0.4)
    router = SignalRouter()

    print("\n[INFO] Streaming synthetic ticks...\n")

    try:
        for _ in range(max_ticks):
            tick = feed.next_tick()
            row = core.process(tick)
            router.dispatch(row[5], row[4], row[10])

            print("[T{:03d}] P1:{:.2f} P2:{:.2f} Spread:{:.4f} Z:{:.2f} "
                  "Regime:{} Pos:{} PnL:{:.2f} Alert:{}".format(
                      row[0], row[1], row[2], row[3], row[4],
                      row[5], POS_TAG[row[7]], row[10], router.alert))
            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted.")
    finally:
        router.shutdown()

    emit_report(core, "FINAL REPORT - SIMULATION")
    return core


# ============================================================================
# PRODUCTION BLOCK
# ----------------------------------------------------------------------------
#   TO ENABLE:
#     1. Delete the line containing only three double-quotes directly below.
#     2. Delete the matching closing line containing only three double-quotes
#        that appears just after 'return core' at the end of run_live().
#     3. Change MODE = "simulation"  to  MODE = "production" at the top.
# ============================================================================
"""
class LiveFeed:
    '''ccxt-based ticker feed. Falls back to TickFeed when ccxt absent.'''

    def __init__(self, symbol1='BTC/USDT', symbol2='ETH/USDT', hedge=1.0):
        self.symbol1 = symbol1
        self.symbol2 = symbol2
        self.hedge = hedge
        self.exchange = None
        self.t = 0
        self.vol = 0.1
        self.hist1 = deque(maxlen=50)
        self.hist2 = deque(maxlen=50)
        self.fallback = TickFeed(seed=7)

        try:
            import ccxt
            self.exchange = ccxt.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
            })
            print('[PROD] ccxt -> Binance connected.')
        except Exception as e:
            print('[PROD] ccxt unavailable (' + str(e) + '). Synthetic fallback.')

    def _classify(self, vol):
        if vol < 0.002:
            return 0
        if vol < 0.005:
            return 1
        return 2

    def next_tick(self):
        if self.exchange is None:
            tick = self.fallback.next_tick()
            tick['regime'] = self._classify(tick['volatility'])
            return tick

        try:
            t1 = self.exchange.fetch_ticker(self.symbol1)
            t2 = self.exchange.fetch_ticker(self.symbol2)
            p1 = float(t1['last'])
            p2 = float(t2['last'])
            vol_units = int(float(t1.get('quoteVolume', 0) or 0) / 1000.0) or 1000

            self.hist1.append(p1)
            self.hist2.append(p2)

            if len(self.hist1) >= 20:
                r1 = np.diff(np.log(np.asarray(self.hist1)))
                r2 = np.diff(np.log(np.asarray(self.hist2)))
                self.vol = float(np.std(np.concatenate([r1, r2])))

            spread = p1 - self.hedge * p2
            self.t += 1

            return {
                'timestamp': self.t,
                'price1': round(p1, 4),
                'price2': round(p2, 4),
                'spread': round(spread, 4),
                'volume': vol_units,
                'regime': self._classify(self.vol),
                'volatility': round(self.vol, 6),
            }
        except Exception as e:
            print('[PROD ERROR] fetch failed: ' + str(e))
            time.sleep(1)
            return None


def run_live():
    '''Live loop: real (or fallback) data + real (or fallback) GPIO.'''
    print('=' * 80)
    print(' PRODUCTION MODE')
    print('=' * 80)

    feed = LiveFeed(symbol1='BTC/USDT', symbol2='ETH/USDT')
    core = StatArbCore(window=30, entry_z=1.8, exit_z=0.4)
    router = SignalRouter(serial_port='/dev/ttyUSB0', webhook_url=None)

    print(chr(10) + '[PROD] Streaming live data. Press Ctrl+C to stop.' + chr(10))

    try:
        while True:
            tick = feed.next_tick()
            if tick is None:
                time.sleep(1)
                continue
            row = core.process(tick)
            router.dispatch(row[5], row[4], row[10])

            print('[T{:04d}] P1:{:.2f} P2:{:.2f} Z:{:.2f} Reg:{} Pos:{} '
                  'PnL:{:.2f} Alert:{}'.format(
                      row[0], row[1], row[2], row[4], row[5],
                      POS_TAG[row[7]], row[10], router.alert))
            time.sleep(5)
    except KeyboardInterrupt:
        print(chr(10) + '[PROD] Interrupted.')
    finally:
        router.shutdown()

    emit_report(core, 'FINAL REPORT - PRODUCTION')
    return core
"""


# ============================================================================
# ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    if MODE == "production":
        if "run_live" in globals():
            print("[INFO] Mode: PRODUCTION")
            run_live()
        else:
            print("ERROR: MODE='production' but the production block is still")
            print("commented out. Either uncomment it, or set MODE='simulation'.")
            sys.exit(1)
    else:
        print("[INFO] Mode: SIMULATION")
        run_sim(max_ticks=200)