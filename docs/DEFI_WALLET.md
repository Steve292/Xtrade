# DeFi wallet + Hyperliquid perps (testnet)

Long/short **BTC, ETH, SOL, HYPE, gold (PAXG), XMR, and 200+ memecoins** on one
venue — Hyperliquid — with a self-custody wallet as the deposit method. Runs on
**testnet** by default, so nothing touches real money.

## How it fits together

```
  wallet_testnet.json        hyperwallet.py            Hyperliquid testnet
  (your EVM keypair)   ──▶   client + SDK       ──▶   api.hyperliquid-testnet.xyz
   the deposit address        long / short / close      209 perp markets
```

The "wallet" is an EVM keypair (like MetaMask). Its **address** is where you
deposit; its **private key** signs your trades. On testnet the deposit is free
faucet USDC.

## Quick start

```bash
source venv/bin/activate
pip install hyperliquid-python-sdk        # already in requirements.txt

# 1) Create your deposit wallet (writes a gitignored wallet_testnet.json)
python hyperwallet.py create

# 2) Fund it — paste the printed address into the faucet
#    https://app.hyperliquid-testnet.xyz/drip
python hyperwallet.py balance             # confirm the USDC arrived

# 3) Browse markets (live prices, no wallet needed)
python hyperwallet.py markets             # majors
python hyperwallet.py markets --group memes
python hyperwallet.py markets --group all # all 200+

# 4) Trade — long/short any listed perp, close when done
python hyperwallet.py long  BTC 50 --lev 5
python hyperwallet.py short XMR 25
python hyperwallet.py long  WIF 20        # a memecoin
python hyperwallet.py positions
python hyperwallet.py close BTC
```

## Notes

- **Amounts are in USD.** `long BTC 50` opens $50 of BTC exposure; the client
  converts to coin size using the live mid and the asset's size precision.
- **Leverage** defaults to `hyperliquid.default_leverage` in `config.yaml`
  (3x); override per trade with `--lev`. Each asset has a max (BTC 40x, XMR 5x…).
- **Gold** is `PAXG` (tokenized gold). **XMR** is a listed perp — you get
  long/short price exposure without holding anything on the Monero chain.
- **Assets** to show in `markets` are configured under `hyperliquid.majors` /
  `hyperliquid.memecoins` in `config.yaml`; `--group all` ignores the lists and
  shows everything the venue lists.

## Security

- `wallet_testnet.json` holds a plaintext private key and is **gitignored** —
  never commit it. Fine for testnet fake funds.
- Going to **mainnet real money** is a different game: use a hardware wallet or
  an encrypted keystore, never a plaintext key on disk, and get any custody /
  order-routing code independently reviewed first. This tool is built and
  verified on testnet only.
```
