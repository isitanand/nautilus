import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from nautilus_trader.adapters.bybit import (
    BybitAccountType,
    BybitDataClientConfig,
    BybitEnvironment,
    BybitExecClientConfig,
    BybitProductType,
)
from nautilus_trader.config import (
    InstrumentProviderConfig,
    LoggingConfig,
    RiskEngineConfig,
    TradingNodeConfig,
)
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import TraderId

from config import BtcAtmStraddleConfig
from strategy import BtcAtmShortStraddle


def parse_args():
    parser = argparse.ArgumentParser(description="Run BTC ATM Straddle Strategy on Bybit")
    parser.add_argument(
        "--level",
        type=int,
        default=2,
        choices=[1, 2],
        help="Strategy level: 1 (Naked Straddle) or 2 (Straddle with 1% SL / 2% TP)",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        default=True,
        help="Use Bybit Testnet",
    )
    parser.add_argument(
        "--trade-size",
        type=str,
        default="0.01",
        help="Order quantity for each option leg (default: 0.01 BTC)",
    )
    parser.add_argument(
        "--underlying",
        type=str,
        default="BTCUSDT-LINEAR.BYBIT",
        help="Underlying reference instrument ID",
    )
    parser.add_argument(
        "--expiry",
        type=str,
        default=None,
        help="Optional specific target expiry (e.g., '28MAR25')",
    )
    return parser.parse_args()


def main():
    env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=env_path)

    args = parse_args()

    api_key = os.getenv("BYBIT_API_KEY", "")
    api_secret = os.getenv("BYBIT_API_SECRET", "")

    environment = BybitEnvironment.TESTNET if args.testnet else BybitEnvironment.MAINNET

    print("\n" + "=" * 60)
    print("Memplus Technologies - BTC Options Straddle Execution Engine")
    print(f"Environment:  {environment.name}")
    print(f"Strategy:     Level {args.level} ({'Naked Straddle' if args.level == 1 else 'Managed 1% SL / 2% TP'})")
    print(f"Trade Size:   {args.trade_size} BTC per leg")
    print(f"Underlying:   {args.underlying}")
    print("=" * 60 + "\n")

    data_client_config = BybitDataClientConfig(
        environment=environment,
        product_types=[
            BybitProductType.OPTION,
            BybitProductType.LINEAR,
        ],
        api_key=api_key if api_key else None,
        api_secret=api_secret if api_secret else None,
        instrument_provider=InstrumentProviderConfig(
            load_all=True,
        ),
    )

    exec_client_config = BybitExecClientConfig(
        environment=environment,
        product_types=[
            BybitProductType.OPTION,
        ],
        account_type=BybitAccountType.UNIFIED,
        api_key=api_key,
        api_secret=api_secret,
        max_retries=3,
        retry_delay=1.0,
    )

    strategy_config = BtcAtmStraddleConfig(
        underlying_instrument_id=args.underlying,
        target_expiry=args.expiry,
        trade_size=Decimal(args.trade_size),
        level=args.level,
        stop_loss_pct=Decimal("0.01"),
        take_profit_pct=Decimal("0.02"),
    )

    node_config = TradingNodeConfig(
        trader_id=TraderId("MEMPLUS-STRADDLE-01"),
        logging=LoggingConfig(log_level="INFO"),
        risk_engine=RiskEngineConfig(bypass=False),
        data_clients={"BYBIT": data_client_config},
        exec_clients={"BYBIT": exec_client_config},
    )

    node = TradingNode(config=node_config)

    strategy = BtcAtmShortStraddle(config=strategy_config)
    node.trader.add_strategy(strategy)

    try:
        node.build()
        print("Trading node built successfully. Connecting to Bybit WebSocket & REST feeds...")
        node.start()
        print("Strategy running. Press Ctrl+C to terminate.")
        node.run()
    except KeyboardInterrupt:
        print("\nShutdown signal received. Stopping strategy and node...")
    finally:
        node.stop()
        node.dispose()
        print("Node cleanly stopped and disposed.")


if __name__ == "__main__":
    main()
