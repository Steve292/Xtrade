"""
Multi-chain balance scanner for funding the Hyperliquid deposit wallet
(`bot/wallet.py`'s `DefiWallet`).

Checks an address's native + stablecoin balance across several EVM chains,
and (given a Tron address) TRC20 USDT on Tron -- all read-only, no
transactions, nothing at risk -- then reports the shortest path to get
funds into Hyperliquid from wherever they currently sit.

This deliberately doesn't execute the actual cross-chain bridge or swap --
Hyperliquid's only canonical deposit door is native USDC on Arbitrum via
the Bridge2 contract, and audited aggregators (Across, Jumper, LI.FI)
already do the EVM-to-EVM hop well. Tron isn't EVM-compatible and none of
those aggregators reach it, so TRC20 USDT needs a different path -- see
`scan_and_report`'s guidance.

SECURITY NOTE: verify the Bridge2 and token contract addresses below
yourself (BscScan / TronScan / hyperliquid.gitbook.io) before trusting them
with anything beyond a test amount. Clipboard hijacking malware that swaps
a copied address for an attacker's is a known, active attack on exactly
this deposit flow -- match every character.

  Arbitrum One Bridge2 (Hyperliquid's deposit contract):
      0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7
  Arbitrum native USDC:
      0xaf88d065e77c8cC2239327C5EDb3A432268e5831
  Minimum deposit: $5 USDC. No Hyperliquid-side fee -- just Arbitrum gas
  (typically $0.10-0.30).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests
from web3 import Web3

from bot.evm.chains import ERC20_ABI

# Public RPCs -- fine for read-only balance checks. Swap in your own
# Alchemy/Infura endpoint via these env vars if a public one is flaky.
CHAINS = {
    "ethereum": os.getenv("RPC_ETHEREUM", "https://eth.llamarpc.com"),
    "arbitrum": os.getenv("RPC_ARBITRUM", "https://arb1.arbitrum.io/rpc"),
    "base":     os.getenv("RPC_BASE", "https://mainnet.base.org"),
    "optimism": os.getenv("RPC_OPTIMISM", "https://mainnet.optimism.io"),
    "polygon":  os.getenv("RPC_POLYGON", "https://polygon-rpc.com"),
    "bsc":      os.getenv("RPC_BSC", "https://bsc-dataseed.binance.org"),
}

NATIVE_SYMBOLS = {
    "ethereum": "ETH", "arbitrum": "ETH", "base": "ETH",
    "optimism": "ETH", "polygon": "POL", "bsc": "BNB",
}

# The stablecoin worth checking per chain: native USDC everywhere except
# BSC, where BEP20 USDT (Binance-Peg BSC-USD) is the common asset landing
# from centralized-exchange withdrawals.
STABLECOINS = {
    "ethereum": ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    "arbitrum": ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    "base":     ("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
    "optimism": ("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"),
    "polygon":  ("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
    "bsc":      ("USDT", "0x55d398326f99059fF775485246999027B3197955"),  # BEP20 USDT, 18 decimals -- read on-chain, don't assume 6
}

# Official Tether USDT contract on Tron (TRC20), 6 decimals.
TRON_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@dataclass
class ChainBalance:
    chain: str
    native_symbol: str
    native_balance: float
    token_symbol: str
    token_balance: float
    error: str = ""


def check_balance(chain_name: str, address: str) -> ChainBalance:
    rpc = CHAINS[chain_name]
    native_symbol = NATIVE_SYMBOLS[chain_name]
    token_symbol, token_address = STABLECOINS[chain_name]
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
    checksum = Web3.to_checksum_address(address)

    try:
        native_wei = w3.eth.get_balance(checksum)
        native = float(w3.from_wei(native_wei, "ether"))
    except Exception as e:
        return ChainBalance(chain_name, native_symbol, 0.0, token_symbol, 0.0, error=f"RPC error: {e}")

    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
        raw = contract.functions.balanceOf(checksum).call()
        decimals = contract.functions.decimals().call()  # don't assume 6 -- e.g. BEP20 USDT is 18
        token_balance = raw / (10 ** decimals)
    except Exception as e:
        return ChainBalance(chain_name, native_symbol, native, token_symbol, 0.0, error=f"{token_symbol} read error: {e}")

    return ChainBalance(chain_name, native_symbol, native, token_symbol, token_balance)


def check_tron_balance(address: str) -> ChainBalance:
    """Read-only TRX + TRC20 USDT balance via TronGrid's public REST API (no SDK needed)."""
    try:
        resp = requests.get(f"https://api.trongrid.io/v1/accounts/{address}", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return ChainBalance("tron", "TRX", 0.0, "USDT", 0.0, error="address not found on-chain yet (no activity)")
        account = data[0]
        trx = account.get("balance", 0) / 1_000_000
        usdt_raw = 0
        for entry in account.get("trc20", []):
            if TRON_USDT_CONTRACT in entry:
                usdt_raw = int(entry[TRON_USDT_CONTRACT])
                break
        return ChainBalance("tron", "TRX", trx, "USDT", usdt_raw / 1_000_000)
    except Exception as e:
        return ChainBalance("tron", "TRX", 0.0, "USDT", 0.0, error=f"TronGrid error: {e}")


def scan_all_chains(address: str) -> list[ChainBalance]:
    return [check_balance(chain, address) for chain in CHAINS]


def scan_and_report(address: str, tron_address: str = "") -> str:
    """Scan `address` across all EVM chains (plus Tron, if `tron_address` is
    given) and return a formatted report with funding guidance."""
    lines = [f"Scanning {address} across {len(CHAINS)} EVM chains...", ""]
    balances = scan_all_chains(address)
    for b in balances:
        if b.error:
            lines.append(f"  {b.chain:10s}  (skipped: {b.error})")
        else:
            lines.append(f"  {b.chain:10s}  {b.native_symbol}={b.native_balance:.5f}   {b.token_symbol}={b.token_balance:,.2f}")

    tron: ChainBalance | None = None
    if tron_address:
        lines.append(f"\nScanning {tron_address} on Tron (TRC20)...\n")
        tron = check_tron_balance(tron_address)
        if tron.error:
            lines.append(f"  tron        (skipped: {tron.error})")
        else:
            lines.append(f"  tron        TRX={tron.native_balance:.5f}   USDT={tron.token_balance:,.2f}")
    else:
        lines.append("\n(Pass --tron / set TRON_ACCOUNT_ADDRESS to also scan TRC20 USDT -- Tron addresses use a")
        lines.append(" different format than your EVM wallet, so it can't reuse the same address.)")

    arb = next((b for b in balances if b.chain == "arbitrum"), None)
    direct = arb is not None and not arb.error and arb.token_balance >= 5
    other_evm = [b for b in balances if b.chain != "arbitrum" and not b.error and b.token_balance > 0]
    has_tron_usdt = tron is not None and not tron.error and tron.token_balance > 0

    lines.append("")
    if direct:
        lines.append(f"${arb.token_balance:,.2f} USDC already on Arbitrum -- this is the direct path.")
        lines.append("Go to https://app.hyperliquid.xyz, click Deposit, confirm the Bridge2 address")
        lines.append("matches the one in this module's docstring, and send it from there.")

    if other_evm:
        chains_list = ", ".join(f"{b.chain} (${b.token_balance:,.2f} {b.token_symbol})" for b in other_evm)
        lines.append(f"{'Also f' if direct else 'F'}ound stablecoin on: {chains_list}")
        lines.append("These are EVM chains, so a swap+bridge aggregator can convert straight to native")
        lines.append("USDC on Arbitrum in one go (BSC's BEP20 USDT included):")
        lines.append("  https://app.across.to/bridge    (fast, often cheapest for L2-to-L2)")
        lines.append("  https://jumper.exchange          (widest chain coverage, handles cross-asset swaps)")
        lines.append("Pick HyperCore as the destination if the aggregator asks, not HyperEVM. Confirm the")
        lines.append("aggregator actually supports your source chain/asset pair before sending real funds.")

    if has_tron_usdt:
        lines.append(f"\n${tron.token_balance:,.2f} USDT found on Tron (TRC20).")
        lines.append("Tron isn't EVM-compatible, so the bridge aggregators above can't reach it directly.")
        lines.append("Practical path: deposit it to a centralized exchange that accepts TRC20 USDT (e.g.")
        lines.append("Binance, OKX), then withdraw USDC choosing the Arbitrum network directly from that")
        lines.append("exchange -- most major exchanges support picking the destination network/asset on")
        lines.append("withdrawal. Land that USDC in your EVM wallet, then deposit into Hyperliquid as usual.")

    if not direct and not other_evm and not has_tron_usdt:
        lines.append("No USDC/USDT found on any scanned chain yet. Fund the wallet first, or check a chain")
        lines.append("not in this list by adding it to the CHAINS dict.")

    return "\n".join(lines)
