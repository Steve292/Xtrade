#!/usr/bin/env python3
"""
Derive an Ethereum private key from a BIP-39 seed phrase — run this YOURSELF,
in your own terminal. The seed phrase and the derived private key never leave
this process: nothing is printed except the derived public address (safe to
share) and a final confirmation. The private key is written straight into
.env's HL_PRIVATE_KEY= line, so you never have to copy-paste it either.

Uses the standard Ethereum derivation path (m/44'/60'/0'/0/0) — the one
MetaMask, Uniswap Wallet, Trust Wallet, Rainbow, etc. all use for the first
account from a seed phrase.
"""
import getpass
import sys
from pathlib import Path

from eth_account import Account

Account.enable_unaudited_hdwallet_features()

EXPECTED_ADDRESS = "0xBBac1698fB53806958E62375D8cB2c1a6fc577f1"
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def main() -> None:
    phrase = getpass.getpass("Enter your 12-word seed phrase (hidden, not echoed): ").strip()
    if not phrase:
        sys.exit("No phrase entered — aborting.")

    acct = Account.from_mnemonic(phrase, account_path="m/44'/60'/0'/0/0")
    del phrase  # drop the phrase from memory as soon as we're done with it

    print(f"\nDerived address: {acct.address}")
    print(f"Expected wallet: {EXPECTED_ADDRESS}")
    if acct.address.lower() == EXPECTED_ADDRESS.lower():
        print("MATCH ✓")
    else:
        print("DOES NOT MATCH — this seed phrase derives a different wallet.")
        proceed = input("Write it to .env anyway? (y/N): ").strip().lower()
        if proceed != "y":
            print("Aborted — nothing written.")
            return

    confirm = input("\nWrite the derived private key to .env as HL_PRIVATE_KEY? (y/N): ").strip().lower()
    if confirm != "y":
        print("Aborted — nothing written.")
        return

    raw_hex = acct.key.hex()
    raw_hex = raw_hex if raw_hex.startswith("0x") else f"0x{raw_hex}"
    key_line = f"HL_PRIVATE_KEY={raw_hex}\n"
    lines = ENV_PATH.read_text().splitlines(keepends=True) if ENV_PATH.exists() else []
    for i, line in enumerate(lines):
        if line.startswith("HL_PRIVATE_KEY="):
            lines[i] = key_line
            break
    else:
        lines.append(key_line)
    ENV_PATH.write_text("".join(lines))

    print(f"\nSaved to {ENV_PATH} — the private key itself was never printed.")
    print("Go back and ask Claude to verify it.")


if __name__ == "__main__":
    main()
