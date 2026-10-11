"""Independent, local-only Dual MA paper accounting. No brokerage adapters."""
from .wallet import Wallet, WalletError

__all__ = ['Wallet', 'WalletError']
