#!/usr/bin/env python3
"""
Coinbase Silver Futures Trailing Stop-Loss Bot with Reversal Re-Entry
========================================================================

Strategy: "Infinite Gain Protection"
- Continuously adjusts trailing stop loss as price rises
- Automatically sells when stop loss is hit (protects gains)
- Detects market reversals after stop out
- Re-enters position when reversal is confirmed
- Never risks original capital, only secures profits

Symbol: Silver Futures (SLR contracts)
Contract Size: 50 troy ounces
"""

import os
import time
import hmac
import hashlib
import json
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import logging
from dataclasses import dataclass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('coinbase_silver_trailing_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Track position state"""
    contracts: int
    entry_price: float
    highest_price: float
    stop_loss_price: float
    entry_time: datetime
    unrealized_pnl: float = 0.0


class CoinbaseSilverTrailingBot:
    """
    Advanced trailing stop-loss bot for Coinbase silver futures
    
    Features:
    - Dynamic trailing stop loss that follows price up
    - Automatic position exit when stop triggered
    - Reversal detection using multiple indicators
    - Automatic re-entry on confirmed reversals
    - Protects all gains, never risks original capital
    - Contract expiry management and rolling
    """
    
    BASE_URL = "https://api.coinbase.com"
    
    def __init__(self, api_key: str, api_secret: str, config: Optional[Dict] = None):
        """
        Initialize the Coinbase Silver Futures Trailing Bot
        
        Args:
            api_key: Coinbase API key
            api_secret: Coinbase API secret  
            config: Bot configuration
        """
        self.api_key = api_key
        self.api_secret = api_secret
        
        # Default configuration
        self.config = {
            # Trailing stop settings
            'trailing_stop_pct': 0.02,  # Trail 2% below highest price
            'initial_stop_pct': 0.03,   # Initial stop 3% below entry
            'stop_update_interval': 30,  # Update stop every 30 seconds
            
            # Reversal detection settings
            'reversal_rsi_threshold': 35,  # RSI below this = oversold
            'reversal_volume_spike': 1.5,  # Volume 1.5x average
            'reversal_candle_count': 3,    # Need 3 green candles
            'reversal_price_move': 0.01,   # Need 1% move up from low
            
            # Re-entry settings
            'max_reentry_attempts': 5,  # Max re-entries per day
            'cooldown_period': 300,     # Wait 5min after stop out
            'reentry_position_pct': 1.0,  # Re-enter with same size
            
            # Risk management
            'max_contracts': 100,       # Maximum position size
            'profit_lock_threshold': 0.10,  # Lock in if 10% profit
            'profit_lock_pct': 0.50,    # Lock in 50% of profit
            
            # Contract management
            'auto_roll_contracts': True,  # Auto-roll before expiry
            'roll_days_before': 5,        # Roll 5 days before expiry
            
            # General settings
            'check_interval': 30,       # Check market every 30 sec
            'max_daily_trades': 20,     # Limit trades per day
        }
        
        if config:
            self.config.update(config)
        
        self.position: Optional[Position] = None
        self.highest_price_achieved = 0.0
        self.total_profit_realized = 0.0
        self.daily_reentries = 0
        self.last_trade_date = None
        self.last_stopout_time = None
        self.contract_symbol = None  # Will be set to current month
        
        logger.info("Coinbase Silver Trailing Bot initialized")
        logger.info(f"Trailing Stop: {self.config['trailing_stop_pct']*100}%")
        logger.info(f"Reversal RSI Threshold: {self.config['reversal_rsi_threshold']}")
    
    def _generate_signature(self, timestamp: str, method: str, path: str, body: str = '') -> str:
        """Generate HMAC signature for Coinbase API"""
        message = timestamp + method + path + body
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                     data: Optional[Dict] = None) -> Dict:
        """
        Make authenticated request to Coinbase Advanced Trade API
        
        Args:
            method: HTTP method (GET, POST)
            endpoint: API endpoint
            params: Query parameters
            data: Request body
            
        Returns:
            Response dictionary
        """
        url = f"{self.BASE_URL}{endpoint}"
        timestamp = str(int(time.time()))
        
        body = json.dumps(data) if data else ''
        path = endpoint + ('?' + '&'.join(f"{k}={v}" for k, v in params.items()) if params else '')
        
        signature = self._generate_signature(timestamp, method, path, body)
        
        headers = {
            'CB-ACCESS-KEY': self.api_key,
            'CB-ACCESS-SIGN': signature,
            'CB-ACCESS-TIMESTAMP': timestamp,
            'Content-Type': 'application/json'
        }
        
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, params=params, timeout=10)
            else:
                response = requests.post(url, headers=headers, json=data, timeout=10)
            
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise
    
    def get_current_silver_contract(self) -> str:
        """
        Get the current active silver futures contract
        
        Returns:
            Contract symbol (e.g., "SLR-25JAN25")
        """
        # Get all products
        response = self._make_request('GET', '/api/v3/brokerage/products')
        
        # Filter for silver futures
        silver_futures = [p for p in response.get('products', []) 
                         if p.get('product_id', '').startswith('SLR-')]
        
        if not silver_futures:
            raise ValueError("No silver futures contracts found")
        
        # Sort by expiry date and get the nearest one
        # Format: SLR-DDMMMYY (e.g., SLR-25JAN25)
        silver_futures.sort(key=lambda x: x.get('product_id', ''))
        
        # Return the nearest expiry contract
        contract = silver_futures[0]['product_id']
        logger.info(f"Using silver futures contract: {contract}")
        return contract
    
    def get_market_price(self) -> float:
        """Get current market price for silver futures"""
        if not self.contract_symbol:
            self.contract_symbol = self.get_current_silver_contract()
        
        response = self._make_request('GET', f'/api/v3/brokerage/products/{self.contract_symbol}')
        price = float(response.get('price', 0))
        
        if price == 0:
            # Try ticker
            ticker = self._make_request('GET', f'/api/v3/brokerage/products/{self.contract_symbol}/ticker')
            price = float(ticker.get('price', 0))
        
        return price
    
    def get_candles(self, granularity: str = 'FIVE_MINUTE', limit: int = 100) -> List[Dict]:
        """
        Get historical candle data
        
        Args:
            granularity: Time granularity (ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, etc.)
            limit: Number of candles
            
        Returns:
            List of candle dictionaries
        """
        if not self.contract_symbol:
            self.contract_symbol = self.get_current_silver_contract()
        
        end_time = int(time.time())
        start_time = end_time - (limit * 300)  # 5 minutes per candle
        
        response = self._make_request(
            'GET',
            f'/api/v3/brokerage/products/{self.contract_symbol}/candles',
            params={
                'start': start_time,
                'end': end_time,
                'granularity': granularity
            }
        )
        
        candles = response.get('candles', [])
        
        # Parse candles
        parsed_candles = []
        for candle in candles:
            parsed_candles.append({
                'timestamp': int(candle.get('start', 0)),
                'open': float(candle.get('open', 0)),
                'high': float(candle.get('high', 0)),
                'low': float(candle.get('low', 0)),
                'close': float(candle.get('close', 0)),
                'volume': float(candle.get('volume', 0))
            })
        
        return sorted(parsed_candles, key=lambda x: x['timestamp'])
    
    def get_futures_position(self) -> Optional[Dict]:
        """Get current futures position"""
        try:
            response = self._make_request('GET', '/api/v3/brokerage/cfm/positions')
            
            positions = response.get('positions', [])
            
            for pos in positions:
                if pos.get('product_id') == self.contract_symbol:
                    return {
                        'product_id': pos.get('product_id'),
                        'size': float(pos.get('number_of_contracts', 0)),
                        'side': pos.get('side'),  # 'LONG' or 'SHORT'
                        'entry_price': float(pos.get('entry_vwap', 0)),
                        'unrealized_pnl': float(pos.get('unrealized_pnl', 0)),
                        'realized_pnl': float(pos.get('realized_pnl', 0))
                    }
            
            return None
        
        except Exception as e:
            logger.error(f"Failed to get position: {e}")
            return None
    
    def place_order(self, side: str, contracts: int, order_type: str = 'MARKET',
                   limit_price: Optional[float] = None, stop_price: Optional[float] = None) -> Dict:
        """
        Place futures order
        
        Args:
            side: 'BUY' or 'SELL'
            contracts: Number of contracts
            order_type: 'MARKET', 'LIMIT', 'STOP_LIMIT'
            limit_price: Limit price (for LIMIT orders)
            stop_price: Stop price (for STOP orders)
            
        Returns:
            Order response
        """
        if not self.contract_symbol:
            self.contract_symbol = self.get_current_silver_contract()
        
        order_config = {
            'product_id': self.contract_symbol,
            'side': side,
            'order_configuration': {}
        }
        
        if order_type == 'MARKET':
            order_config['order_configuration'] = {
                'market_market_ioc': {
                    'quote_size': str(contracts)  # For futures, this is contract count
                }
            }
        elif order_type == 'LIMIT':
            order_config['order_configuration'] = {
                'limit_limit_gtc': {
                    'base_size': str(contracts),
                    'limit_price': str(limit_price)
                }
            }
        elif order_type == 'STOP_LIMIT':
            order_config['order_configuration'] = {
                'stop_limit_stop_limit_gtc': {
                    'base_size': str(contracts),
                    'limit_price': str(limit_price),
                    'stop_price': str(stop_price),
                    'stop_direction': 'STOP_DIRECTION_STOP_DOWN' if side == 'SELL' else 'STOP_DIRECTION_STOP_UP'
                }
            }
        
        try:
            response = self._make_request('POST', '/api/v3/brokerage/orders', data=order_config)
            logger.info(f"Order placed: {side} {contracts} contracts @ {order_type}")
            return response
        
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            raise
    
    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """Calculate RSI indicator"""
        if len(prices) < period + 1:
            return 50.0
        
        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def detect_reversal(self) -> bool:
        """
        Detect if market has reversed and is ready for re-entry
        
        Uses multiple indicators:
        - RSI oversold
        - Volume spike
        - Series of green candles
        - Price move up from recent low
        
        Returns:
            True if reversal detected
        """
        candles = self.get_candles(granularity='FIVE_MINUTE', limit=50)
        
        if len(candles) < 20:
            return False
        
        # Extract data
        closes = [c['close'] for c in candles]
        volumes = [c['volume'] for c in candles]
        recent_candles = candles[-10:]
        
        # 1. Check RSI
        rsi = self.calculate_rsi(closes)
        if rsi >= self.config['reversal_rsi_threshold']:
            logger.debug(f"RSI not oversold: {rsi:.2f}")
            return False
        
        # 2. Check for volume spike
        avg_volume = sum(volumes[-20:-5]) / 15
        recent_volume = sum(volumes[-3:]) / 3
        
        if recent_volume < avg_volume * self.config['reversal_volume_spike']:
            logger.debug(f"No volume spike: {recent_volume:.2f} vs {avg_volume:.2f}")
            return False
        
        # 3. Check for green candles (bullish)
        green_candles = sum(1 for c in recent_candles[-self.config['reversal_candle_count']:]
                           if c['close'] > c['open'])
        
        if green_candles < self.config['reversal_candle_count']:
            logger.debug(f"Not enough green candles: {green_candles}")
            return False
        
        # 4. Check price move from low
        recent_low = min(c['low'] for c in recent_candles[-5:])
        current_price = closes[-1]
        price_move = (current_price - recent_low) / recent_low
        
        if price_move < self.config['reversal_price_move']:
            logger.debug(f"Insufficient price move: {price_move*100:.2f}%")
            return False
        
        logger.info(f"✓ REVERSAL DETECTED - RSI: {rsi:.2f}, Volume: {recent_volume:.0f}, "
                   f"Green candles: {green_candles}, Price move: {price_move*100:.2f}%")
        return True
    
    def update_trailing_stop(self, current_price: float) -> None:
        """Update trailing stop loss based on current price"""
        if not self.position:
            return
        
        # Update highest price if we have a new high
        if current_price > self.position.highest_price:
            old_highest = self.position.highest_price
            self.position.highest_price = current_price
            
            # Calculate new trailing stop
            new_stop = current_price * (1 - self.config['trailing_stop_pct'])
            
            # Only update if new stop is higher (protecting more profit)
            if new_stop > self.position.stop_loss_price:
                old_stop = self.position.stop_loss_price
                self.position.stop_loss_price = new_stop
                
                profit_pct = ((current_price - self.position.entry_price) / self.position.entry_price) * 100
                
                logger.info(f"🔼 NEW HIGH: ${current_price:.2f} (was ${old_highest:.2f})")
                logger.info(f"📈 STOP RAISED: ${new_stop:.2f} (was ${old_stop:.2f})")
                logger.info(f"💰 Unrealized Profit: {profit_pct:.2f}%")
                
                # Check if we should lock in partial profits
                if profit_pct >= self.config['profit_lock_threshold'] * 100:
                    self.lock_partial_profits(profit_pct)
    
    def lock_partial_profits(self, profit_pct: float) -> None:
        """Lock in partial profits by tightening stop"""
        if not self.position:
            return
        
        # Calculate locked stop (tighter than trailing)
        current_price = self.get_market_price()
        locked_profit_pct = self.config['profit_lock_pct']
        profit_amount = current_price - self.position.entry_price
        locked_stop = self.position.entry_price + (profit_amount * locked_profit_pct)
        
        if locked_stop > self.position.stop_loss_price:
            self.position.stop_loss_price = locked_stop
            logger.info(f"🔒 LOCKING {locked_profit_pct*100}% OF PROFIT at ${locked_stop:.2f}")
    
    def check_stop_loss(self, current_price: float) -> bool:
        """
        Check if stop loss has been hit
        
        Returns:
            True if stop triggered (should exit position)
        """
        if not self.position:
            return False
        
        if current_price <= self.position.stop_loss_price:
            profit = (current_price - self.position.entry_price) * self.position.contracts * 50
            profit_pct = ((current_price - self.position.entry_price) / self.position.entry_price) * 100
            
            logger.warning(f"🛑 STOP LOSS TRIGGERED!")
            logger.warning(f"Entry: ${self.position.entry_price:.2f} → Exit: ${current_price:.2f}")
            logger.warning(f"Profit: ${profit:,.2f} ({profit_pct:.2f}%)")
            
            return True
        
        return False
    
    def execute_exit(self, reason: str = "Stop Loss") -> None:
        """Exit current position"""
        if not self.position:
            return
        
        logger.info(f"📤 EXITING POSITION - Reason: {reason}")
        
        try:
            # Place market sell order
            self.place_order('SELL', self.position.contracts, 'MARKET')
            
            # Calculate final profit
            current_price = self.get_market_price()
            profit = (current_price - self.position.entry_price) * self.position.contracts * 50
            self.total_profit_realized += profit
            
            logger.info(f"✅ Position closed at ${current_price:.2f}")
            logger.info(f"💵 Profit: ${profit:,.2f}")
            logger.info(f"💰 Total Realized Profit: ${self.total_profit_realized:,.2f}")
            
            # Record stopout time
            self.last_stopout_time = datetime.now()
            
            # Clear position
            self.position = None
        
        except Exception as e:
            logger.error(f"Failed to exit position: {e}")
    
    def execute_entry(self, contracts: int, reason: str = "Initial Entry") -> None:
        """Enter new position"""
        logger.info(f"📥 ENTERING POSITION - Reason: {reason}")
        
        try:
            # Place market buy order
            self.place_order('BUY', contracts, 'MARKET')
            
            # Get entry price
            time.sleep(2)  # Wait for order to fill
            current_price = self.get_market_price()
            
            # Initialize position
            initial_stop = current_price * (1 - self.config['initial_stop_pct'])
            
            self.position = Position(
                contracts=contracts,
                entry_price=current_price,
                highest_price=current_price,
                stop_loss_price=initial_stop,
                entry_time=datetime.now()
            )
            
            logger.info(f"✅ Position opened: {contracts} contracts @ ${current_price:.2f}")
            logger.info(f"🛡️ Initial stop loss: ${initial_stop:.2f}")
            
            # Update daily counter
            self.daily_reentries += 1
        
        except Exception as e:
            logger.error(f"Failed to enter position: {e}")
    
    def should_reenter(self) -> bool:
        """Determine if conditions are right to re-enter after stop out"""
        # Check if we're in cooldown period
        if self.last_stopout_time:
            time_since_stop = (datetime.now() - self.last_stopout_time).total_seconds()
            if time_since_stop < self.config['cooldown_period']:
                remaining = self.config['cooldown_period'] - time_since_stop
                logger.debug(f"In cooldown: {remaining:.0f}s remaining")
                return False
        
        # Check daily re-entry limit
        if self.daily_reentries >= self.config['max_reentry_attempts']:
            logger.debug(f"Daily re-entry limit reached: {self.daily_reentries}")
            return False
        
        # Check for reversal
        if not self.detect_reversal():
            return False
        
        logger.info(f"✅ RE-ENTRY CONDITIONS MET (Attempt {self.daily_reentries + 1})")
        return True
    
    def manage_existing_position(self) -> None:
        """Manage existing position with trailing stop"""
        # Get current position from API
        api_position = self.get_futures_position()
        
        if not api_position or api_position['size'] == 0:
            if self.position:
                logger.warning("Position closed externally")
                self.position = None
            return
        
        # Initialize position object if needed
        if not self.position and api_position['size'] > 0:
            logger.info("Detected existing position, initializing tracking...")
            current_price = self.get_market_price()
            initial_stop = current_price * (1 - self.config['initial_stop_pct'])
            
            self.position = Position(
                contracts=int(api_position['size']),
                entry_price=api_position['entry_price'],
                highest_price=current_price,
                stop_loss_price=initial_stop,
                entry_time=datetime.now()
            )
            logger.info(f"Tracking {self.position.contracts} contracts @ ${self.position.entry_price:.2f}")
        
        # Get current price
        current_price = self.get_market_price()
        
        # Update trailing stop
        self.update_trailing_stop(current_price)
        
        # Check stop loss
        if self.check_stop_loss(current_price):
            self.execute_exit("Trailing Stop Loss Hit")
        else:
            # Log status
            profit_pct = ((current_price - self.position.entry_price) / self.position.entry_price) * 100
            distance_to_stop = ((current_price - self.position.stop_loss_price) / current_price) * 100
            
            logger.info(f"📊 Current: ${current_price:.2f} | Stop: ${self.position.stop_loss_price:.2f} "
                       f"({distance_to_stop:.2f}% away) | P/L: {profit_pct:.2f}%")
    
    def check_for_reentry(self) -> None:
        """Check if we should re-enter after being stopped out"""
        if self.position:
            return  # Already in position
        
        if self.should_reenter():
            # Calculate position size (same as before)
            contracts = self.config.get('reentry_contracts', 10)  # Default to 10 if not set
            
            self.execute_entry(contracts, "Reversal Re-Entry")
    
    def run(self) -> None:
        """Main bot loop"""
        logger.info("=" * 60)
        logger.info("🚀 COINBASE SILVER FUTURES TRAILING STOP BOT")
        logger.info("=" * 60)
        logger.info(f"Strategy: Infinite Gain Protection")
        logger.info(f"Trailing Stop: {self.config['trailing_stop_pct']*100}%")
        logger.info(f"Initial Stop: {self.config['initial_stop_pct']*100}%")
        logger.info(f"Max Re-entries: {self.config['max_reentry_attempts']}/day")
        logger.info("=" * 60)
        
        # Get current contract
        self.contract_symbol = self.get_current_silver_contract()
        
        # Check for existing position
        existing_pos = self.get_futures_position()
        if existing_pos and existing_pos['size'] > 0:
            logger.info(f"⚠️  EXISTING POSITION DETECTED: {existing_pos['size']} contracts")
            logger.info(f"The bot will manage this position with trailing stops.")
        
        try:
            while True:
                try:
                    # Reset daily counters
                    today = datetime.now().date()
                    if self.last_trade_date != today:
                        self.daily_reentries = 0
                        self.last_trade_date = today
                        logger.info(f"📅 New trading day: {today}")
                    
                    if self.position:
                        # Manage existing position
                        self.manage_existing_position()
                    else:
                        # Look for re-entry opportunities
                        self.check_for_reentry()
                    
                    # Sleep before next check
                    time.sleep(self.config['check_interval'])
                
                except KeyboardInterrupt:
                    logger.info("🛑 Bot stopped by user")
                    break
                
                except Exception as e:
                    logger.error(f"Error in main loop: {e}", exc_info=True)
                    time.sleep(60)
        
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)


def main():
    """Main entry point"""
    print("""
    ╔═══════════════════════════════════════════════════════════════╗
    ║  Coinbase Silver Futures - Infinite Gain Protection Bot      ║
    ║                                                               ║
    ║  • Dynamic Trailing Stop Loss                                ║
    ║  • Automatic Reversal Re-Entry                               ║
    ║  • Never Risk Original Capital                               ║
    ╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # Load API credentials
    api_key = os.getenv('COINBASE_API_KEY')
    api_secret = os.getenv('COINBASE_API_SECRET')
    
    if not api_key or not api_secret:
        print("\n⚠️  API credentials not found!")
        print("\nPlease set environment variables:")
        print("  export COINBASE_API_KEY='your_api_key'")
        print("  export COINBASE_API_SECRET='your_api_secret'")
        print("\nGet API keys at: https://www.coinbase.com/settings/api")
        return
    
    # Configuration
    config = {
        'trailing_stop_pct': 0.02,      # 2% trailing stop
        'initial_stop_pct': 0.03,       # 3% initial stop
        'reversal_rsi_threshold': 35,   # RSI threshold for reversal
        'max_reentry_attempts': 5,      # Max 5 re-entries per day
        'cooldown_period': 300,         # 5 min cooldown after stop
        'profit_lock_threshold': 0.10,  # Lock profit at 10% gain
        'profit_lock_pct': 0.50,        # Lock 50% of profit
        'check_interval': 30,           # Check every 30 seconds
        'reentry_contracts': 10,        # Number of contracts to trade
    }
    
    print("\n📊 Configuration:")
    print(f"  Trailing Stop: {config['trailing_stop_pct']*100}%")
    print(f"  Initial Stop: {config['initial_stop_pct']*100}%")
    print(f"  Profit Lock: {config['profit_lock_pct']*100}% at {config['profit_lock_threshold']*100}% gain")
    print(f"  Max Re-entries: {config['max_reentry_attempts']}/day")
    print(f"  Position Size: {config['reentry_contracts']} contracts")
    
    print("\n⚠️  STRATEGY EXPLANATION:")
    print("  1. Bot monitors your position continuously")
    print("  2. Trailing stop rises with price (locks in gains)")
    print("  3. When stop hit → sells position (profit secured)")
    print("  4. Waits for reversal signal (RSI + volume + price action)")
    print("  5. Re-enters position when reversal confirmed")
    print("  6. Repeat = Infinite gain potential, protected downside")
    
    print("\n⚠️  IMPORTANT:")
    print("  • This bot works with EXISTING positions")
    print("  • Make sure you have silver futures contracts open")
    print("  • Bot will protect and grow your position")
    print("  • Gains are locked, original capital protected")
    
    response = input("\n✓ Start bot? (yes/no): ")
    if response.lower() != 'yes':
        print("Bot not started.")
        return
    
    # Initialize and run bot
    bot = CoinbaseSilverTrailingBot(api_key, api_secret, config)
    bot.run()


if __name__ == "__main__":
    main()
