"""Explicit manual commands only. No background process is started."""
import argparse
import json
from .wallet import Wallet, WalletError


def main():
    parser = argparse.ArgumentParser(description='Local PAPER wallet')
    parser.add_argument('--db', default='data/dual_ma_ai/wallet.sqlite3')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'status', 'history'):
        commands.add_parser(name)
    for name in ('deposit', 'withdraw'):
        cmd = commands.add_parser(name)
        cmd.add_argument('amount')
        cmd.add_argument('--key', required=True)
    for name in ('buy', 'sell'):
        cmd = commands.add_parser(name)
        cmd.add_argument('ticker')
        cmd.add_argument('quantity', type=int)
        cmd.add_argument('price')
        cmd.add_argument('--fee', default='0.00')
        cmd.add_argument('--key', required=True)
    cmd = commands.add_parser('mark')
    cmd.add_argument('ticker')
    cmd.add_argument('price')
    cmd.add_argument('--key', required=True)
    args = parser.parse_args()
    try:
        wallet = Wallet(args.db)
        if args.command in ('init', 'status'):
            result = wallet.snapshot()
        elif args.command == 'history':
            result = wallet.history()
        elif args.command in ('deposit', 'withdraw'):
            result = getattr(wallet, args.command)(args.amount, args.key)
        elif args.command == 'mark':
            result = wallet.mark(args.ticker, args.price, args.key)
        else:
            result = getattr(wallet, args.command)(args.ticker, args.quantity, args.price, args.key, args.fee)
        print(json.dumps(result, indent=2))
    except WalletError as exc:
        parser.exit(2, f'Wallet request rejected: {exc}\n')


if __name__ == '__main__':
    main()
