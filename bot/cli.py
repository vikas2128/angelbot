"""AngelBot command line.

  python -m bot.cli check                     # validate config, login, show IP/funds/positions
  python -m bot.cli run                       # trade today (mode from .env: paper|live)
  python -m bot.cli run --replay data/candles/SBIN-EQ_ONE_MINUTE.csv   # offline dry run
  python -m bot.cli fetch SBIN-EQ --days 60   # download 1-min candles to data/candles/
  python -m bot.cli backtest SBIN-EQ --from 2026-08-01 --to 2026-10-15 [--strategy orb --params '{"range_minutes":30}']
  python -m bot.cli instruments RELIANCE [--exchange NSE]
  python -m bot.cli screen [--top 3] [--universe SBIN-EQ,TCS-EQ] [--html out.html]   # rank stocks (advisory, read-only)
  python -m bot.cli status | pause | resume | kill | unkill
  python -m bot.cli sample-data SBIN-EQ       # synthetic candles for offline testing ONLY
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import random
import sys
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot import config as C
from bot import control
from bot.data import csv_path, fetch, load_csv, save_csv, to_bars
from bot.timeutil import IST, is_trading_day, now_ist


def setup_logging(level: str = "INFO") -> None:
    C.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    fh = RotatingFileHandler(control.LOG, maxBytes=5_000_000, backupCount=10)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("websocket", "urllib3", "logzero_default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _instruments(cfg, master_path=None):
    from bot.instruments import load_master, resolve
    return resolve(cfg["instruments"], load_master(master_path))


def _live_broker(cfg):
    from bot.broker import LiveBroker
    from bot.risk import OrderThrottle
    b = LiveBroker(C.credentials(), OrderThrottle(cfg["risk"]["max_orders_per_sec"]),
                   cfg["execution"].get("ordertag", "angelbot"))
    b.login()
    return b


# ---------------------------------------------------------------- commands
def cmd_check(args, cfg):
    print("✓ config valid")
    print(f"  mode: {C.trading_mode()}")
    insts = _instruments(cfg)
    for i in insts:
        print(f"  instrument: {i.exchange}:{i.symbol} token={i.token} lot={i.lot_size} tick={i.tick_size} qty={i.order_qty}")
    try:
        import requests
        print("  public IP of this machine:", requests.get("https://api.ipify.org", timeout=10).text,
              "(must match the static IP registered in SmartAPI)")
    except Exception as e:
        print("  could not determine public IP:", e)
    b = _live_broker(cfg)
    print("✓ SmartAPI login OK as", b.session.get("clientcode"))
    f = b.funds()
    print("  available cash:", f.get("availablecash"), " net:", f.get("net"))
    print("  open positions:", b.positions() or "none")
    for i in insts:
        print(f"  LTP {i.symbol}: {b.ltp(i)}")
    b.logout()


def _select_instruments(cfg, broker, insts, log):
    """Morning stock selection. Does nothing unless config `selection.mode` is auto or confirm."""
    if str((cfg.get("selection") or {}).get("mode", "off")).lower() == "off":
        return insts
    try:
        from bot import selection
        from bot.instruments import load_master
        chosen, decision = selection.run(cfg, broker, insts, load_master())
        log.info("selection: %s (%s)", decision.get("symbols"), decision.get("source"))
        return chosen
    except Exception:
        log.exception("selection crashed; using the configured list")
        return insts


def cmd_run(args, cfg):
    from bot.engine import Engine
    from bot.feed import LiveFeed, ReplayFeed
    from bot.journal import Journal
    from bot.paper import PaperBroker

    setup_logging(cfg.get("logging", {}).get("level", "INFO"))
    log = logging.getLogger("angelbot.cli")
    q: queue.Queue = queue.Queue()
    # replays get their own journal so they never mix with real paper/live history
    default_journal = control.RUNTIME_DIR / "replay_journal.db" if args.replay else control.JOURNAL
    journal = Journal(Path(args.journal) if args.journal else default_journal)
    slip = float(cfg.get("paper", {}).get("slippage_pct", 0.02))

    if args.replay:
        from bot import messages, notify
        notify.set_replay(True)
        notify.set_muted(True)                        # never one message per replayed trade
        insts = _instruments(cfg, Path(args.master) if args.master else None)
        if len(args.replay) != len(insts):
            sys.exit("--replay needs one CSV per configured instrument, in the same order")
        all_rows = {i.token: load_csv(Path(p)) for i, p in zip(insts, args.replay)}
        days = sorted({r[0][:10] for rows in all_rows.values() for r in rows})
        code, total, results = 0, 0.0, []
        for d in days:          # one Engine per session, exactly like production
            q = queue.Queue()
            candles = {tok: [r for r in rows if r[0][:10] == d] for tok, rows in all_rows.items()}
            eng = Engine(cfg, PaperBroker(slip), ReplayFeed(candles, q), insts, journal, q, replay=True)
            code = eng.run() or code
            total += eng.om.realized_net
            results.append({"day": d, "net": round(eng.om.realized_net, 2), "trades": eng.risk.trades_today,
                            "halted": eng.risk.halted_reason})
        print(json.dumps({"days": len(days), "net_pnl": round(total, 2), "per_day": results}, indent=2))
        if os.environ.get("ANGELBOT_PROFILE") == "research":   # one summary to the Test bot, and only from that area
            notify.set_muted(False)
            notify.send(messages.replay_summary(len(days), total, sum(r["trades"] for r in results),
                                                min((r["net"] for r in results), default=None)))
        return code

    today = now_ist()
    if not is_trading_day(today, cfg["session"].get("holidays", [])):
        log.info("not a trading day (%s) — exiting", today.date())
        return 0
    from bot.timeutil import at
    if today >= at(today, cfg["session"]["shutdown"]):
        log.info("after session shutdown time — nothing to do")
        return 0
    mode = C.trading_mode()
    insts = _instruments(cfg)
    data_broker = _live_broker(cfg)                  # needed for the feed in both modes
    insts = _select_instruments(cfg, data_broker, insts, log)
    if not insts:
        log.warning("stock selection decided: no trading today")
        data_broker.logout()
        return 0
    today = now_ist()                                # selection may have waited for the open: refresh before the warm-up
    broker = data_broker if mode == "live" else PaperBroker(slip)
    feed = LiveFeed(data_broker, insts, q)
    eng = Engine(cfg, broker, feed, insts, journal, q)
    if mode == "live":
        log.warning("=== LIVE MODE — REAL ORDERS ===")
        eng.reconcile()
    # warm up indicators if starting mid-session
    try:
        from bot.data import resample
        start = today.replace(hour=9, minute=15, second=0, microsecond=0)
        if today > start + timedelta(minutes=2):
            pre = {}
            for i in insts:
                rows = data_broker.candles(i, "ONE_MINUTE", start, today)
                bars = resample(to_bars(rows), int(cfg["strategy"].get("timeframe_min", 1)))
                pre[i.symbol] = bars[:-1]      # drop the still-forming bar
            eng.prefill(pre)
    except Exception as e:
        log.warning("prefill skipped: %s", e)
    code = eng.run()
    data_broker.logout()
    return code


def cmd_fetch(args, cfg):
    from bot.instruments import load_master, resolve
    inst = resolve([{"symbol": args.symbol, "exchange": args.exchange, "qty": 1}], load_master())[0]
    b = _live_broker(cfg)
    end = now_ist().replace(second=0, microsecond=0)
    start = (end - timedelta(days=args.days)).replace(hour=9, minute=15)
    rows = fetch(b, inst, start, end, args.interval)
    path = csv_path(inst.symbol, args.interval)
    save_csv(rows, path)
    print(f"saved {len(rows)} candles → {path}")
    b.logout()


def cmd_backtest(args, cfg):
    from bot.backtest import run_backtest
    from bot.instruments import load_master, resolve
    path = Path(args.csv) if args.csv else csv_path(args.symbol)
    if not path.exists():
        sys.exit(f"no data at {path}. Run: python -m bot.cli fetch {args.symbol} --days 60")
    bars = to_bars(load_csv(path))
    if args.date_from:
        bars = [b for b in bars if b.start.strftime("%Y-%m-%d") >= args.date_from]
    if args.date_to:
        bars = [b for b in bars if b.start.strftime("%Y-%m-%d") <= args.date_to]
    if args.timeframe:
        cfg["strategy"]["timeframe_min"] = args.timeframe
    qty = args.qty
    if qty is None:
        try:
            inst = resolve([{"symbol": args.symbol, "exchange": args.exchange,
                             "qty": next((i["qty"] for i in cfg["instruments"] if i["symbol"] == args.symbol), 1)}],
                           load_master(Path(args.master) if args.master else None))[0]
            qty = inst.order_qty
        except Exception:
            qty = 1
    params = json.loads(args.params) if args.params else None
    out = C.RUNTIME_DIR / "backtests" / f"{args.symbol}_{args.strategy or cfg['strategy']['name']}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    res = run_backtest(cfg, bars, qty=qty, strategy=args.strategy, params=params, out_csv=out)
    if not args.daily:
        res.pop("daily_pnl", None)
    print(json.dumps(res, indent=2))


def cmd_screen(args, cfg):
    """Rank a universe of stocks (advisory). Reads market data only, then writes runtime/screen.json."""
    from bot import screen_data
    from bot.instruments import load_master
    from bot.screen_report import render_html

    st = control.read_status()
    if st.get("bot_alive") and not st.get("replay") and not args.force:
        sys.exit("The bot is running right now. A second broker login during a session may disturb it. "
                 "Run this before 09:00 or after the session ends, or pass --force if you accept that risk.")
    universe = [x.strip() for x in args.universe.split(",") if x.strip()] if args.universe else None
    master = load_master()
    beside_bot = bool(st.get("bot_alive") and not st.get("replay"))
    broker = _live_broker(cfg)
    print("logged in; reading quotes and daily candles (one call a second, so about "
          f"{len(universe) if universe else 20} seconds or more)...")
    try:
        snap = screen_data.run_screen(cfg, broker, master, now_ist(), universe, args.top,
                                      progress=lambda i, n, sym: print(f"  [{i}/{n}] {sym}", flush=True))
    except ValueError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit("interrupted")
    finally:
        if not beside_bot:        # never end the broker session while the bot is running: it may share it
            broker.logout()
    control.write_screen(snap)
    print(f"screened {snap['universe']} stocks, {snap['eligible']} passed the gates  ({snap['market_date']})")
    for r in snap["rows"]:
        if r["excluded"]:
            print(f"  --  {r['symbol']:<14} left out: {'; '.join(r['excluded'])}")
        else:
            flag = "*" if r["symbol"] in snap["picks"] else " "
            print(f" {flag}{r['rank']:>2}  {r['symbol']:<14} score {r['score']:>5.1f}  gap {r['gap_pct'] if r['gap_pct'] is not None else '-':>5}  "
                  f"room {r['range_to_cost']}x  vol {r['vol_pct_of_avg']}%  | {'; '.join(r['why'])}")
    for e in snap["errors"]:
        print("  problem:", e)
    if args.html:
        Path(args.html).write_text(render_html(snap))
        print("report ->", args.html)
    print("(advisory only: the bot still trades just the instruments in config.yaml)")


def cmd_instruments(args, cfg):
    from bot.instruments import load_master, search
    for r in search(load_master(), args.query, args.exchange, args.limit):
        print(r)


def cmd_sample(args, cfg):
    """Synthetic random-walk 1-min candles (NOT real data) for offline testing."""
    rnd = random.Random(args.seed)
    px, rows = args.start_price, []
    day = now_ist().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=args.days * 2)
    made = 0
    while made < args.days:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        made += 1
        t = day.replace(hour=9, minute=15)
        drift = rnd.gauss(0, 0.0004)
        for _ in range(375):
            o = px
            c = max(1.0, o * (1 + drift + rnd.gauss(0, 0.0012)))
            h = max(o, c) * (1 + abs(rnd.gauss(0, 0.0005)))
            l = min(o, c) * (1 - abs(rnd.gauss(0, 0.0005)))
            rows.append([t.isoformat(), round(o, 2), round(h, 2), round(l, 2), round(c, 2), rnd.randint(1000, 20000)])
            px, t = c, t + timedelta(minutes=1)
    path = Path(args.out) if args.out else csv_path(args.symbol + "_SYNTHETIC")
    path.parent.mkdir(parents=True, exist_ok=True)
    save_csv(rows, path)
    print(f"wrote {len(rows)} synthetic candles → {path}  (random walk — not market data)")


def cmd_telegram_test(args, cfg):
    import os, requests
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first")
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "[AngelBot] ✅ Telegram alerts are working"}, timeout=10)
    print("sent ✓" if r.ok else f"failed: {r.status_code} {r.text}")


def _no_area_guard(cmd: str) -> None:
    """After the areas exist (runtime/paper or runtime/live), a bare pause/kill would write to the old runtime/ folder
    and silently not reach the running bot. Refuse and say how to address the right area."""
    if os.environ.get("ANGELBOT_PROFILE") or os.environ.get("ANGELBOT_RUNTIME"):
        return
    if any((C.ROOT / "runtime" / a).is_dir() for a in ("paper", "live")):
        sys.exit(f"No area given, so '{cmd}' would not reach the running bot. Use: ./deploy/ab paper {cmd}  (or live)")


def cmd_control(args, cfg):
    _no_area_guard(args.cmd)
    if args.cmd == "status":
        print(json.dumps(control.read_status(), indent=2, default=str))
        print("control:", control.read_control())
        return
    action = {"pause": dict(paused=True), "resume": dict(paused=False, kill=False),
              "kill": dict(kill=True), "unkill": dict(kill=False)}[args.cmd]
    print(control.set_control(**action, note=f"cli {args.cmd}"))


def main(argv=None):
    C.load_dotenv()
    ap = argparse.ArgumentParser(prog="angelbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check")
    r = sub.add_parser("run")
    r.add_argument("--replay", nargs="+", help="CSV(s) of 1-min candles to replay (paper, offline)")
    r.add_argument("--master", help="instrument master JSON (offline)")
    r.add_argument("--journal", help="journal db path (default runtime/journal.db)")

    f = sub.add_parser("fetch")
    f.add_argument("symbol"); f.add_argument("--exchange", default="NSE")
    f.add_argument("--days", type=int, default=30); f.add_argument("--interval", default="ONE_MINUTE")

    b = sub.add_parser("backtest")
    b.add_argument("symbol"); b.add_argument("--exchange", default="NSE")
    b.add_argument("--from", dest="date_from"); b.add_argument("--to", dest="date_to")
    b.add_argument("--strategy"); b.add_argument("--params", help="JSON dict of strategy params")
    b.add_argument("--timeframe", type=int); b.add_argument("--qty", type=int)
    b.add_argument("--csv", help="explicit candle CSV path"); b.add_argument("--master")
    b.add_argument("--daily", action="store_true", help="include per-day P&L")

    i = sub.add_parser("instruments")
    i.add_argument("query"); i.add_argument("--exchange"); i.add_argument("--limit", type=int, default=25)

    sc = sub.add_parser("screen")
    sc.add_argument("--top", type=int); sc.add_argument("--universe", help="comma-separated symbols, e.g. SBIN-EQ,TCS-EQ")
    sc.add_argument("--html", help="also write the standalone HTML report here")
    sc.add_argument("--force", action="store_true", help="run even while the bot is running (second login!)")

    s = sub.add_parser("sample-data")
    s.add_argument("symbol"); s.add_argument("--days", type=int, default=20)
    s.add_argument("--start-price", type=float, default=800.0); s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out")

    sub.add_parser("telegram-test")
    for c in ("status", "pause", "resume", "kill", "unkill"):
        sub.add_parser(c)

    args = ap.parse_args(argv)
    if args.cmd in ("run", "screen", "status") and not os.environ.get("ANGELBOT_PROFILE") and not args.__dict__.get("replay"):
        print("note: ANGELBOT_PROFILE is not set, so this uses the legacy runtime/ folder. "
              "Use ./deploy/ab <paper|live|research> ... to pick an area.", file=sys.stderr)
    cfg = C.load_config(Path(args.config) if args.config else None)
    handlers = {"check": cmd_check, "run": cmd_run, "fetch": cmd_fetch, "backtest": cmd_backtest,
                "instruments": cmd_instruments, "sample-data": cmd_sample, "screen": cmd_screen,
                "telegram-test": cmd_telegram_test}
    rc = handlers.get(args.cmd, cmd_control)(args, cfg)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
