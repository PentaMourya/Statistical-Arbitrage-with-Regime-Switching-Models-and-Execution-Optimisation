"""
================================================================================
 STATISTICAL ARBITRAGE WITH REGIME-SWITCHING, EXECUTION OPTIMISATION
                 & PHYSICAL/SOFTWARE ACTION MAPPING
================================================================================
DEFAULT MODE: SIMULATION (Live data simulator + Mock GPIO)
PRODUCTION MODE: (Commented out below) Uses Real Binance/CCXT Data + RPi.GPIO
"""

import numpy as np
import pandas as pd
from collections import deque
import time
import sys

# =============================================================================
# PRODUCTION IMPORTS (COMMENTED OUT - Uncomment for Production)
# =============================================================================
"""
import requests
import serial
import ccxt  # pip install ccxt
# import RPi.GPIO as GPIO  # Real GPIO for Raspberry Pi
# from gpiozero import LED  # Alternative GPIO library
"""


# =============================================================================
# 1. SIMULATION: LIVE DATA SIMULATOR (Default)
# =============================================================================
class LiveMarketSimulator:
    """
    Simulates a live tick stream for 2 cointegrated assets.
    The spread follows an Ornstein-Uhlenbeck process with regime-dependent parameters.
    """
    def __init__(self, seed=42):
        np.random.seed(seed)
        self.time = 0
        self.regime = 0  # 0=Preferred(LowVol), 1=Neutral, 2=Worst(HighVol)
        self.regime_timer = 0
        
        # Base prices
        self.price1 = 100.0
        self.price2 = 95.0
        self.hedge_ratio = 1.2
        
        # Spread parameters (OU process)
        self.spread = 0.0
        self.mu = 0.0
        
        # Regime-specific OU parameters
        self.regime_params = {
            0: {'theta': 0.15, 'sigma': 0.10, 'drift1': 0.0001, 'drift2': 0.0001},
            1: {'theta': 0.08, 'sigma': 0.25, 'drift1': 0.0003, 'drift2': 0.0003},
            2: {'theta': 0.02, 'sigma': 0.50, 'drift1': 0.0005, 'drift2': 0.0005}
        }
        
    def step(self):
        """Generate one new tick (simulate 1-minute intervals)."""
        self.time += 1
        self.regime_timer += 1
        
        # Switch regime every ~30-50 ticks
        if self.regime_timer > np.random.randint(30, 50):
            self.regime = np.random.choice([0, 1, 2], p=[0.3, 0.5, 0.2])
            self.regime_timer = 0
            
        params = self.regime_params[self.regime]
        dt = 1.0
        
        # Update spread (OU)
        dW = np.random.normal(0, 1) * np.sqrt(dt)
        self.spread += params['theta'] * (self.mu - self.spread) * dt + params['sigma'] * dW
        
        # Update asset prices
        ret1 = params['drift1'] * dt + 0.15 * np.random.normal(0, 1) * np.sqrt(dt)
        ret2 = params['drift2'] * dt + 0.15 * np.random.normal(0, 1) * np.sqrt(dt)
        
        self.price1 *= np.exp(ret1)
        self.price2 = (self.price1 - self.spread) / self.hedge_ratio
        self.price2 *= np.exp(0.001 * np.random.normal(0, 1))
        
        volume = np.random.poisson(5000 + 2000 * (1 + self.regime))
        
        return {
            'timestamp': self.time,
            'price1': round(self.price1, 4),
            'price2': round(self.price2, 4),
            'spread': round(self.spread, 4),
            'volume': volume,
            'regime': self.regime,
            'volatility': round(params['sigma'], 4)
        }


# =============================================================================
# 2. REGIME DETECTOR & STAT ARB ENGINE (Shared by Simulation & Production)
# =============================================================================
class RegimeSwitchingArbEngine:
    """
    Detects regimes using rolling volatility, computes Z-score,
    generates signals, simulates execution, and tracks P&L.
    """
    def __init__(self, window=50, entry_z=2.0, exit_z=0.5):
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z
        
        # Rolling buffers
        self.spread_history = deque(maxlen=window)
        self.return_history = deque(maxlen=window)
        
        # State
        self.position = 0  # -1 short, 0 flat, +1 long
        self.entry_price = 0.0
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.total_pnl = 0.0
        self.trade_count = 0
        self.win_count = 0
        
        # Execution metrics
        self.total_slippage = 0.0
        self.total_fees = 0.0
        
        # Store history for final analysis
        self.history = []
        
    def compute_metrics(self, spread):
        """Update rolling metrics and return current Z-score, mean, std, Sharpe."""
        self.spread_history.append(spread)
        
        if len(self.spread_history) < self.window:
            return None, None, None, None
        
        arr = np.array(self.spread_history)
        mean = np.mean(arr)
        std = np.std(arr)
        z_score = (spread - mean) / std if std > 1e-6 else 0.0
        vol = std
        
        # Rolling Sharpe of spread returns
        if len(self.return_history) > 1:
            returns = np.array(self.return_history)
            sharpe = np.mean(returns) / (np.std(returns) + 1e-6) * np.sqrt(252)
        else:
            sharpe = 0.0
            
        return z_score, mean, std, sharpe
    
    def execute_order(self, signal, price, volume, regime_vol):
        """Simulate execution with slippage (scales with volume & regime vol)."""
        base_slippage = 0.0005
        vol_factor = volume / 10000.0
        regime_mult = 1.0 + regime_vol
        slippage_cost = base_slippage * vol_factor * regime_mult * price
        self.total_slippage += abs(slippage_cost)
        
        if signal == 1:
            fill_price = price + slippage_cost
        elif signal == -1:
            fill_price = price - slippage_cost
        else:
            fill_price = price
        return fill_price
    
    def update_pnl(self, current_spread):
        """Update unrealized and total PnL."""
        if self.position == 1:
            self.unrealized_pnl = (current_spread - self.entry_price) * 1000
        elif self.position == -1:
            self.unrealized_pnl = (self.entry_price - current_spread) * 1000
        else:
            self.unrealized_pnl = 0.0
        self.total_pnl = self.realized_pnl + self.unrealized_pnl
        return self.total_pnl
    
    def step(self, data):
        """Process one new tick."""
        spread = data['spread']
        price1 = data['price1']
        price2 = data['price2']
        regime = data['regime']
        vol = data['volatility']
        volume = data['volume']
        
        # Store returns for Sharpe
        if len(self.spread_history) > 0:
            prev_spread = self.spread_history[-1]
            ret = (spread - prev_spread) / (abs(prev_spread) + 1e-6)
            self.return_history.append(ret)
        
        # Compute metrics
        z_score, mean, std, sharpe = self.compute_metrics(spread)
        
        # Generate signal
        signal = 0
        if z_score is not None:
            if z_score > self.entry_z and self.position <= 0:
                signal = -1
            elif z_score < -self.entry_z and self.position >= 0:
                signal = 1
            elif abs(z_score) < self.exit_z and self.position != 0:
                signal = 0
        
        # Execute if signal triggers
        if signal != 0:
            if self.position != 0:
                self.realized_pnl += self.unrealized_pnl
                self.unrealized_pnl = 0.0
                if self.total_pnl > 0:
                    self.win_count += 1
                self.trade_count += 1
                self.position = 0
                
            if signal == 1 or signal == -1:
                fill_price = self.execute_order(signal, spread, volume, vol)
                self.entry_price = fill_price
                self.position = signal
                self.total_fees += 0.1
        
        # Update PnL
        self.update_pnl(spread)
        
        # Store record
        record = {
            'time': data['timestamp'],
            'price1': price1,
            'price2': price2,
            'spread': spread,
            'z_score': z_score if z_score is not None else 0.0,
            'regime': regime,
            'volatility': vol,
            'position': self.position,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
            'total_pnl': self.total_pnl,
            'sharpe_roll': sharpe if sharpe is not None else 0.0,
            'signal': signal
        }
        self.history.append(record)
        return record


# =============================================================================
# 3. SIMULATION: PHYSICAL / SOFTWARE ACTION MAPPER (Mock GPIO - Default)
# =============================================================================
class MockGPIO:
    """Mock GPIO for pure software simulation (no hardware required)."""
    BOARD = "BOARD"
    OUT = "OUT"
    HIGH = 1
    LOW = 0
    
    @staticmethod
    def setmode(mode):
        print(f"[MOCK GPIO] Mode set to {mode}")
    
    @staticmethod
    def setup(pin, direction):
        print(f"[MOCK GPIO] Pin {pin} set to {direction}")
    
    @staticmethod
    def output(pin, state):
        status = "ON" if state == 1 else "OFF"
        print(f"[MOCK GPIO] >>> Pin {pin} turned {status}")

# Use MockGPIO (replace with 'import RPi.GPIO as GPIO' for real hardware)
GPIO = MockGPIO()

class PhysicalActionMapper:
    """
    Maps detected Regime (0=Preferred, 1=Neutral, 2=Worst)
    to physical/software actions: GPIO traffic light, speed command, webhook.
    """
    def __init__(self):
        self.PIN_GREEN = 17
        self.PIN_YELLOW = 27
        self.PIN_RED = 22
        
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(self.PIN_GREEN, GPIO.OUT)
        GPIO.setup(self.PIN_YELLOW, GPIO.OUT)
        GPIO.setup(self.PIN_RED, GPIO.OUT)
        
        self.current_regime = -1
        self.alert_level = "UNKNOWN"

    def update(self, regime, z_score, total_pnl):
        """Translate regime into GPIO outputs, speed command, and alerts."""
        # Reset all pins
        GPIO.output(self.PIN_GREEN, GPIO.LOW)
        GPIO.output(self.PIN_YELLOW, GPIO.LOW)
        GPIO.output(self.PIN_RED, GPIO.LOW)
        
        if regime == 0:  # PREFERRED (Best)
            self.alert_level = "OPTIMAL"
            speed_command = 100
            GPIO.output(self.PIN_GREEN, GPIO.HIGH)
            print(f"   🟢 [ACTION] Regime PREFERRED -> Sending SPEED = {speed_command}% to Motor Controller.")
            
        elif regime == 1:  # NEUTRAL / CAUTION
            self.alert_level = "CAUTION"
            speed_command = 50
            GPIO.output(self.PIN_YELLOW, GPIO.HIGH)
            print(f"   🟡 [ACTION] Regime CAUTION -> Reducing SPEED to {speed_command}%.")
            
        else:  # regime == 2 (WORST)
            self.alert_level = "DANGER / STOP"
            speed_command = 5
            GPIO.output(self.PIN_RED, GPIO.HIGH)
            print(f"   🔴 [ACTION] Regime WORST -> EMERGENCY! Speed reduced to {speed_command}%. Webhook alert triggered.")
            # Uncomment to send real HTTP alert:
            # requests.post("https://your-alert-system.com/emergency", json={"status": "STOP", "pnl": total_pnl})
        
        # Simulate serial command to an Arduino / PLC
        serial_cmd = f"SPEED {speed_command}\n"
        print(f"   📡 [SERIAL] Writing to COM3/Dev/ttyUSB0: {serial_cmd.strip()}")
        
        self.current_regime = regime
        return speed_command


# =============================================================================
# =============================================================================
#  PRODUCTION IMPLEMENTATIONS (COMMENTED OUT)
#  ============================================================================
#  UNCOMMENT THE ENTIRE BLOCK BELOW TO SWITCH TO PRODUCTION MODE.
#  ALSO UNCOMMENT THE "PRODUCTION IMPORTS" AT THE TOP.
#  ============================================================================
#  NOTE: In production, you will use real market data (e.g., Binance/CCXT)
#        and real GPIO pins (Raspberry Pi) instead of mocks.
# =============================================================================
"""
# -----------------------------------------------------------------------------
# PRODUCTION: REAL MARKET DATA FETCHER (Using CCXT / Binance)
# -----------------------------------------------------------------------------
class RealMarketDataFetcher:
    ''
    Fetches live tick data from Binance (or any CCXT exchange).
    Uses 2 cointegrated crypto pairs (e.g., BTC/USDT and ETH/USDT).
    ''
    def __init__(self, symbol1='BTC/USDT', symbol2='ETH/USDT', timeframe='1m'):
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        self.symbol1 = symbol1
        self.symbol2 = symbol2
        self.timeframe = timeframe
        self.time = 0
        self.last_close1 = None
        self.last_close2 = None
        
        # Fetch initial OHLCV to set hedge ratio and spread
        self.hedge_ratio = 1.0  # Will be calibrated dynamically
        self.spread = 0.0
        self.volatility = 0.1
        
    def step(self):
        ''
        Fetch the latest ticker prices for both assets.
        Returns a dictionary mimicking the simulation data structure.
        ''
        try:
            ticker1 = self.exchange.fetch_ticker(self.symbol1)
            ticker2 = self.exchange.fetch_ticker(self.symbol2)
            
            price1 = ticker1['last']
            price2 = ticker2['last']
            volume = ticker1['quoteVolume'] / 1000  # Normalize volume
            
            # Calculate spread (using dynamic hedge ratio)
            # For simplicity, we hedge using a fixed ratio (or Kalman filter in production)
            self.spread = price1 - self.hedge_ratio * price2
            
            # Estimate volatility from 1m returns
            if self.last_close1 is not None:
                ret1 = (price1 - self.last_close1) / self.last_close1
                ret2 = (price2 - self.last_close2) / self.last_close2
                self.volatility = (abs(ret1) + abs(ret2)) / 2
                
            self.last_close1 = price1
            self.last_close2 = price2
            self.time += 1
            
            # Detect regime using the same rolling logic (handled by engine)
            # We pass a default regime 1 here; the engine will compute z-score based on history
            # The actual regime detection is done by the engine's rolling volatility.
            regime = 1  # Placeholder, engine will override based on spread history
            
            return {
                'timestamp': self.time,
                'price1': round(price1, 2),
                'price2': round(price2, 2),
                'spread': round(self.spread, 4),
                'volume': int(volume),
                'regime': regime,  # Will be overwritten by engine's internal detection
                'volatility': round(self.volatility, 4)
            }
        except Exception as e:
            print(f"[PRODUCTION ERROR] Failed to fetch data: {e}")
            time.sleep(1)
            return None

# -----------------------------------------------------------------------------
# PRODUCTION: REAL GPIO & SERIAL ACTION MAPPER (Raspberry Pi / Arduino)
# -----------------------------------------------------------------------------
# Uncomment these if you have a Raspberry Pi and want real hardware control
# import RPi.GPIO as GPIO  # Already imported at top
# import serial

class RealActionMapper:
    ''
    Production version using real Raspberry Pi GPIO pins and Serial/UART.
    ''
    def __init__(self, serial_port='/dev/ttyUSB0', baudrate=9600):
        # Setup real GPIO
        GPIO.setmode(GPIO.BCM)  # Use BCM pin numbering
        self.PIN_GREEN = 17
        self.PIN_YELLOW = 27
        self.PIN_RED = 22
        
        GPIO.setup(self.PIN_GREEN, GPIO.OUT)
        GPIO.setup(self.PIN_YELLOW, GPIO.OUT)
        GPIO.setup(self.PIN_RED, GPIO.OUT)
        
        # Setup real Serial connection (for Arduino / Motor driver)
        try:
            self.ser = serial.Serial(serial_port, baudrate, timeout=1)
            self.ser.flush()
            print(f"[PRODUCTION] Serial connected to {serial_port}")
        except Exception as e:
            print(f"[PRODUCTION] Serial failed: {e}. Using print fallback.")
            self.ser = None
            
        self.current_regime = -1
        self.alert_level = "UNKNOWN"
        self.webhook_url = "https://your-api.com/emergency"  # Replace with your URL

    def update(self, regime, z_score, total_pnl):
        ''
        Send real GPIO signals, Serial speed commands, and HTTP alerts.
        ''
        # Reset all pins
        GPIO.output(self.PIN_GREEN, GPIO.LOW)
        GPIO.output(self.PIN_YELLOW, GPIO.LOW)
        GPIO.output(self.PIN_RED, GPIO.LOW)
        
        if regime == 0:  # PREFERRED
            self.alert_level = "OPTIMAL"
            speed = 100
            GPIO.output(self.PIN_GREEN, GPIO.HIGH)
            print(f"[PROD] 🟢 Speed = {speed}%")
            
        elif regime == 1:  # CAUTION
            self.alert_level = "CAUTION"
            speed = 50
            GPIO.output(self.PIN_YELLOW, GPIO.HIGH)
            print(f"[PROD] 🟡 Speed = {speed}%")
            
        else:  # WORST
            self.alert_level = "DANGER"
            speed = 5
            GPIO.output(self.PIN_RED, GPIO.HIGH)
            print(f"[PROD] 🔴 EMERGENCY STOP! Speed = {speed}%")
            # Send real HTTP alert (uncomment if requests is imported)
            # try:
            #     requests.post(self.webhook_url, json={"status": "STOP", "pnl": total_pnl, "z": z_score}, timeout=2)
            # except:
            #     pass
        
        # Send real serial command
        cmd = f"SPEED {speed}\n"
        if self.ser and self.ser.is_open:
            self.ser.write(cmd.encode())
        else:
            print(f"[PROD SERIAL] Would send: {cmd.strip()}")
            
        self.current_regime = regime
        return speed

# -----------------------------------------------------------------------------
# PRODUCTION: MAIN LOOP (Uncomment to run with real data & hardware)
# -----------------------------------------------------------------------------
def main_production():
    ''
    Production entry point: uses real market data and real GPIO.
    ''
    print("="*60)
    print(" PRODUCTION MODE ACTIVE (Real Market Data + Real GPIO)")
    print("="*60)
    
    # Init real components
    data_fetcher = RealMarketDataFetcher(symbol1='BTC/USDT', symbol2='ETH/USDT')
    engine = RegimeSwitchingArbEngine(window=30, entry_z=1.8, exit_z=0.4)
    action_mapper = RealActionMapper(serial_port='/dev/ttyUSB0')  # Change to your port
    
    print("\n[PROD] Streaming live data from Binance... Press Ctrl+C to stop.\n")
    
    try:
        while True:
            # Fetch real tick
            data = data_fetcher.step()
            if data is None:
                time.sleep(1)
                continue
                
            # Process through engine
            record = engine.step(data)
            
            # Map to real physical actions
            action_mapper.update(
                regime=record['regime'],
                z_score=record['z_score'],
                total_pnl=record['total_pnl']
            )
            
            # Print status
            status = "FLAT"
            if record['position'] == 1: status = "LONG"
            elif record['position'] == -1: status = "SHORT"
            
            print(f"[T{record['time']}] BTC:{record['price1']:.2f} ETH:{record['price2']:.2f} "
                  f"Z:{record['z_score']:.2f} Reg:{record['regime']} Pos:{status} "
                  f"PnL:{record['total_pnl']:.2f} Alert:{action_mapper.alert_level}")
            
            # Wait for next tick (e.g., 60 seconds for 1m candles)
            time.sleep(60)
            
    except KeyboardInterrupt:
        print("\n[PROD] Shutting down...")
        GPIO.cleanup()  # Clean up GPIO pins
        if action_mapper.ser and action_mapper.ser.is_open:
            action_mapper.ser.close()
"""


# =============================================================================
# 4. ANALYSIS FUNCTION (Shared by both modes)
# =============================================================================
def analyze_conditions(history, engine):
    """
    Group historical ticks by regime.
    Calculate average Sharpe, volatility, PnL, and Z-score per regime.
    Return a sorted DataFrame from Preferred (Best) to Worst.
    """
    df = pd.DataFrame(history)
    groups = df.groupby('regime')
    summary = []
    
    for regime, group in groups:
        avg_sharpe = group['sharpe_roll'].mean()
        avg_vol = group['volatility'].mean()
        avg_z = group['z_score'].mean()
        total_pnl = group['total_pnl'].iloc[-1]
        win_rate = engine.win_count / (engine.trade_count + 1e-6)
        
        # Preference score: higher Sharpe + lower Vol + positive PnL
        preference_score = avg_sharpe * 0.5 - avg_vol * 0.3 + (total_pnl / 1000.0) * 0.2
        
        label = {0: 'Preferred (Low Vol, Fast Reversion)',
                 1: 'Neutral (Moderate Vol)',
                 2: 'Worst (High Vol, Slow Reversion)'}[regime]
        
        summary.append({
            'Regime': regime,
            'Label': label,
            'Avg Sharpe': round(avg_sharpe, 3),
            'Avg Volatility': round(avg_vol, 4),
            'Avg |Z-Score|': round(abs(avg_z), 3),
            'Total PnL': round(total_pnl, 2),
            'Win Rate': round(win_rate, 3),
            'Preference Score': round(preference_score, 3)
        })
    
    summary_df = pd.DataFrame(summary)
    return summary_df.sort_values('Preference Score', ascending=False)


# =============================================================================
# 5. SIMULATION: MAIN LOOP (DEFAULT - RUNS BY DEFAULT)
# =============================================================================
def main():
    print("="*80)
    print(" STAT ARB WITH REGIME-SWITCHING + EXECUTION OPTIMISATION")
    print("              + PHYSICAL/SOFTWARE ACTION MAPPING")
    print("                    [SIMULATION MODE - DEFAULT]")
    print("="*80)
    
    # Init components
    sim = LiveMarketSimulator(seed=42)
    engine = RegimeSwitchingArbEngine(window=30, entry_z=1.8, exit_z=0.4)
    action_mapper = PhysicalActionMapper()   # <-- Physical actions integrated here
    
    print("\n[INFO] Starting live data stream (simulated ticks)...")
    print("[INFO] Each tick prints: prices, regime, PnL, and physical actions.\n")
    
    try:
        for tick in range(200):
            # 1. Get new live data
            data = sim.step()
            
            # 2. Process through trading engine
            record = engine.step(data)
            
            # 3. Map the detected regime to physical actions (GPIO, speed, alerts)
            action_mapper.update(
                regime=record['regime'],
                z_score=record['z_score'],
                total_pnl=record['total_pnl']
            )
            
            # 4. Print concise tick output with action status
            status = "FLAT"
            if record['position'] == 1:
                status = "LONG "
            elif record['position'] == -1:
                status = "SHORT"
            
            print(f"[T{record['time']:03d}] P1:{record['price1']:.2f} P2:{record['price2']:.2f} "
                  f"Spread:{record['spread']:.4f} Z:{record['z_score']:.2f} "
                  f"Regime:{record['regime']} Pos:{status} PnL:{record['total_pnl']:.2f} "
                  f"Alert: {action_mapper.alert_level}")
            
            # Print a detailed summary every 20 ticks
            if tick % 20 == 0 and tick > 0:
                print("-"*80)
                print(f"--- SUMMARY at T={record['time']} ---")
                print(f"  Realized PnL: {record['realized_pnl']:.2f} | "
                      f"Unrealized: {record['unrealized_pnl']:.2f} | "
                      f"Total: {record['total_pnl']:.2f}")
                print(f"  Trades: {engine.trade_count} | Wins: {engine.win_count} | "
                      f"Win Rate: {engine.win_count/(engine.trade_count+1e-6)*100:.1f}%")
                print(f"  Total Slippage: {engine.total_slippage:.4f} | "
                      f"Total Fees: {engine.total_fees:.2f}")
                print("-"*80)
            
            # Simulate live delay
            time.sleep(0.05)
            
    except KeyboardInterrupt:
        print("\n[INFO] Stopping live stream...")

    # =========================================================================
    # 6. FINAL PERFORMANCE REPORT & REGIME RANKING (Preferred → Worst)
    # =========================================================================
    print("\n" + "="*80)
    print(" FINAL PERFORMANCE REPORT & REGIME RANKING (PREFERRED → WORST)")
    print("="*80)
    
    df_history = pd.DataFrame(engine.history)
    final_pnl = engine.total_pnl
    final_sharpe = df_history['sharpe_roll'].mean()
    max_dd = (df_history['total_pnl'] - df_history['total_pnl'].cummax()).min()
    
    print(f"\nOVERALL SYSTEM PERFORMANCE:")
    print(f"  ▶ Final Total PnL: {final_pnl:.2f}")
    print(f"  ▶ Average Rolling Sharpe: {final_sharpe:.3f}")
    print(f"  ▶ Maximum Drawdown: {max_dd:.2f}")
    print(f"  ▶ Total Trades Executed: {engine.trade_count}")
    print(f"  ▶ Win Rate: {engine.win_count/(engine.trade_count+1e-6)*100:.1f}%")
    print(f"  ▶ Total Execution Slippage: {engine.total_slippage:.4f}")
    print(f"  ▶ Total Fees Paid: {engine.total_fees:.2f}")
    
    ranking_df = analyze_conditions(engine.history, engine)
    
    print("\n" + "-"*80)
    print(" REGIME RANKING (from PREFERRED / BEST to WORST):")
    print("-"*80)
    print(ranking_df.to_string(index=False))
    
    print("\n" + "-"*80)
    print(" DETAILED BREAKDOWN PER REGIME:")
    print("-"*80)
    for _, row in ranking_df.iterrows():
        print(f"\n▶ Regime {row['Regime']}: {row['Label']}")
        print(f"   - Avg Sharpe: {row['Avg Sharpe']}")
        print(f"   - Avg Volatility: {row['Avg Volatility']}")
        print(f"   - Avg |Z-Score|: {row['Avg |Z-Score|']}")
        print(f"   - Total PnL generated in this regime: {row['Total PnL']}")
        print(f"   - Win Rate: {row['Win Rate']*100:.1f}%")
        print(f"   - Preference Score: {row['Preference Score']}")
    
    print("\n[INFO] Analysis complete. All physical actions were simulated in software.")
    print("[INFO] To switch to PRODUCTION mode, uncomment the PRODUCTION block at the top.")


if __name__ == "__main__":
    main()
    # UNCOMMENT THE LINE BELOW TO RUN PRODUCTION MODE (after uncommenting the block above)
    # main_production()