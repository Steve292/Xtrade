from __future__ import annotations

import time
from dataclasses import dataclass

from web3 import Web3

from .chains import CHAINS, ROUTER_ABI
from .wallet import EVMWallet


@dataclass
class EVMPosition:
    side: str  # "long" | "short"
    entry: float
    size_eth: float
    stop_loss: float
    take_profit: float
    reason: str


class EVMDex:
    """
    On-chain DEX interface for SMC signals.
    Long = swap USDC → ETH (buy ETH)
    Short = swap ETH → USDC (sell ETH)
    """

    def __init__(self, wallet: EVMWallet, slippage_pct: float = 0.5):
        self.wallet = wallet
        self.slippage_pct = slippage_pct
        self.position: EVMPosition | None = None
        self.trade_log: list[dict] = []
        self.chain = wallet.chain

        self.router = wallet.w3.eth.contract(
            address=Web3.to_checksum_address(self.chain["router"]),
            abi=ROUTER_ABI,
        )

    def fetch_price(self) -> float:
        return self.wallet.get_eth_price_usd()

    def open_position(
        self, side: str, entry: float, size_usd: float, sl: float, tp: float, reason: str
    ) -> None:
        if self.position is not None:
            return

        price = self.fetch_price()
        size_eth = size_usd / price

        if self.wallet.mode == "paper":
            if side == "long":
                self.wallet.paper_swap_usdc_to_eth(size_usd, price)
            else:
                self.wallet.paper_swap_eth_to_usdc(size_eth, price)

            self.position = EVMPosition(side, entry, size_eth, sl, tp, reason)
            self.trade_log.append(
                {"action": "open", "side": side, "entry": entry, "size_eth": size_eth, "reason": reason}
            )
            print(f"[EVM PAPER] OPEN {side.upper()} {size_eth:.6f} ETH @ ${entry:.2f}")
            print(f"            SL=${sl:.2f} TP=${tp:.2f} | {reason}")
        else:
            self._execute_swap(side, size_usd, size_eth)
            self.position = EVMPosition(side, entry, size_eth, sl, tp, reason)

    def _execute_swap(self, side: str, size_usd: float, size_eth: float) -> None:
        w3 = self.wallet.w3
        account = self.wallet.account
        if account is None:
            raise RuntimeError("Live EVM trading requires EVM_PRIVATE_KEY")

        deadline = int(time.time()) + 300
        weth = Web3.to_checksum_address(self.chain["weth"])
        usdc = Web3.to_checksum_address(self.chain["usdc"])
        path = [usdc, weth] if side == "long" else [weth, usdc]

        if side == "long":
            amount_in = int(size_usd * 1e6)  # USDC 6 decimals
            amounts = self.router.functions.getAmountsOut(amount_in, path).call()
            min_out = int(amounts[-1] * (1 - self.slippage_pct / 100))
            tx = self.router.functions.swapExactTokensForETH(
                amount_in, min_out, path, account.address, deadline
            ).build_transaction(
                {
                    "from": account.address,
                    "nonce": w3.eth.get_transaction_count(account.address),
                    "gas": 300000,
                    "gasPrice": w3.eth.gas_price,
                }
            )
        else:
            amount_in = w3.to_wei(size_eth, "ether")
            amounts = self.router.functions.getAmountsOut(amount_in, path).call()
            min_out = int(amounts[-1] * (1 - self.slippage_pct / 100))
            tx = self.router.functions.swapExactETHForTokens(
                min_out, path, account.address, deadline
            ).build_transaction(
                {
                    "from": account.address,
                    "value": amount_in,
                    "nonce": w3.eth.get_transaction_count(account.address),
                    "gas": 300000,
                    "gasPrice": w3.eth.gas_price,
                }
            )

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"[EVM LIVE] Swap tx: {self.chain['explorer']}/tx/{tx_hash.hex()}")

    def check_exit(self, current_price: float) -> bool:
        if self.position is None:
            return False

        pos = self.position
        hit_sl = hit_tp = False

        if pos.side == "long":
            hit_sl = current_price <= pos.stop_loss
            hit_tp = current_price >= pos.take_profit
        else:
            hit_sl = current_price >= pos.stop_loss
            hit_tp = current_price <= pos.take_profit

        if hit_sl or hit_tp:
            exit_price = current_price
            pnl_usd = self._calc_pnl(exit_price)
            outcome = "TP" if hit_tp else "SL"

            if self.wallet.mode == "paper":
                if pos.side == "long":
                    self.wallet.paper_swap_eth_to_usdc(pos.size_eth, exit_price)
                else:
                    self.wallet.paper_swap_usdc_to_eth(pos.size_eth * exit_price, exit_price)

            self.trade_log.append(
                {"action": "close", "side": pos.side, "exit": exit_price, "pnl_usd": pnl_usd, "outcome": outcome}
            )
            print(f"[EVM PAPER] CLOSE {pos.side.upper()} @ ${exit_price:.2f} | {outcome} | PnL=${pnl_usd:+.2f}")
            self.position = None
            return True

        return False

    def _calc_pnl(self, exit_price: float) -> float:
        pos = self.position
        if pos is None:
            return 0.0
        if pos.side == "long":
            return (exit_price - pos.entry) * pos.size_eth
        return (pos.entry - exit_price) * pos.size_eth

    def get_usdc_balance(self) -> float:
        return self.wallet.get_balance().usdc
