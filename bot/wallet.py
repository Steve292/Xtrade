"""
DeFi wallet — the deposit method for the Hyperliquid venue.

A wallet here is just an EVM keypair (same kind MetaMask uses). Its address is
where you deposit funds; its private key signs your trades. On testnet the funds
come from the Hyperliquid faucet, so the key is throwaway.

Security posture:
- This is built for TESTNET. The generated key is stored in a local, gitignored
  file in plaintext, which is fine for fake funds but NOT acceptable for real
  money. For mainnet, use a hardware wallet or an encrypted keystore and never
  let a key touch disk in plaintext.
- The private key is only ever printed by `create`, once, so you can save it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

FAUCET_URL = "https://app.hyperliquid-testnet.xyz/drip"
DEFAULT_WALLET_FILE = "wallet_testnet.json"


@dataclass
class DefiWallet:
    address: str
    private_key: str
    testnet: bool = True

    @classmethod
    def create(cls, testnet: bool = True) -> "DefiWallet":
        from eth_account import Account

        acct = Account.create()
        return cls(address=acct.address, private_key=acct.key.hex(), testnet=testnet)

    @classmethod
    def from_key(cls, private_key: str, testnet: bool = True) -> "DefiWallet":
        from eth_account import Account

        acct = Account.from_key(private_key)
        return cls(address=acct.address, private_key=private_key, testnet=testnet)

    @classmethod
    def from_env(cls, env_var: str = "HL_PRIVATE_KEY", testnet: bool = True) -> "DefiWallet | None":
        key = os.getenv(env_var, "")
        return cls.from_key(key, testnet=testnet) if key else None

    @classmethod
    def load(cls, path: str = DEFAULT_WALLET_FILE) -> "DefiWallet | None":
        p = Path(path)
        if not p.exists():
            return None
        data = json.loads(p.read_text())
        return cls(
            address=data["address"],
            private_key=data["private_key"],
            testnet=data.get("testnet", True),
        )

    def save(self, path: str = DEFAULT_WALLET_FILE) -> str:
        payload = {
            "address": self.address,
            "private_key": self.private_key,
            "testnet": self.testnet,
            "warning": "TESTNET wallet — do NOT fund with real money or reuse on mainnet.",
        }
        Path(path).write_text(json.dumps(payload, indent=2))
        return path

    @property
    def faucet_url(self) -> str:
        return FAUCET_URL
