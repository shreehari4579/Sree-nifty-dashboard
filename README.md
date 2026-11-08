# Free NSE NIFTY Options Intelligence Dashboard

A mobile-friendly, read-only Streamlit dashboard using NSE's publicly accessible NIFTY option-chain endpoint. No Dhan API key, access token, or paid Dhan Data API subscription is required.

## Analytics

1. ATM straddle expected move and indicative expiry range.
2. Simplified expiry-level breach probabilities.
3. ATM implied-volatility expansion/contraction versus the first app observation.
4. Heuristic directional read from ATM premium changes and call/put IV skew, with PCR as context.
5. Candidate CE/PE short strikes filtered by model-calculated absolute delta and OI.
6. Illustrative hedged-spread max profit/max loss.
7. Observed spot movement versus the first captured straddle-implied move.

## Deploy on Streamlit Community Cloud

1. Upload `app.py`, `requirements.txt`, and `README.md` to your GitHub repository. You can replace the old versions.
2. Open https://share.streamlit.io/ and sign in with GitHub.
3. Create or edit the app, select your repository and `main` branch, and set **Main file path** to `app.py`.
4. Deploy. No secrets or API credentials are required.
5. Open the Streamlit URL in iPhone Safari. Use Share → Add to Home Screen for an app-like shortcut.

## Data-source limitations — please read

- The app requests the publicly accessible NSE endpoint `https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY` using a browser-like session.
- NSE does not provide a guaranteed public API service-level agreement for this use. It may throttle, change, delay or block automated requests, especially from cloud-hosting IP addresses. A 5–15 minute delay is not guaranteed.
- If NSE blocks Streamlit Cloud, the dashboard will show an error rather than silently use fabricated or stale prices. Try again later or run locally from a normal residential connection.
- App refresh defaults to 5 minutes. Increasing refresh frequency does not guarantee faster data and may increase blocking risk.
- The NSE response may not include a clear exchange timestamp. The dashboard displays the timestamp if supplied and separately shows when the app fetched the response.
- Greeks are approximated with Black–Scholes using NSE IV, an assumed 6.5% risk-free rate, and no dividend yield. They are not exchange-provided Greeks.

## Safety and methodology

- ATM straddle is a heuristic, not a guaranteed high/low range or statistical confidence interval.
- Breach probabilities are simplified model estimates, not broker-calculated probabilities.
- OI/PCR does not reveal whether traders are buying or writing options.
- Candidate strikes are not recommendations. Check liquidity, bid/ask spread, event risk, margin and payoff before trading.
- Lot size is editable; verify the current exchange contract specification.
- The dashboard is read-only and does not place, modify or cancel orders.
- Baselines are stored in `data/baselines.json`; Streamlit Cloud's local filesystem may reset when the app restarts, so the first-observation comparison may reset too.
