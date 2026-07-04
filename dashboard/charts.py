"""
Reusable Plotly figure builders.
All functions return a go.Figure — callers render with st.plotly_chart(fig, use_container_width=True).
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

from dashboard.utils import RESULT_COLORS, EXIT_COLORS, CONVICTION_COLORS


# ── Portfolio page ────────────────────────────────────────────────────────────

def cumulative_pnl_chart(df: pd.DataFrame, daily_loss_limit: float) -> go.Figure:
    """Cumulative P&L line chart with daily loss limit reference line."""
    s = df.sort_values("date")
    cum = s.groupby("date")["pnl_rs"].sum().cumsum().reset_index()
    cum.columns = ["date", "cumulative_pnl"]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=cum["date"], y=cum["cumulative_pnl"],
        mode="lines", name="Cumulative P&L",
        line=dict(color="#3b82f6", width=2),
        fill="tozeroy",
        fillcolor="rgba(59,130,246,0.1)",
    ))
    fig.add_hline(
        y=-daily_loss_limit,
        line_dash="dash", line_color="#ef4444",
        annotation_text=f"Daily loss limit  Rs {daily_loss_limit:,.0f}",
        annotation_position="bottom right",
    )
    fig.update_layout(
        title="Cumulative P&L",
        xaxis_title="Date", yaxis_title="Rs",
        margin=dict(t=40, b=40),
        hovermode="x unified",
    )
    return fig


def result_donut(df: pd.DataFrame) -> go.Figure:
    counts = df["result"].value_counts()
    labels = counts.index.tolist()
    values = counts.values.tolist()
    colors = [RESULT_COLORS.get(l, "#94a3b8") for l in labels]
    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.55, marker_colors=colors,
        textinfo="label+percent",
    ))
    fig.update_layout(title="Win / Loss Split", margin=dict(t=40, b=20, l=20, r=20))
    return fig


def exit_reason_bar(df: pd.DataFrame) -> go.Figure:
    counts = df["exit_reason"].value_counts().reset_index()
    counts.columns = ["exit_reason", "count"]
    colors = [EXIT_COLORS.get(r, "#94a3b8") for r in counts["exit_reason"]]
    fig = go.Figure(go.Bar(
        x=counts["exit_reason"], y=counts["count"],
        marker_color=colors, text=counts["count"], textposition="outside",
    ))
    fig.update_layout(title="Exit Reason", margin=dict(t=40, b=40), yaxis_title="Count")
    return fig


def conviction_tier_bar(df: pd.DataFrame) -> go.Figure:
    counts = df["conviction_tier"].value_counts().reset_index()
    counts.columns = ["conviction_tier", "count"]
    colors = [CONVICTION_COLORS.get(t, "#94a3b8") for t in counts["conviction_tier"]]
    fig = go.Figure(go.Bar(
        x=counts["conviction_tier"], y=counts["count"],
        marker_color=colors, text=counts["count"], textposition="outside",
    ))
    fig.update_layout(title="Conviction Tier", margin=dict(t=40, b=40), yaxis_title="Count")
    return fig


# ── Signals page ──────────────────────────────────────────────────────────────

def strategy_firing_freq(df: pd.DataFrame) -> go.Figure:
    """Horizontal bar chart of how often each strategy appears in strategies_fired."""
    from dashboard.data_loader import explode_strategies
    exploded = explode_strategies(df)
    if exploded.empty:
        return go.Figure()
    counts = exploded["strategy"].value_counts().sort_values()
    fig = go.Figure(go.Bar(
        x=counts.values, y=counts.index,
        orientation="h",
        marker_color="#3b82f6",
        text=counts.values, textposition="outside",
    ))
    fig.update_layout(
        title="Strategy Firing Frequency",
        xaxis_title="Signal Count",
        margin=dict(t=40, b=40, l=120),
        height=max(400, len(counts) * 24),
    )
    return fig


def composite_score_histogram(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for result, color in RESULT_COLORS.items():
        subset = df[df["result"] == result]
        if subset.empty:
            continue
        fig.add_trace(go.Histogram(
            x=subset["composite_score"],
            name=result,
            marker_color=color,
            opacity=0.7,
            nbinsx=20,
        ))
    fig.update_layout(
        title="Composite Score Distribution",
        barmode="overlay",
        xaxis_title="Composite Score", yaxis_title="Count",
        margin=dict(t=40, b=40),
    )
    return fig


def strategy_month_heatmap(df: pd.DataFrame) -> go.Figure:
    """Strategy × Month win-rate heatmap (RdYlGn color scale)."""
    if df.empty or "strategies_fired" not in df.columns:
        return go.Figure()

    exploded = (
        df[["month", "result", "strategies_fired"]]
        .assign(strategy=df["strategies_fired"].str.split(","))
        .explode("strategy")
    )
    exploded["strategy"] = exploded["strategy"].str.strip()
    exploded["win"] = exploded["result"].isin(["EXACT_WIN", "WIN"]).astype(int)

    pivot = exploded.groupby(["strategy", "month"])["win"].mean().unstack(fill_value=float("nan"))

    fig = go.Figure(go.Heatmap(
        z=pivot.values * 100,
        x=pivot.columns.tolist(),
        y=pivot.index.tolist(),
        colorscale="RdYlGn",
        zmin=0, zmax=100,
        colorbar=dict(title="Win %"),
        hoverongaps=False,
    ))
    fig.update_layout(
        title="Strategy × Month Win Rate (%)",
        xaxis_title="Month", yaxis_title="Strategy",
        margin=dict(t=40, b=60, l=100),
        height=max(400, len(pivot) * 24),
    )
    return fig


# ── P&L page ──────────────────────────────────────────────────────────────────

def monthly_pnl_bar(df: pd.DataFrame, monthly_target: float) -> go.Figure:
    """Monthly P&L bars with 3% target line and running cumulative."""
    monthly = df.groupby("month")["pnl_rs"].sum().reset_index()
    monthly.columns = ["month", "pnl_rs"]
    monthly = monthly.sort_values("month")
    monthly["cumulative"] = monthly["pnl_rs"].cumsum()
    monthly["color"] = monthly["pnl_rs"].apply(lambda x: "#22c55e" if x >= 0 else "#f87171")

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(
        x=monthly["month"], y=monthly["pnl_rs"],
        name="Monthly P&L", marker_color=monthly["color"],
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=monthly["month"], y=monthly["cumulative"],
        name="Cumulative P&L", mode="lines+markers",
        line=dict(color="#3b82f6", width=2, dash="dot"),
    ), secondary_y=True)
    fig.add_hline(y=monthly_target, line_dash="dot", line_color="#facc15",
                  annotation_text="3% target", secondary_y=False)
    fig.update_layout(
        title="Monthly P&L",
        xaxis_title="Month",
        margin=dict(t=40, b=60),
        hovermode="x unified",
    )
    fig.update_yaxes(title_text="Monthly Rs", secondary_y=False)
    fig.update_yaxes(title_text="Cumulative Rs", secondary_y=True)
    return fig


def drawdown_chart(df: pd.DataFrame, capital: float, daily_loss_limit: float) -> go.Figure:
    from dashboard.data_loader import compute_equity_curve
    s = compute_equity_curve(df, capital)
    if s.empty:
        return go.Figure()

    daily = s.groupby("date").agg(pnl_rs=("pnl_rs", "sum")).reset_index()
    daily["equity"] = capital + daily["pnl_rs"].cumsum()
    daily["drawdown"] = daily["equity"] - daily["equity"].cummax()
    max_dd = daily["drawdown"].min()
    max_dd_date = daily.loc[daily["drawdown"].idxmin(), "date"]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=("Equity Curve", "Drawdown"))

    fig.add_trace(go.Scatter(
        x=daily["date"], y=daily["equity"],
        mode="lines", name="Equity", line=dict(color="#3b82f6", width=2),
    ), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=daily["date"], y=daily["drawdown"],
        mode="lines", name="Drawdown",
        line=dict(color="#ef4444", width=1),
        fill="tozeroy", fillcolor="rgba(239,68,68,0.2)",
    ), row=2, col=1)

    fig.add_hline(y=-daily_loss_limit, line_dash="dash", line_color="#f97316",
                  annotation_text=f"Daily limit  Rs {daily_loss_limit:,.0f}",
                  row=2, col=1)

    fig.add_annotation(
        x=max_dd_date, y=max_dd,
        text=f"Max DD  Rs {max_dd:,.0f}",
        showarrow=True, arrowhead=2, row=2, col=1,
    )
    fig.update_layout(title="Equity & Drawdown", margin=dict(t=60, b=40), height=500)
    return fig


def daily_pnl_bar(df: pd.DataFrame) -> go.Figure:
    daily = df.groupby("date")["pnl_rs"].sum().reset_index()
    daily["color"] = daily["pnl_rs"].apply(lambda x: "#22c55e" if x >= 0 else "#f87171")
    fig = go.Figure(go.Bar(
        x=daily["date"], y=daily["pnl_rs"],
        marker_color=daily["color"], name="Daily P&L",
    ))
    fig.update_layout(title="Daily P&L", xaxis_title="Date", yaxis_title="Rs",
                      margin=dict(t=40, b=40))
    return fig


def yearly_summary_table(df: pd.DataFrame, parquet_df: pd.DataFrame) -> go.Figure:
    """Combined yearly summary from paper_trades + parquet history."""
    frames = []
    if not df.empty and "year" in df.columns:
        frames.append(df.assign(source="paper"))
    if not parquet_df.empty and "year" in parquet_df.columns and "pnl_rs" in parquet_df.columns:
        frames.append(parquet_df.assign(source="parquet"))

    if not frames:
        return go.Figure()

    combined = pd.concat(frames, ignore_index=True)
    grp = combined.groupby("year")
    rows = []
    for year, g in grp:
        wins = g["result"].isin(["EXACT_WIN", "WIN"]).sum() if "result" in g.columns else 0
        total = len(g)
        wr = wins / total * 100 if total else 0
        pnl = g["pnl_rs"].sum()
        rows.append({"Year": int(year), "Trades": total, "Win %": f"{wr:.1f}%",
                     "P&L Rs": f"Rs {pnl:+,.0f}"})

    summary = pd.DataFrame(rows).sort_values("Year")
    fig = go.Figure(go.Table(
        header=dict(values=list(summary.columns), fill_color="#1e293b", font_color="white"),
        cells=dict(values=[summary[c] for c in summary.columns],
                   fill_color=[["#0f172a" if i % 2 == 0 else "#1e293b" for i in range(len(summary))]
                               for _ in summary.columns],
                   font_color="white"),
    ))
    fig.update_layout(title="Yearly Summary", margin=dict(t=40, b=20))
    return fig


# ── Strategies page ───────────────────────────────────────────────────────────

def weight_vs_wr_bubble(weights: dict, lifetime_wr: dict, signal_counts: dict) -> go.Figure:
    strategies = sorted(set(weights) | set(lifetime_wr))
    x, y, size, labels = [], [], [], []
    for s in strategies:
        wr = lifetime_wr.get(s, 50.0)
        w  = weights.get(s, 1.0)
        cnt = signal_counts.get(s, 10)
        x.append(wr)
        y.append(w)
        size.append(max(cnt, 5))
        labels.append(s)

    fig = go.Figure(go.Scatter(
        x=x, y=y, mode="markers+text",
        text=labels, textposition="top center",
        marker=dict(
            size=[min(s / 3, 40) + 8 for s in size],
            color=x, colorscale="RdYlGn", cmin=30, cmax=75,
            showscale=True, colorbar=dict(title="Lifetime WR %"),
            line=dict(width=1, color="#1e293b"),
        ),
        hovertemplate="<b>%{text}</b><br>WR: %{x:.1f}%<br>Weight: %{y:.3f}<extra></extra>",
    ))
    fig.add_vline(x=50, line_dash="dash", line_color="#94a3b8", annotation_text="50% WR")
    fig.update_layout(
        title="Weight vs Lifetime Win Rate (bubble = signal count)",
        xaxis_title="Lifetime Win Rate (%)", yaxis_title="Current Weight",
        margin=dict(t=40, b=40),
    )
    return fig


def rolling_performance_lines(perf: dict, selected_strategies: list[str] | None = None) -> go.Figure:
    """Rolling-10 mean from strategy_performance.json signal arrays."""
    import numpy as np
    strategies = selected_strategies or list(perf.keys())
    fig = go.Figure()
    for strat in strategies:
        arr = perf.get(strat, [])
        if len(arr) < 5:
            continue
        s = pd.Series(arr, dtype=float)
        rolling = s.rolling(10, min_periods=3).mean() * 100
        fig.add_trace(go.Scatter(
            y=rolling, mode="lines", name=strat,
            hovertemplate=f"<b>{strat}</b><br>Rolling WR: %{{y:.1f}}%<extra></extra>",
        ))
    fig.add_hline(y=50, line_dash="dash", line_color="#94a3b8", annotation_text="50%")
    fig.update_layout(
        title="Rolling-10 Signal Win Rate per Strategy",
        xaxis_title="Signal #", yaxis_title="Win Rate (%)",
        margin=dict(t=40, b=40),
        hovermode="x unified",
    )
    return fig


def strategy_year_heatmap(df: pd.DataFrame) -> go.Figure:
    """Strategy × Year win-rate heatmap."""
    if df.empty or "strategies_fired" not in df.columns:
        return go.Figure()

    exploded = (
        df[["year", "result", "strategies_fired"]]
        .assign(strategy=df["strategies_fired"].str.split(","))
        .explode("strategy")
    )
    exploded["strategy"] = exploded["strategy"].str.strip()
    exploded["win"] = exploded["result"].isin(["EXACT_WIN", "WIN"]).astype(int)

    pivot = exploded.groupby(["strategy", "year"])["win"].mean().unstack(fill_value=float("nan"))

    fig = go.Figure(go.Heatmap(
        z=pivot.values * 100,
        x=[str(c) for c in pivot.columns],
        y=pivot.index.tolist(),
        colorscale="RdYlGn",
        zmin=0, zmax=100,
        colorbar=dict(title="Win %"),
        hoverongaps=False,
        hovertemplate="Strategy: %{y}<br>Year: %{x}<br>Win Rate: %{z:.1f}%<extra></extra>",
    ))
    fig.update_layout(
        title="Strategy × Year Win Rate (%)",
        xaxis_title="Year", yaxis_title="Strategy",
        margin=dict(t=40, b=60, l=120),
        height=max(500, len(pivot) * 22),
    )
    return fig


# ── Page 6 — Candle chart ─────────────────────────────────────────────────────

def candle_chart(
    candles: "pd.DataFrame",
    trade_row: "pd.Series | None" = None,
    date_str: str = "",
) -> go.Figure:
    """
    5-min OHLCV candlestick for a single trading day.
    Overlays entry (green triangle-up) and exit (red triangle-down) markers
    if a trade_row is provided.
    """
    import pandas as pd

    day = candles.copy()
    if date_str:
        day = day[day["datetime"].dt.date.astype(str) == date_str]

    if day.empty:
        fig = go.Figure()
        fig.update_layout(title=f"No candle data for {date_str}")
        return fig

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.04,
        subplot_titles=("Price (5-min)", "Volume"),
    )

    # Candlestick
    fig.add_trace(go.Candlestick(
        x=day["datetime"],
        open=day["open"], high=day["high"],
        low=day["low"],  close=day["close"],
        name="OHLC",
        increasing_line_color="#22c55e",
        decreasing_line_color="#ef4444",
    ), row=1, col=1)

    # Volume bars
    vol_colors = [
        "#22c55e" if c >= o else "#ef4444"
        for c, o in zip(day["close"], day["open"])
    ]
    fig.add_trace(go.Bar(
        x=day["datetime"], y=day["volume"],
        name="Volume", marker_color=vol_colors, opacity=0.6,
    ), row=2, col=1)

    # Trade entry / exit markers
    if trade_row is not None:
        entry_time = pd.to_datetime(f"{date_str} {trade_row.get('signal_time', '')}", errors="coerce")
        if pd.notna(entry_time):
            entry_time = entry_time.tz_localize("Asia/Kolkata")
            fig.add_trace(go.Scatter(
                x=[entry_time], y=[trade_row["entry_price"]],
                mode="markers", name="Entry",
                marker=dict(symbol="triangle-up", size=14, color="#22c55e",
                            line=dict(width=1, color="#ffffff")),
                hovertemplate=(
                    f"ENTRY<br>Price: {trade_row['entry_price']:.2f}<br>"
                    f"Strategy: {trade_row.get('driver_strategy', '—')}<extra></extra>"
                ),
            ), row=1, col=1)

        exit_time = pd.to_datetime(f"{date_str} {trade_row.get('exit_time', '')}", errors="coerce")
        if pd.notna(exit_time):
            exit_time = exit_time.tz_localize("Asia/Kolkata")
            result   = trade_row.get("result", "")
            ex_color = "#22c55e" if "WIN" in result else "#ef4444"
            pnl      = trade_row.get("pnl_rs", 0)
            fig.add_trace(go.Scatter(
                x=[exit_time], y=[trade_row["exit_price"]],
                mode="markers", name="Exit",
                marker=dict(symbol="triangle-down", size=14, color=ex_color,
                            line=dict(width=1, color="#ffffff")),
                hovertemplate=(
                    f"EXIT ({trade_row.get('exit_reason', '—')})<br>"
                    f"Price: {trade_row['exit_price']:.2f}<br>"
                    f"P&L: Rs {pnl:+,.0f}<extra></extra>"
                ),
            ), row=1, col=1)

        # Stop-loss and target lines
        for level, label, color in [
            ("stop_loss", "Stop Loss", "#ef4444"),
            ("target",    "Target",    "#22c55e"),
        ]:
            if level in trade_row and pd.notna(trade_row[level]):
                fig.add_hline(
                    y=trade_row[level], line_dash="dash",
                    line_color=color, opacity=0.6,
                    annotation_text=label,
                    annotation_position="right",
                    row=1, col=1,
                )

    symbol = day["symbol"].iloc[0] if "symbol" in day.columns else ""
    fig.update_layout(
        title=f"{symbol} — 5-min candles  {date_str}",
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        margin=dict(t=60, b=40),
        height=600,
        legend=dict(orientation="h", y=1.02),
    )
    fig.update_xaxes(
        tickformat="%H:%M",
        rangebreaks=[dict(bounds=["sat", "mon"])],
    )
    return fig
