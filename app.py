import json
import math
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

IST = ZoneInfo("Asia/Kolkata")
NSE_HOME = "https://www.nseindia.com/"
NSE_EXPIRIES = "https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY"
NSE_CHAIN = "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry={expiry}"
BASELINE_FILE = Path("data/baselines.json")

st.set_page_config(page_title="NIFTY Options Intelligence", page_icon="📈", layout="wide")


def num(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def nse_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": NSE_HOME,
        "Connection": "keep-alive",
    })
    # NSE commonly requires cookies obtained from its homepage before API requests.
    s.get("https://www.nseindia.com/option-chain", timeout=15)
    return s


@st.cache_data(ttl=300, show_spinner=False)
def fetch_nse_expiries():
    session = nse_session()
    response = session.get(NSE_EXPIRIES, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"NSE expiry endpoint returned HTTP {response.status_code}; it may be rate-limiting this server.")
    payload = response.json()
    expiries = payload.get("expiryDates") or payload.get("records", {}).get("expiryDates") or []
    if not expiries:
        raise RuntimeError("NSE did not return NIFTY expiry dates in the expected format.")
    return expiries


@st.cache_data(ttl=60, show_spinner=False)
def fetch_nse_chain(expiry):
    session = nse_session()
    response = session.get(NSE_CHAIN.format(expiry=expiry), timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"NSE option-chain endpoint returned HTTP {response.status_code}; it may be rate-limiting or blocking this cloud server.")
    payload = response.json()
    if "records" not in payload or "data" not in payload["records"]:
        raise RuntimeError("NSE response did not contain the expected option-chain records.")
    return payload


def expiry_date_label(value):
    # NSE expiry labels are typically like 12-Nov-2026.
    return datetime.strptime(value, "%d-%b-%Y").date()


def norm_iv(iv):
    return iv / 100.0 if iv > 1 else iv


def bs_greeks(spot, strike, iv_pct, years, option_type):
    """Approximate European Greeks. IV is taken from NSE; rate is assumed 6.5%."""
    sigma = norm_iv(iv_pct)
    if spot <= 0 or strike <= 0 or sigma <= 0 or years <= 0:
        return 0.0, 0.0, 0.0, 0.0
    rate = 0.065
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * years) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    cdf_d1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    if option_type == "CE":
        delta = cdf_d1
        theta = (-(spot * pdf * sigma) / (2 * root_t) - rate * strike * math.exp(-rate * years) * (0.5 * (1 + math.erf(d2 / math.sqrt(2))))) / 365
    else:
        delta = cdf_d1 - 1
        theta = (-(spot * pdf * sigma) / (2 * root_t) + rate * strike * math.exp(-rate * years) * (0.5 * (1 + math.erf(-d2 / math.sqrt(2))))) / 365
    gamma = pdf / (spot * sigma * root_t)
    vega = spot * pdf * root_t / 100
    return delta, gamma, theta, vega


def expiry_time(label):
    d = expiry_date_label(label)
    return datetime.combine(d, time(15, 30), tzinfo=IST)


def parse_chain(payload, expiry_label):
    records = payload["records"]
    spot = num(records.get("underlyingValue"))
    timestamp = records.get("timestamp") or payload.get("timestamp") or "Not supplied by NSE"
    rows = []
    expiry_dt = expiry_time(expiry_label)
    years = max(0, (expiry_dt - datetime.now(IST)).total_seconds()) / (365 * 24 * 3600)
    for item in records.get("data", []):
        if item.get("expiryDate") != expiry_label:
            continue
        strike = num(item.get("strikePrice"))
        for side, key in (("CE", "CE"), ("PE", "PE")):
            opt = item.get(key)
            if not opt:
                continue
            iv = num(opt.get("impliedVolatility"))
            ltp = num(opt.get("lastPrice"))
            change = num(opt.get("change"))
            delta, gamma, theta, vega = bs_greeks(spot, strike, iv, years, side)
            rows.append({
                "Strike": strike, "Type": side, "LTP": ltp,
                "Prev close": max(0, ltp - change), "IV": iv,
                "Delta": delta, "Gamma": gamma, "Theta": theta, "Vega": vega,
                "OI": int(num(opt.get("openInterest"))),
                "Prev OI": int(num(opt.get("openInterest")) - num(opt.get("changeinOpenInterest"))),
                "Volume": int(num(opt.get("totalTradedVolume"))),
                "Bid": num(opt.get("bidprice")), "Ask": num(opt.get("askPrice")),
            })
    return spot, pd.DataFrame(rows), timestamp


def find_option(df, strike, side):
    match = df[(df["Strike"] == strike) & (df["Type"] == side)]
    return None if match.empty else match.iloc[0].to_dict()


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def probability_above(level, spot, iv_pct, years):
    if years <= 0 or iv_pct <= 0 or spot <= 0 or level <= 0:
        return None
    sigma = norm_iv(iv_pct)
    z = (math.log(level / spot) + 0.5 * sigma * sigma * years) / (sigma * math.sqrt(years))
    return 1 - normal_cdf(z)


def load_baselines():
    try:
        return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_baselines(data):
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def pct_change(now, prev):
    return None if prev <= 0 else (now / prev - 1) * 100


def directional_read(atm_ce, atm_pe, pcr):
    ce_chg = pct_change(atm_ce["LTP"], atm_ce["Prev close"])
    pe_chg = pct_change(atm_pe["LTP"], atm_pe["Prev close"])
    skew = atm_pe["IV"] - atm_ce["IV"]
    score, notes = 0, []
    if ce_chg is not None and pe_chg is not None:
        diff = ce_chg - pe_chg
        if diff > 4:
            score += 1; notes.append(f"ATM call premium is outperforming put premium ({diff:+.1f} percentage points)")
        elif diff < -4:
            score -= 1; notes.append(f"ATM put premium is outperforming call premium ({diff:+.1f} percentage points)")
    if skew > 2:
        score -= 1; notes.append(f"Put IV is {skew:.1f} points above call IV (downside-hedging demand)")
    elif skew < -2:
        score += 1; notes.append(f"Call IV is {abs(skew):.1f} points above put IV")
    label = "Bullish signals" if score >= 2 else "Mild bullish signals" if score == 1 else "Bearish signals" if score <= -2 else "Mild bearish signals" if score == -1 else "Neutral / mixed"
    if not notes:
        notes.append("Premium and IV signals are mixed or too small to establish a bias.")
    notes.append(f"Put/Call OI ratio: {pcr:.2f}; OI alone does not reveal whether positions are bought or written.")
    return label, notes, ce_chg, pe_chg, skew


def candidate_table(df, side, delta_low, delta_high, min_oi):
    x = df[df["Type"] == side].copy()
    x["AbsDelta"] = x["Delta"].abs()
    x = x[x["AbsDelta"].between(delta_low, delta_high) & (x["LTP"] > 0) & (x["OI"] >= min_oi)]
    if x.empty:
        return x
    x["Spread %"] = x.apply(lambda r: ((r["Ask"] - r["Bid"]) / r["LTP"] * 100) if r["LTP"] > 0 and r["Ask"] > 0 and r["Bid"] > 0 else None, axis=1)
    x["Delta distance"] = (x["AbsDelta"] - 0.16).abs()
    return x.sort_values(["Delta distance", "Spread %"], na_position="last").head(6)


def spread_risk(df, side, short_row, width, lot_size):
    short_strike = float(short_row["Strike"])
    target = short_strike + width if side == "CE" else short_strike - width
    hedges = df[(df["Type"] == side) & (df["LTP"] > 0)].copy()
    hedges = hedges[hedges["Strike"] >= target] if side == "CE" else hedges[hedges["Strike"] <= target]
    hedges = hedges.sort_values("Strike", ascending=(side == "CE"))
    if hedges.empty:
        return None
    hedge = hedges.iloc[0]
    actual_width = abs(float(hedge["Strike"]) - short_strike)
    credit = float(short_row["LTP"]) - float(hedge["LTP"])
    return {"hedge_strike": float(hedge["Strike"]), "net_credit": credit,
            "max_loss": max(0, actual_width - credit) * lot_size,
            "max_profit": max(0, credit) * lot_size, "width": actual_width}


st.title("📈 NIFTY Weekly Options Intelligence")
st.caption("Free NSE public option-chain data • Read-only analytics • No order execution")

with st.sidebar:
    st.header("Data settings")
    refresh_seconds = st.select_slider("Refresh interval", options=[60, 120, 300, 600], value=300,
                                        format_func=lambda x: f"{x // 60} min" if x >= 60 else f"{x} sec")
    st.caption("NSE public endpoints may delay, rate-limit or block cloud requests. Refreshing more often does not guarantee fresher data.")
    st.divider()
    st.header("Strike filters")
    delta_low = st.slider("Minimum absolute delta", 0.05, 0.40, 0.10, 0.01)
    delta_high = st.slider("Maximum absolute delta", 0.10, 0.50, 0.25, 0.01)
    min_oi = st.number_input("Minimum open interest", min_value=0, value=100000, step=25000)
    lot_size = st.number_input("NIFTY lot size (verify current contract spec)", min_value=1, value=65, step=1)
    hedge_width = st.number_input("Hedge width (points)", min_value=50, max_value=2000, value=200, step=50)

try:
    available_expiries = fetch_nse_expiries()
    today = datetime.now(IST).date()
    valid_expiries = [e for e in available_expiries if expiry_date_label(e) >= today]
    if not valid_expiries:
        st.warning("NSE returned no current or future NIFTY expiries.")
        st.stop()
    selected_expiry = st.selectbox("Select NIFTY expiry", valid_expiries)
    st_autorefresh(interval=refresh_seconds * 1000, key="nse_refresh")
    payload = fetch_nse_chain(selected_expiry)
    spot, chain, source_timestamp = parse_chain(payload, selected_expiry)
    if chain.empty or spot <= 0:
        st.error("NSE returned an empty option chain or invalid NIFTY spot value.")
        st.stop()
except Exception as exc:
    st.error(f"Could not load free NSE option-chain data: {exc}")
    st.markdown("**Try:** wait a few minutes and reload. NSE may restrict automated requests from cloud IP addresses. This app does not fall back to fabricated or stale prices.")
    st.stop()

strikes = sorted(chain["Strike"].dropna().unique().tolist())
atm_strike = min(strikes, key=lambda x: abs(x - spot))
atm_ce, atm_pe = find_option(chain, atm_strike, "CE"), find_option(chain, atm_strike, "PE")
if not atm_ce or not atm_pe:
    st.error("Could not locate both ATM call and put contracts."); st.stop()

now = datetime.now(IST)
expiry_dt = expiry_time(selected_expiry)
seconds_left = max(0, (expiry_dt - now).total_seconds())
years_left = seconds_left / (365 * 24 * 3600)
days_left = seconds_left / (24 * 3600)
atm_straddle = atm_ce["LTP"] + atm_pe["LTP"]
expected_pct = atm_straddle / spot * 100 if spot else 0
lower_range, upper_range = spot - atm_straddle, spot + atm_straddle
atm_iv = (atm_ce["IV"] + atm_pe["IV"]) / 2

baselines = load_baselines()
baseline = baselines.get(selected_expiry)
if not baseline:
    baseline = {"spot": spot, "straddle": atm_straddle, "iv": atm_iv, "captured_at": now.isoformat(), "atm_strike": atm_strike}
    baselines[selected_expiry] = baseline
    save_baselines(baselines)
baseline_spot, baseline_straddle, baseline_iv = num(baseline.get("spot"), spot), num(baseline.get("straddle"), atm_straddle), num(baseline.get("iv"), atm_iv)
realized_points = spot - baseline_spot
realized_pct = realized_points / baseline_spot * 100 if baseline_spot else 0
move_used = abs(realized_points) / baseline_straddle * 100 if baseline_straddle > 0 else 0
iv_change = atm_iv - baseline_iv
vol_state = "IV expansion" if iv_change >= .5 else "IV contraction" if iv_change <= -.5 else "IV broadly stable"
ce_oi, pe_oi = chain.loc[chain.Type == "CE", "OI"].sum(), chain.loc[chain.Type == "PE", "OI"].sum()
pcr = pe_oi / ce_oi if ce_oi else 0
bias, bias_notes, ce_chg, pe_chg, skew = directional_read(atm_ce, atm_pe, pcr)

st.caption(f"NSE source timestamp: {source_timestamp} • App fetched: {now.strftime('%d %b %Y, %H:%M:%S IST')} • Expiry: {selected_expiry} • ATM: {atm_strike:,.0f}")
st.warning("Free public NSE data is best-effort, not a guaranteed 5–15 minute feed. The endpoint may be delayed, blocked or temporarily unavailable. Greeks below are model-calculated from NSE IV, not exchange-supplied Greeks.")

c1, c2, c3, c4 = st.columns(4)
c1.metric("NIFTY spot", f"{spot:,.2f}")
c2.metric("ATM straddle", f"{atm_straddle:,.2f}", f"{expected_pct:.2f}% of spot")
c3.metric("ATM average IV", f"{atm_iv:.2f}%", f"{iv_change:+.2f} vol points vs first capture")
c4.metric("Directional read", bias)

st.divider(); st.header("1–2. Expected move and expiry range")
m1, m2, m3 = st.columns(3)
m1.metric("Premium-implied move", f"±{atm_straddle:,.1f} points")
m2.metric("Lower estimate", f"{lower_range:,.1f}")
m3.metric("Upper estimate", f"{upper_range:,.1f}")
st.caption("ATM call + put premium is a practical expected-move heuristic, not a guaranteed range or confidence interval.")
fig_range = go.Figure(go.Scatter(x=[lower_range, spot, upper_range], y=[0, 1, 0], mode="lines+markers+text", text=[f"Lower {lower_range:,.0f}", f"Spot {spot:,.0f}", f"Upper {upper_range:,.0f}"], textposition="top center"))
fig_range.update_layout(height=240, margin=dict(l=20, r=20, t=20, b=20), xaxis_title="NIFTY level", yaxis=dict(visible=False))
st.plotly_chart(fig_range, use_container_width=True)

st.divider(); st.header("3. Probability of breaching selected levels")
p1, p2 = st.columns(2)
with p1: upper_level = st.number_input("Upper level to test", value=float(round(upper_range / 50) * 50), step=50.0)
with p2: lower_level = st.number_input("Lower level to test", value=float(round(lower_range / 50) * 50), step=50.0)
if years_left > 0 and atm_iv > 0:
    p_up = probability_above(upper_level, spot, atm_iv, years_left)
    p_down = 1 - probability_above(lower_level, spot, atm_iv, years_left)
    q1, q2, q3 = st.columns(3)
    q1.metric(f"Expiry above {upper_level:,.0f}", f"{p_up*100:.1f}%")
    q2.metric(f"Expiry below {lower_level:,.0f}", f"{p_down*100:.1f}%")
    q3.metric("Time to expiry", f"{days_left:.2f} days")
st.caption("Simplified lognormal estimate using average ATM IV and zero drift. Not a broker probability or guarantee.")

st.divider(); st.header("4. Volatility expansion / contraction")
v1, v2, v3 = st.columns(3)
v1.metric("Volatility regime", vol_state); v2.metric("Current ATM average IV", f"{atm_iv:.2f}%"); v3.metric("First-capture IV", f"{baseline_iv:.2f}%")
st.caption(f"Baseline captured at {baseline.get('captured_at', 'unknown')}; on cloud hosting it may reset when the app restarts.")

st.divider(); st.header("5. Directional bias indicators")
st.subheader(bias)
b1, b2, b3 = st.columns(3)
b1.metric("ATM CE change vs previous close", "n/a" if ce_chg is None else f"{ce_chg:+.1f}%")
b2.metric("ATM PE change vs previous close", "n/a" if pe_chg is None else f"{pe_chg:+.1f}%")
b3.metric("Put-call IV skew", f"{skew:+.2f} vol points")
for note in bias_notes: st.write("• " + note)
st.caption("Heuristic only. OI/PCR cannot identify whether participants are buying or writing options.")

st.divider(); st.header("6. Candidate option-selling strikes")
st.write(f"Filters: absolute model delta {delta_low:.2f}–{delta_high:.2f}, minimum OI {min_oi:,.0f}. Ranked near 0.16 absolute delta.")
ce_candidates, pe_candidates = candidate_table(chain, "CE", delta_low, delta_high, min_oi), candidate_table(chain, "PE", delta_low, delta_high, min_oi)
left, right = st.columns(2)
def show_candidates(container, title, candidates, side):
    with container:
        st.subheader(title)
        if candidates.empty:
            st.info("No strikes matched the current filters."); return
        view = candidates[["Strike", "LTP", "Delta", "IV", "OI", "Volume", "Bid", "Ask", "Spread %"]].copy()
        view["Delta"] = view["Delta"].round(3); view["IV"] = view["IV"].round(2); view["Spread %"] = view["Spread %"].round(2)
        st.dataframe(view, hide_index=True, use_container_width=True)
        choice = st.selectbox(f"Select {side} short strike", candidates["Strike"].tolist(), key=f"pick_{side}")
        selected = candidates[candidates["Strike"] == choice].iloc[0].to_dict()
        risk = spread_risk(chain, side, selected, float(hedge_width), int(lot_size))
        if risk:
            st.write(f"Illustrative hedge: {risk['hedge_strike']:,.0f} {side}")
            st.write(f"Estimated net credit: ₹{risk['net_credit']:.2f} per unit")
            st.write(f"Max profit: ₹{risk['max_profit']:,.0f} per lot")
            st.write(f"Max loss: ₹{risk['max_loss']:,.0f} per lot")
            st.caption("Assumes LTP fills; excludes charges, slippage and execution risk.")
        else: st.warning("Could not find a farther OTM hedge in the returned chain.")
show_candidates(left, "Call candidates (CE)", ce_candidates, "CE")
show_candidates(right, "Put candidates (PE)", pe_candidates, "PE")

st.divider(); st.header("7. Observed movement vs first implied move")
r1, r2, r3, r4 = st.columns(4)
r1.metric("Spot change since first capture", f"{realized_points:+,.1f} pts", f"{realized_pct:+.2f}%")
r2.metric("First observed straddle", f"{baseline_straddle:,.1f} pts")
r3.metric("Absolute move used", f"{move_used:.1f}%")
r4.metric("Baseline spot", f"{baseline_spot:,.1f}")

st.divider(); st.header("Option chain around ATM")
near = chain[chain["Strike"].between(atm_strike - 1000, atm_strike + 1000)].copy()
pivot = near.pivot_table(index="Strike", columns="Type", values=["LTP", "IV", "Delta", "OI", "Volume"], aggfunc="first")
pivot.columns = [f"{a} {b}" for a, b in pivot.columns]; st.dataframe(pivot.reset_index().sort_values("Strike"), hide_index=True, use_container_width=True)
fig = go.Figure()
for side in ("CE", "PE"):
    part = near[near.Type == side].sort_values("Strike")
    fig.add_trace(go.Scatter(x=part.Strike, y=part.LTP, mode="lines+markers", name=f"{side} premium"))
fig.add_vline(x=spot, line_dash="dash", annotation_text="Spot")
fig.update_layout(title="Option premium by strike (±1,000 points around ATM)", xaxis_title="Strike", yaxis_title="Premium (₹)", height=420)
st.plotly_chart(fig, use_container_width=True)

with st.expander("Methodology and limitations"):
    st.markdown("""
    - NSE public option-chain endpoint is unofficial for automated use and may block cloud-hosted requests. No data feed is guaranteed free or real-time.
    - Greeks are Black–Scholes approximations calculated from NSE implied volatility, with a 6.5% assumed risk-free rate and no dividend yield.
    - Expected move is ATM CE + PE LTP; expiry range is spot ± straddle premium.
    - Breach probabilities are simplified lognormal estimates, not calibrated forecasts.
    - Directional bias is a heuristic. PCR/OI alone cannot tell whether positions are long or short.
    - Candidate strikes are filters, not recommendations. Validate live liquidity, spreads, events, margin and payoff independently.
    - Defined-risk spread assumes LTP fills and excludes charges, slippage and execution risk.
    - Lot size is editable. Verify the current NIFTY contract specification before using risk numbers.
    - The app is read-only and never places, modifies or cancels orders.
    """)
