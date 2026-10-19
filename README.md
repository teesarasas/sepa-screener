# SEPA Screener

A US stock screener based on Mark Minervini's SEPA method, with a Streamlit dashboard you can open from any browser.

- **Weekly scan, Saturday 09:00 Bangkok.** Screens about 1,500–2,500 liquid US stocks against the Trend Template, RS rating, VCP bases and EPS growth, then builds a watchlist.
- **Daily check, Tue–Sat 05:30 Bangkok.** Compares each watchlist stock with its pivot after the US close and flags breakouts.
- **Dashboard.** Streamlit Community Cloud reads the results. Nothing runs on your computer.

```
GitHub Actions (free server, on a schedule)        Streamlit Cloud (the website)
  pipeline.py → Twelve Data → data/*.csv  ──push──▶  app.py reads data/*.csv
```

The heavy fetching runs on GitHub, where jobs can last up to 6 hours. Streamlit only reads small files, so the dashboard opens in seconds.

---

## One-time setup (about 20 minutes)

### 1. Create a private GitHub repo and upload the code

1. Go to github.com → **New repository** → name it `sepa-screener` → choose **Private** → Create.
2. On the empty repo page, click **uploading an existing file**. Drag in **everything inside this folder**, including the hidden `.github` and `.streamlit` folders, then **Commit changes**.
   - On Windows, turn on *View → Show → Hidden items* in File Explorer so you can see `.github`.
   - Check afterwards that `.github/workflows/screener.yml` exists in the repo. Without it, nothing runs on a schedule.

Or, with git:
```bash
cd sepa-screener
git init && git add . && git commit -m "initial"
git branch -M main
git remote add origin https://github.com/<you>/sepa-screener.git
git push -u origin main
```

### 2. Add your Twelve Data key to GitHub

Repo → **Settings → Secrets and variables → Actions**.

- **Secrets** tab → *New repository secret*:
  - Name: `TWELVE_DATA_API_KEY`
  - Value: your key
- **Variables** tab (optional) → *New repository variable*:
  - `TD_CREDITS_PER_MIN`: your plan's API credits per minute, from your Twelve Data dashboard. The default is `55`. If your Grow tier allows more, set it here and every scan gets faster.
  - `TD_FUNDAMENTAL_BUDGET`: the maximum credits spent on EPS and sector data per weekly run. The default is `6000`.

### 3. Run the first scan by hand

Repo → **Actions** tab. If asked, click *I understand… enable workflows*. Then open **Screener data → Run workflow** and choose `weekly`.

The first run is the longest, roughly **2–4 hours** at 55 credits/minute:
- it checks every NYSE/NASDAQ common stock for liquidity (~5,000+ symbols), and
- it fetches EPS and sector data for every Trend Template pass.

Later weekly runs take about **40–60 minutes**, because EPS data is cached and refreshed only after each company reports. Daily checks take **1–2 minutes**.

When the run finishes, a commit named `data: weekly …` appears in the repo.

### 4. Deploy the dashboard on Streamlit Community Cloud

1. Go to **share.streamlit.io** → sign in with GitHub → allow access to your private repos.
2. **Create app** → deploy from your GitHub repo → repo `sepa-screener`, branch `main`, main file `app.py`.
3. **Advanced settings → Secrets**: paste the content of `.streamlit/secrets.toml.example`, filled in with your values.
   - `TWELVE_DATA_API_KEY` powers the Chart tab, at 1 credit per ticker, cached for 6 hours.
   - `APP_PASSWORD` is optional.
4. **Deploy.** Apps from a private repo can only be viewed by people you invite: your app → **Share**. Leave it as is, or invite only your own email.

Bookmark the app URL. It works from your phone or any browser, with nothing installed.

---

## Using it

| When | Where | What to do |
|---|---|---|
| Saturday or Sunday | **Screener** tab | Review the new Trend Template passes and VCP setups. Open each one in **Chart** and confirm the base by eye. |
| Each morning, about 30 seconds | **Signals** tab | Look for 🟢 **Breakout** or 🟡 **Ready**. Check the market banner at the top. |
| Before buying | **Position size** tab | Get share count from your account size, risk % and the stop. |

**Signal statuses**

| Status | Meaning |
|---|---|
| 🟢 Breakout | Closed above the pivot on ≥ 1.4× average volume, and buying now keeps the stop within 8% |
| 🟠 Breakout, weak volume | Above the pivot, but volume didn't confirm. Minervini treats these with suspicion. |
| 🟡 Ready | Within 5% below the pivot. Consider a buy-stop order at the pivot. |
| ⚪ Forming | Base still developing |
| 🔵 Extended | Above the pivot, but buying now would put the stop more than 8% away (or it's over 10% above the pivot). Don't chase it; wait for a pullback toward the pivot or a new base. |
| 🔴 Failed | Closed below the suggested stop |

**Leadership columns.** These help you find leaders in a weak market:
- **RS line** is the stock ÷ S&P 500. ✓ means the RS line is at a 52-week high. ★ means it got there *before* the price did, the classic early-leader tell.
- **Fell vs SPY** is the stock's deepest fall since the S&P 500's last high, divided by the S&P 500's fall. ✓ means 2.5× or less, so the stock held up. Minervini: *"The best stocks make their lows before the general market averages do."*
- Both are filters on the Screener tab.

**Risk at entry** is the distance to the stop if you buy now (or at the pivot, if it hasn't broken out yet). Keep it under 8%.

The **market banner** uses SPY and QQQ: green when both are in an uptrend, amber in a correction (below the 50-day), red when either is below its 200-day. Red is Minervini's cue to stay mostly in cash.

---

## What's automated and what isn't

| SEPA step | Status | Notes |
|---|---|---|
| Trend Template, criteria 1–7 | ✅ Automated | Daily bars from `/time_series` |
| RS rating, criterion 8 | ✅ Automated | IBD-style approximation: 40% last 3 months + 20% each for the 6-, 9- and 12-month periods, ranked 1–99 across the liquid universe |
| VCP base, pivot, stop | ⚠️ Heuristic | A swing-point detector finds 2–6 shrinking pullbacks. It shortlists candidates; **always confirm on the chart.** |
| EPS growth, acceleration, earnings beats | ✅ Automated | `/earnings` on the Grow plan, for Trend Template passes only. Growth % is hidden when the year-ago quarter is negative or under $0.05 (shown as *turnaround* or *tiny base* instead), and those don't count as EPS ✓. |
| Sales growth, margins ("Code 33") | ❌ Manual | Needs Twelve Data's Pro plan. Check by hand using the Finviz link on the Chart tab. |
| Sector strength | ✅ Automated | SPDR sector ETFs, plus a count of leaders per sector |
| Catalyst | ❌ Manual | |

**Known limitations**
- **RS is an approximation, not IBD's number.** It's a percentile within this universe, so values won't match MarketSmith.
- **The screen uses today's listed stocks only.** That's fine for a live screen, but don't use these files to backtest; that would carry survivorship bias.
- **Duplicate tickers are removed automatically.** Old tickers of renamed or merged companies (e.g. CDEV → PR) that return identical prices are dropped, keeping the one with a company profile.
- **If EPS data runs out of budget**, the remaining names show "pending" and are filled in the following week.

---

## Changing the rules

All thresholds live in `config.py`: price and volume floors, Trend Template percentages, VCP depths, the breakout volume multiple, maximum stop, and the EPS minimum. Edit the file on GitHub (pencil icon) and commit. The next run uses the new values, or you can run the workflow by hand straight away.

To change the schedule, edit the two `cron` lines in `.github/workflows/screener.yml`. They're in **UTC**, and Bangkok is UTC+7.

---

## Cost and limits

| | Per run | Per month |
|---|---|---|
| Twelve Data credits | weekly ~2,000–2,500 (first run ~10,000+), daily ~30 | well within Grow (no daily cap) |
| GitHub Actions minutes | weekly ~45, daily ~2 | ~250–400 of the 2,000 free minutes for private repos |
| Streamlit Community Cloud | — | free |

The Chart tab and the scans share your per-minute credit limit. If you open charts while a weekly scan is running, the scan just waits a few seconds.

---

## Troubleshooting

- **Dashboard shows old data.** Streamlit picks up new commits automatically, usually within a minute. If it doesn't, open your app → ⋮ → **Reboot app**.
- **Run failed with "TWELVE_DATA_API_KEY is not set".** The secret name must match exactly (step 2).
- **Run failed at "Save data to the repo" with a permission error.** Repo → Settings → Actions → General → *Workflow permissions* → **Read and write** → Save.
- **"Data is N days old" warning.** Open the Actions tab to see whether runs failed. GitHub emails you when a scheduled run fails.
- **Test without an API key.** Run `pip install -r requirements.txt && python tests/smoke_test.py` to execute the whole pipeline on synthetic data in a temp folder.

---

*This tool screens a published method; it isn't financial advice. Every trade decision, including the stop, is yours.*
