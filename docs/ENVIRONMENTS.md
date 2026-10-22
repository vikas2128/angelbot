# Areas: paper, live, research and test

Everything the bot records used to land in one `runtime/` folder, with one Telegram bot. Now each area has its own
files, services and (where it makes sense) Telegram bot, so a command, a tap or a report in one area can never touch another.

| Area | What it is | Files | Mode | Telegram bot | Broker login | How it runs |
|---|---|---|---|---|---|---|
| **paper** | real market prices, simulated orders | `runtime/paper/` | pinned `paper` | Paper bot | yes | `angelbot@paper` (weekday timer) + `angelbot-telegram@paper` |
| **live** | real money. **Prepared, OFF** | `runtime/live/` | you set it | Live bot | yes | `angelbot@live`: installed, never enabled or started by the setup |
| **research** | replays, backtests, sample data | `runtime/research/` | paper, no broker | Test bot | no | by hand: `./deploy/ab research ...` |
| **test** | `pytest` | a temp folder | paper | Test bot (optional) | no | `pytest` |

Market candles in `data/` are shared (market data, not trading records).

## How an area is selected
Each area has a file in `profiles/` (real files are git-ignored and secret; the `*.env.example` templates are committed).
A profile sets `ANGELBOT_PROFILE`, `ANGELBOT_MODE`, `ANGELBOT_RUNTIME`, `ANGELBOT_CONFIG`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
Broker credentials stay in the shared `.env`. systemd loads the profile with `EnvironmentFile=`; for manual commands use the wrapper:

```bash
./deploy/ab paper status          # same as: python -m bot.cli status, inside the paper area
./deploy/ab paper screen
./deploy/ab research run --replay data/candles/SBIN-EQ_ONE_MINUTE.csv
```

- `paper.env` pins `ANGELBOT_MODE=paper`: the paper services can never trade live, even if the shared `.env` is edited.
- A bare `python -m bot.cli pause|kill|resume|unkill` is refused once the areas exist, because it would write to the old
  `runtime/` folder and silently miss the running bot. Use `./deploy/ab paper kill`.
- The `angelbot` MCP server (Claude's tools) works on the paper area.

## Telegram bots
Create three bots with @BotFather: **Paper**, **Live**, **Test**. Put the Paper token in `profiles/paper.env`, the Test token in
`profiles/research.env`, and leave the Live token for `profiles/live.env`. Two controllers must never share a token.
Alert labels: `📝 PAPER`, `🔴 LIVE`, `🧪 TEST` (replay or pytest). A replay sends one summary to the Test bot, never one message per trade.
To see test-run alerts, set `ANGELBOT_TEST_TELEGRAM_BOT_TOKEN` and `ANGELBOT_TEST_TELEGRAM_CHAT_ID` in the shell that runs `pytest`;
without them the tests send nothing, and they can never reach the Paper or Live bot.

## Moving an existing install to the areas (on the VM, outside 09:05 to 15:32)
1. Create the Paper and Test bots; `git pull`; `cp profiles/paper.env.example profiles/paper.env` (same for research), fill in the tokens, `chmod 600`.
2. Stop and disable the old units, and install the new ones:
   ```bash
   systemctl --user disable --now angelbot.timer angelbot.service angelbot-telegram.service
   ```
3. Split the old journal (read-only dry run first):
   ```bash
   python3 deploy/split_journal.py --dry-run
   python3 deploy/split_journal.py
   ```
   Real paper sessions go to `runtime/paper/journal.db`, replays to `runtime/research/replay_journal.db`. The script checks that no row
   or rupee was lost, and only then moves the old files to `runtime/legacy_<date>/` as the backup. Status, control, log, screen and
   selection files move into `runtime/paper/`. It refuses to run if the bot looks active or a destination exists.
4. Install the templates and start the paper area only:
   ```bash
   mkdir -p ~/.config/systemd/user runtime/paper runtime/live runtime/research
   cp deploy/angelbot@.service deploy/angelbot@.timer deploy/angelbot-telegram@.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now angelbot@paper.timer angelbot-telegram@paper.service
   ```
5. Next morning check the 09:05 start and the heartbeat in the Paper bot, and that nothing new appears directly in `runtime/`.

Rollback: re-enable the old units (their installed files are unchanged) and move `runtime/legacy_<date>/journal.db*` back.

## Going live (later, by you only)
Not part of the setup. After the paper phase and `/preflight`: create `config/config.live.yaml` with stricter risk limits, create
`profiles/live.env` from the template, add the two `ANGELBOT_MODE` / `ANGELBOT_LIVE_CONFIRM` lines yourself, and enable
`angelbot@live.timer` and `angelbot-telegram@live`. Without `profiles/live.env` the live services refuse to start.
