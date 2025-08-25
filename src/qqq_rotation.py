import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
import matplotlib.pyplot as plt
import matplotlib as mpl

mpl.rcParams['font.sans-serif'] = ['Microsoft YaHei']

# ── 使用者參數（請自行修改 DB_URI） ──
DB_URI      = 'postgresql://<username>:<password>@<host>:5432/<dbname>'  # ← 改成你自己的
START_DATE  = '2016-01-01'
END_DATE    = '2024-12-31'
N_SELECT    = 10      # 每次選前 N 檔
ETF_CODE    = 'QQQ'
N_GROUPS    = 3       # 分組數

# 1) 建連線 & 讀資料
engine = create_engine(DB_URI)

# 價格
df_price = pd.read_sql(text("""
    SELECT da::date AS da, code, adj
    FROM price
    WHERE da BETWEEN :start AND :end
"""), engine, params={"start": START_DATE, "end": END_DATE})
df_price['da'] = pd.to_datetime(df_price['da'])
price = df_price.pivot(index='da', columns='code', values='adj').sort_index()

# 市值
df_mc = pd.read_sql(text("""
    SELECT da::date AS da, code, cap::float AS cap
    FROM market_cap
    WHERE da BETWEEN :start AND :end
"""), engine, params={"start": START_DATE, "end": END_DATE})
df_mc['da'] = pd.to_datetime(df_mc['da'])
mc = df_mc.pivot(index='da', columns='code', values='cap').reindex(price.index).ffill()

# 成分股歷史
df_h = pd.read_sql(text("""
    SELECT da::date AS da, code
    FROM etf_holdings
    WHERE belongs_to = :etf
      AND da BETWEEN :start AND :end
    ORDER BY da
"""), engine, params={"etf": ETF_CODE, "start": START_DATE, "end": END_DATE})
df_h['da'] = pd.to_datetime(df_h['da'])
first_holding_date = df_h['da'].min()
if pd.isna(first_holding_date):
    first_holding_date = price.index.min()

# 技術指標
ma60    = price.rolling(60).mean()
cap_mom = mc - mc.shift(100)

def get_constituents(asof):
    eff = max(asof, first_holding_date)
    sub = df_h[df_h['da'] <= eff]
    if sub.empty:
        return list(price.columns)
    latest = sub['da'].max()
    return sub.loc[sub['da']==latest, 'code'].tolist()

# 2) 每月第一交易日 (signal) & T+1 買進日 (trade)
dates       = price.index
months      = sorted({d.to_period('M') for d in dates})
month_first = [min(d for d in dates if d.to_period('M') == m) for m in months]
month_trade = []
for d in month_first:
    pos = dates.get_loc(d)
    nxt = dates[pos+1] if pos+1 < len(dates) and dates[pos+1].to_period('M') == d.to_period('M') else None
    month_trade.append(nxt)
month2idx = {m: i for i, m in enumerate(months)}

group_ws = [pd.Series(0, index=price.columns) for _ in range(N_GROUPS)]
pending  = [None] * N_GROUPS
w_list, d_list = [], []

trade_set = {d: month2idx[d.to_period('M')] % N_GROUPS for d in month_trade if d is not None}

for today in dates:
    # signal：產生待執行名單
    if today in month_first:
        gi = month2idx[today.to_period('M')] % N_GROUPS
        cons = [c for c in get_constituents(today) if c in price.columns]
        wins = []
        if cons:
            m60  = ma60.loc[today, cons]
            p0   = price.loc[today, cons]
            mask = m60.isna() | (p0 > m60)              # 站上 MA60
            mom  = cap_mom.loc[today, cons].copy()      # 動量
            mom.fillna(mc.loc[today, cons], inplace=True)
            wins = mom[mask].nlargest(N_SELECT).index.tolist()
        pending[gi] = wins

    # trade：T+1 依 pending 實際調倉
    if today in trade_set:
        gi   = trade_set[today]
        wins = pending[gi] or []
        w    = pd.Series(0, index=price.columns)
        if wins:
            w.loc[wins] = 1.0 / len(wins)
        group_ws[gi] = w
        pending[gi]  = None

    # 合併三組持倉
    total_w = sum(group_ws) / N_GROUPS
    w_list.append(total_w)
    d_list.append(today)

# 3) 回測與績效
port_w   = pd.DataFrame(w_list, index=d_list).fillna(0)
ret      = price.pct_change().reindex(port_w.index)
port_ret = (port_w.shift(1) * ret).sum(axis=1)
nav      = (1 + port_ret).cumprod()

days   = port_ret.dropna().shape[0]
CAGR   = nav.iloc[-1]**(252/days) - 1
MaxDD  = (nav/nav.cummax() - 1).min()
Sharpe = port_ret.mean() / port_ret.std() * np.sqrt(252)

print(f"期間：{START_DATE} ~ {END_DATE}")
print(f"CAGR   : {CAGR:.2%}")
print(f"MaxDD  : {MaxDD:.2%}")
print(f"Sharpe : {Sharpe:.2f}")

# 4) 圖表輸出到 images/
import os
os.makedirs('images', exist_ok=True)
plt.figure(figsize=(7,3.5))
nav.plot()
plt.title('3-Group Monthly Rotation (T+1)')
plt.xlabel('Date'); plt.ylabel('NAV'); plt.grid(True, alpha=.3)
plt.tight_layout()
plt.savefig('images/nav_plot.png', dpi=200)
