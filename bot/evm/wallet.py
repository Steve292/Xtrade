from __future__ import annotations

import os
from dataclasses import dataclass

from web3 import Web3

from .chains import CHAINS, ERC20_ABI


@dataclass
class WalletBalance:
    eth: float
    usdc: float
    address: str


class EVMWallet:
    """EVM wallet for on-chain DEX trading."""

    def __init__(
        self,
        chain: str = "base",
        private_key: str = "",
        rpc_url: str = "",
        mode: str = "paper",
    ):
        self.chain_name = chain
        self.chain = CHAINS[chain]
        self.mode = mode
        self.rpc_url = rpc_url or self.chain["rpc"]
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))

        if private_key and mode == "live":
            self.account = self.w3.eth.account.from_key(private_key)
            self.address = self.account.address
        else:
            self.account = None
            # Deterministic paper address
            self.address = "0x" + "0" * 40
            self._paper_eth = 1.0
            self._paper_usdc = 10000.0

    @classmethod
    def from_env(cls) -> "EVMWallet":
        return cls(
            chain=os.getenv("EVM_CHAIN", "base"),
            private_key=os.getenv("EVM_PRIVATE_KEY", ""),
            rpc_url=os.getenv("EVM_RPC_URL", ""),
            mode=os.getenv("MODE", "paper"),
        )

    @property
    def is_connected(self) -> bool:
        try:
            return self.w3.is_connected()
        except Exception:
            return False

    def get_balance(self) -> WalletBalance:
        if self.mode == "paper":
            return WalletBalance(
                eth=self._paper_eth,
                usdc=self._paper_usdc,
                address=self.address,
            )

        eth_wei = self.w3.eth.get_balance(self.address)
        usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.chain["usdc"]), abi=ERC20_ABI
        )
        usdc_raw = usdc.functions.balanceOf(self.address).call()
        decimals = usdc.functions.decimals().call()

        return WalletBalance(
            eth=float(self.w3.from_wei(eth_wei, "ether")),
            usdc=usdc_raw / (10**decimals),
            address=self.address,
        )

    def paper_swap_eth_to_usdc(self, eth_amount: float, price: float) -> float:
        """Simulate buying USDC with ETH at given price."""
        eth_amount = min(eth_amount, self._paper_eth)
        usdc_out = eth_amount * price
        self._paper_eth -= eth_amount
        self._paper_usdc += usdc_out
        return usdc_out

    def paper_swap_usdc_to_eth(self, usdc_amount: float, price: float) -> float:
        """Simulate selling USDC for ETH at given price."""
        usdc_amount = min(usdc_amount, self._paper_usdc)
        eth_out = usdc_amount / price
        self._paper_usdc -= usdc_amount
        self._paper_eth += eth_out
        return eth_out

    def get_eth_price_usd(self) -> float:
        """Fetch ETH/USD price via Chainlink-style fallback or RPC estimate."""
        if self.mode == "paper":
            return 3500.0  # fallback for paper
        try:
            import ccxt

            ticker = ccxt.binance().fetch_ticker("ETH/USDT")
            return float(ticker["last"])
        except Exception:
            return 3500.0
